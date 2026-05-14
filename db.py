from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from config import DEFAULT_RULES
from utils.blocklist import normalize_blocked_terms


CONFIG_COLUMNS = {
    "log_channel_id",
    "rules_channel_id",
    "report_channel_id",
    "appeal_channel_id",
    "automod_enabled",
    "invite_filter_enabled",
    "caps_filter_enabled",
    "spam_filter_enabled",
    "repeat_filter_enabled",
    "mention_filter_enabled",
    "min_account_age_hours",
    "warn_timeout_threshold",
    "warn_kick_threshold",
    "warn_ban_threshold",
    "warn_timeout_minutes",
    "caps_ratio",
    "caps_min_length",
    "mention_threshold",
    "spam_threshold",
    "spam_window_seconds",
    "repeat_threshold",
    "repeat_window_seconds",
    "raid_mode",
    "security_enabled",
    "antinuke_enabled",
    "antinuke_threshold",
    "antinuke_window_seconds",
    "antinuke_timeout_minutes",
    "audit_message_logs_enabled",
    "audit_server_logs_enabled",
}

ROLE_LIST_COLUMNS = {"mod_role_ids", "admin_role_ids"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        database_path = Path(path).expanduser()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(database_path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _initialize(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id INTEGER PRIMARY KEY,
                    mod_role_ids TEXT NOT NULL DEFAULT '[]',
                    admin_role_ids TEXT NOT NULL DEFAULT '[]',
                    log_channel_id INTEGER,
                    rules_channel_id INTEGER,
                    report_channel_id INTEGER,
                    appeal_channel_id INTEGER,
                    automod_enabled INTEGER NOT NULL DEFAULT 1,
                    invite_filter_enabled INTEGER NOT NULL DEFAULT 1,
                    caps_filter_enabled INTEGER NOT NULL DEFAULT 1,
                    spam_filter_enabled INTEGER NOT NULL DEFAULT 1,
                    repeat_filter_enabled INTEGER NOT NULL DEFAULT 1,
                    mention_filter_enabled INTEGER NOT NULL DEFAULT 1,
                    min_account_age_hours INTEGER NOT NULL DEFAULT 0,
                    warn_timeout_threshold INTEGER NOT NULL DEFAULT 3,
                    warn_kick_threshold INTEGER NOT NULL DEFAULT 5,
                    warn_ban_threshold INTEGER NOT NULL DEFAULT 7,
                    warn_timeout_minutes INTEGER NOT NULL DEFAULT 1440,
                    caps_ratio REAL NOT NULL DEFAULT 0.75,
                    caps_min_length INTEGER NOT NULL DEFAULT 24,
                    mention_threshold INTEGER NOT NULL DEFAULT 5,
                    spam_threshold INTEGER NOT NULL DEFAULT 6,
                    spam_window_seconds INTEGER NOT NULL DEFAULT 12,
                    repeat_threshold INTEGER NOT NULL DEFAULT 3,
                    repeat_window_seconds INTEGER NOT NULL DEFAULT 90,
                    raid_mode INTEGER NOT NULL DEFAULT 0,
                    security_enabled INTEGER NOT NULL DEFAULT 1,
                    antinuke_enabled INTEGER NOT NULL DEFAULT 1,
                    antinuke_threshold INTEGER NOT NULL DEFAULT 4,
                    antinuke_window_seconds INTEGER NOT NULL DEFAULT 120,
                    antinuke_timeout_minutes INTEGER NOT NULL DEFAULT 60,
                    audit_message_logs_enabled INTEGER NOT NULL DEFAULT 1,
                    audit_server_logs_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    points INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS blocked_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    term TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, term)
                );

                CREATE TABLE IF NOT EXISTS lenient_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    term TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, term)
                );

                CREATE TABLE IF NOT EXISTS promo_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    term TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, term)
                );

                CREATE TABLE IF NOT EXISTS cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    points INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS embed_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    footer TEXT,
                    image_url TEXT,
                    thumbnail_url TEXT,
                    fields_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE(guild_id, name)
                );

                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    author_id INTEGER NOT NULL,
                    target_id INTEGER,
                    case_id INTEGER,
                    reason TEXT NOT NULL,
                    evidence_url TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ticket_abuse_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    execute_at TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intro_acknowledgements (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS bluesky_feeds (
                    guild_id INTEGER PRIMARY KEY,
                    handle TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_post_uri TEXT,
                    last_post_created_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS security_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target_id INTEGER,
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            for column_name, column_sql in (
                ("security_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("antinuke_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("antinuke_threshold", "INTEGER NOT NULL DEFAULT 4"),
                ("antinuke_window_seconds", "INTEGER NOT NULL DEFAULT 120"),
                ("antinuke_timeout_minutes", "INTEGER NOT NULL DEFAULT 60"),
                ("audit_message_logs_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("audit_server_logs_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ):
                self._ensure_column_locked("guild_config", column_name, column_sql)
            self._ensure_column_locked("bluesky_feeds", "last_post_created_at", "TEXT")
            self.connection.commit()

    def _ensure_column_locked(self, table_name: str, column_name: str, column_sql: str) -> None:
        rows = self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column_name in existing:
            return
        self.connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def ensure_guild(self, guild_id: int) -> None:
        with self._lock:
            now = utcnow_iso()
            self.connection.execute(
                "INSERT OR IGNORE INTO guild_config (guild_id, created_at, updated_at) VALUES (?, ?, ?)",
                (guild_id, now, now),
            )
            existing_rules = self.connection.execute(
                "SELECT COUNT(*) AS count FROM rules WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()["count"]
            if existing_rules == 0:
                for index, (title, description, points) in enumerate(DEFAULT_RULES, start=1):
                    self.connection.execute(
                        "INSERT INTO rules (guild_id, position, title, description, points, enabled) VALUES (?, ?, ?, ?, ?, 1)",
                        (guild_id, index, title, description, points),
                    )
            self.connection.commit()

    def _touch_guild(self, guild_id: int) -> None:
        self.connection.execute(
            "UPDATE guild_config SET updated_at = ? WHERE guild_id = ?",
            (utcnow_iso(), guild_id),
        )

    def get_guild_config(self, guild_id: int) -> dict[str, Any]:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM guild_config WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Missing config row for guild {guild_id}.")

        data = dict(row)
        data["mod_role_ids"] = json.loads(data["mod_role_ids"])
        data["admin_role_ids"] = json.loads(data["admin_role_ids"])
        for key in (
            "automod_enabled",
            "invite_filter_enabled",
            "caps_filter_enabled",
            "spam_filter_enabled",
            "repeat_filter_enabled",
            "mention_filter_enabled",
            "raid_mode",
            "security_enabled",
            "antinuke_enabled",
            "audit_message_logs_enabled",
            "audit_server_logs_enabled",
        ):
            data[key] = bool(data[key])
        return data

    def create_backup(self, destination_path: str) -> str:
        destination = Path(destination_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            backup_connection = sqlite3.connect(str(destination))
            try:
                self.connection.backup(backup_connection)
            finally:
                backup_connection.close()
        return str(destination)

    def add_security_event(
        self,
        guild_id: int,
        actor_id: int,
        action: str,
        *,
        target_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO security_events (guild_id, actor_id, action, target_id, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    actor_id,
                    action,
                    target_id,
                    json.dumps(details or {}),
                    utcnow_iso(),
                ),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def count_recent_security_events(
        self,
        guild_id: int,
        actor_id: int,
        *,
        actions: list[str],
        since_iso: str,
    ) -> int:
        if not actions:
            return 0
        placeholders = ",".join("?" for _ in actions)
        with self._lock:
            row = self.connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM security_events
                WHERE guild_id = ?
                  AND actor_id = ?
                  AND action IN ({placeholders})
                  AND created_at >= ?
                """,
                (guild_id, actor_id, *actions, since_iso),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def list_recent_security_events(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT *
                FROM security_events
                WHERE guild_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event["details"] or "{}")
            events.append(event)
        return events

    def set_config_value(self, guild_id: int, column: str, value: Any) -> None:
        if column not in CONFIG_COLUMNS:
            raise ValueError(f"Unsupported config column: {column}")
        self.ensure_guild(guild_id)
        with self._lock:
            self.connection.execute(
                f"UPDATE guild_config SET {column} = ? WHERE guild_id = ?",
                (value, guild_id),
            )
            self._touch_guild(guild_id)
            self.connection.commit()

    def save_bluesky_feed(
        self,
        guild_id: int,
        *,
        handle: str,
        channel_id: int,
        enabled: bool = True,
        last_post_uri: str | None = None,
        last_post_created_at: str | None = None,
    ) -> None:
        self.ensure_guild(guild_id)
        normalized_handle = handle.strip().lstrip("@").lower()
        if not normalized_handle:
            raise ValueError("Bluesky handle cannot be empty.")
        now = utcnow_iso()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO bluesky_feeds (
                    guild_id, handle, channel_id, enabled, last_post_uri, last_post_created_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    handle = excluded.handle,
                    channel_id = excluded.channel_id,
                    enabled = excluded.enabled,
                    last_post_uri = excluded.last_post_uri,
                    last_post_created_at = excluded.last_post_created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id,
                    normalized_handle,
                    channel_id,
                    1 if enabled else 0,
                    last_post_uri,
                    last_post_created_at,
                    now,
                    now,
                ),
            )
            self.connection.commit()

    def get_bluesky_feed(self, guild_id: int) -> dict[str, Any] | None:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM bluesky_feeds WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return data

    def list_enabled_bluesky_feeds(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM bluesky_feeds WHERE enabled = 1 ORDER BY guild_id ASC",
            ).fetchall()
        feeds: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["enabled"] = bool(data["enabled"])
            feeds.append(data)
        return feeds

    def set_bluesky_feed_enabled(self, guild_id: int, enabled: bool) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE bluesky_feeds SET enabled = ?, updated_at = ? WHERE guild_id = ?",
                (1 if enabled else 0, utcnow_iso(), guild_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def update_bluesky_feed_cursor(
        self,
        guild_id: int,
        *,
        last_post_uri: str | None,
        last_post_created_at: str | None,
    ) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE bluesky_feeds
                SET last_post_uri = ?, last_post_created_at = ?, updated_at = ?
                WHERE guild_id = ?
                """,
                (last_post_uri, last_post_created_at, utcnow_iso(), guild_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def delete_bluesky_feed(self, guild_id: int) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM bluesky_feeds WHERE guild_id = ?",
                (guild_id,),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def add_role_id(self, guild_id: int, column: str, role_id: int) -> None:
        if column not in ROLE_LIST_COLUMNS:
            raise ValueError(f"Unsupported role list column: {column}")
        config = self.get_guild_config(guild_id)
        values = set(config[column])
        values.add(role_id)
        with self._lock:
            self.connection.execute(
                f"UPDATE guild_config SET {column} = ? WHERE guild_id = ?",
                (json.dumps(sorted(values)), guild_id),
            )
            self._touch_guild(guild_id)
            self.connection.commit()

    def remove_role_id(self, guild_id: int, column: str, role_id: int) -> None:
        if column not in ROLE_LIST_COLUMNS:
            raise ValueError(f"Unsupported role list column: {column}")
        config = self.get_guild_config(guild_id)
        values = [value for value in config[column] if value != role_id]
        with self._lock:
            self.connection.execute(
                f"UPDATE guild_config SET {column} = ? WHERE guild_id = ?",
                (json.dumps(values), guild_id),
            )
            self._touch_guild(guild_id)
            self.connection.commit()

    def list_rules(self, guild_id: int) -> list[dict[str, Any]]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT id, position, title, description, points, enabled FROM rules WHERE guild_id = ? ORDER BY position ASC, id ASC",
                (guild_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_rule(self, guild_id: int, title: str, description: str, points: int) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            max_position_row = self.connection.execute(
                "SELECT COALESCE(MAX(position), 0) AS max_position FROM rules WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            next_position = int(max_position_row["max_position"]) + 1
            cursor = self.connection.execute(
                "INSERT INTO rules (guild_id, position, title, description, points, enabled) VALUES (?, ?, ?, ?, ?, 1)",
                (guild_id, next_position, title, description, points),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def update_rule(
        self,
        guild_id: int,
        rule_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        points: int | None = None,
        enabled: bool | None = None,
    ) -> bool:
        self.ensure_guild(guild_id)
        updates: list[str] = []
        values: list[Any] = []
        if title is not None:
            updates.append("title = ?")
            values.append(title)
        if description is not None:
            updates.append("description = ?")
            values.append(description)
        if points is not None:
            updates.append("points = ?")
            values.append(points)
        if enabled is not None:
            updates.append("enabled = ?")
            values.append(1 if enabled else 0)
        if not updates:
            return False

        values.extend((guild_id, rule_id))
        with self._lock:
            cursor = self.connection.execute(
                f"UPDATE rules SET {', '.join(updates)} WHERE guild_id = ? AND id = ?",
                tuple(values),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def delete_rule(self, guild_id: int, rule_id: int) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM rules WHERE guild_id = ? AND id = ?",
                (guild_id, rule_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def reset_rules(self, guild_id: int) -> None:
        self.ensure_guild(guild_id)
        with self._lock:
            self.connection.execute("DELETE FROM rules WHERE guild_id = ?", (guild_id,))
            for index, (title, description, points) in enumerate(DEFAULT_RULES, start=1):
                self.connection.execute(
                    "INSERT INTO rules (guild_id, position, title, description, points, enabled) VALUES (?, ?, ?, ?, ?, 1)",
                    (guild_id, index, title, description, points),
                )
            self.connection.commit()

    def add_blocked_word(self, guild_id: int, term: str) -> bool:
        self.ensure_guild(guild_id)
        normalized = term.strip().lower()
        if not normalized:
            return False
        with self._lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO blocked_words (guild_id, term, created_at) VALUES (?, ?, ?)",
                (guild_id, normalized, utcnow_iso()),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def remove_blocked_word(self, guild_id: int, term: str) -> bool:
        self.ensure_guild(guild_id)
        normalized = term.strip().lower()
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM blocked_words WHERE guild_id = ? AND term = ?",
                (guild_id, normalized),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def list_blocked_words(self, guild_id: int) -> list[str]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT term FROM blocked_words WHERE guild_id = ? ORDER BY term ASC",
                (guild_id,),
            ).fetchall()
        return [str(row["term"]) for row in rows]

    def count_blocked_words(self, guild_id: int) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM blocked_words WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return int(row["count"])

    def clear_blocked_words(self, guild_id: int) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM blocked_words WHERE guild_id = ?",
                (guild_id,),
            )
            self.connection.commit()
            return int(cursor.rowcount)

    def bulk_add_blocked_words(self, guild_id: int, terms: list[str]) -> int:
        self.ensure_guild(guild_id)
        normalized_terms = normalize_blocked_terms(terms)
        if not normalized_terms:
            return 0
        rows = [(guild_id, term, utcnow_iso()) for term in normalized_terms]
        with self._lock:
            before = self.connection.total_changes
            self.connection.executemany(
                "INSERT OR IGNORE INTO blocked_words (guild_id, term, created_at) VALUES (?, ?, ?)",
                rows,
            )
            self.connection.commit()
            return int(self.connection.total_changes - before)

    def add_lenient_word(self, guild_id: int, term: str) -> bool:
        self.ensure_guild(guild_id)
        normalized = term.strip().lower()
        if not normalized:
            return False
        with self._lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO lenient_words (guild_id, term, created_at) VALUES (?, ?, ?)",
                (guild_id, normalized, utcnow_iso()),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def remove_lenient_word(self, guild_id: int, term: str) -> bool:
        self.ensure_guild(guild_id)
        normalized = term.strip().lower()
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM lenient_words WHERE guild_id = ? AND term = ?",
                (guild_id, normalized),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def list_lenient_words(self, guild_id: int) -> list[str]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT term FROM lenient_words WHERE guild_id = ? ORDER BY term ASC",
                (guild_id,),
            ).fetchall()
        return [str(row["term"]) for row in rows]

    def clear_lenient_words(self, guild_id: int) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM lenient_words WHERE guild_id = ?",
                (guild_id,),
            )
            self.connection.commit()
            return int(cursor.rowcount)

    def bulk_add_lenient_words(self, guild_id: int, terms: list[str]) -> int:
        self.ensure_guild(guild_id)
        normalized_terms = normalize_blocked_terms(terms)
        if not normalized_terms:
            return 0
        rows = [(guild_id, term, utcnow_iso()) for term in normalized_terms]
        with self._lock:
            before = self.connection.total_changes
            self.connection.executemany(
                "INSERT OR IGNORE INTO lenient_words (guild_id, term, created_at) VALUES (?, ?, ?)",
                rows,
            )
            self.connection.commit()
            return int(self.connection.total_changes - before)

    def add_promo_keyword(self, guild_id: int, term: str) -> bool:
        self.ensure_guild(guild_id)
        normalized = term.strip().lower()
        if not normalized:
            return False
        with self._lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO promo_keywords (guild_id, term, created_at) VALUES (?, ?, ?)",
                (guild_id, normalized, utcnow_iso()),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def remove_promo_keyword(self, guild_id: int, term: str) -> bool:
        self.ensure_guild(guild_id)
        normalized = term.strip().lower()
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM promo_keywords WHERE guild_id = ? AND term = ?",
                (guild_id, normalized),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def list_promo_keywords(self, guild_id: int) -> list[str]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT term FROM promo_keywords WHERE guild_id = ? ORDER BY term ASC",
                (guild_id,),
            ).fetchall()
        return [str(row["term"]) for row in rows]

    def clear_promo_keywords(self, guild_id: int) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM promo_keywords WHERE guild_id = ?",
                (guild_id,),
            )
            self.connection.commit()
            return int(cursor.rowcount)

    def bulk_add_promo_keywords(self, guild_id: int, terms: list[str]) -> int:
        self.ensure_guild(guild_id)
        normalized_terms = normalize_blocked_terms(terms)
        if not normalized_terms:
            return 0
        rows = [(guild_id, term, utcnow_iso()) for term in normalized_terms]
        with self._lock:
            before = self.connection.total_changes
            self.connection.executemany(
                "INSERT OR IGNORE INTO promo_keywords (guild_id, term, created_at) VALUES (?, ?, ?)",
                rows,
            )
            self.connection.commit()
            return int(self.connection.total_changes - before)

    def add_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str,
        *,
        points: int = 0,
        active: bool = True,
        expires_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO cases (
                    guild_id, user_id, moderator_id, action, reason, points,
                    active, expires_at, created_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    moderator_id,
                    action,
                    reason,
                    points,
                    1 if active else 0,
                    expires_at,
                    utcnow_iso(),
                    json.dumps(metadata or {}),
                ),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def get_case(self, guild_id: int, case_id: int) -> dict[str, Any] | None:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM cases WHERE guild_id = ? AND id = ?",
                (guild_id, case_id),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = json.loads(data["metadata"])
        data["active"] = bool(data["active"])
        return data

    def list_member_cases(self, guild_id: int, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM cases WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, user_id, limit),
            ).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            item["active"] = bool(item["active"])
            data.append(item)
        return data

    def list_active_warning_cases(self, guild_id: int, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT *
                FROM cases
                WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND active = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, user_id, limit),
            ).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            item["active"] = bool(item["active"])
            data.append(item)
        return data

    def get_latest_member_case(
        self,
        guild_id: int,
        user_id: int,
        *,
        actions: list[str] | None = None,
        active_only: bool = False,
    ) -> dict[str, Any] | None:
        self.ensure_guild(guild_id)
        query = ["SELECT * FROM cases WHERE guild_id = ? AND user_id = ?"]
        values: list[Any] = [guild_id, user_id]
        if actions:
            placeholders = ",".join("?" for _ in actions)
            query.append(f"AND action IN ({placeholders})")
            values.extend(actions)
        if active_only:
            query.append("AND active = 1")
        query.append("ORDER BY id DESC LIMIT 1")
        with self._lock:
            row = self.connection.execute(" ".join(query), tuple(values)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = json.loads(data["metadata"])
        data["active"] = bool(data["active"])
        return data

    def search_cases(
        self,
        guild_id: int,
        *,
        user_id: int | None = None,
        action: str | None = None,
        created_after: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        self.ensure_guild(guild_id)
        query = ["SELECT * FROM cases WHERE guild_id = ?"]
        values: list[Any] = [guild_id]
        if user_id is not None:
            query.append("AND user_id = ?")
            values.append(user_id)
        if action is not None:
            query.append("AND action = ?")
            values.append(action)
        if created_after is not None:
            query.append("AND created_at >= ?")
            values.append(created_after)
        query.append("ORDER BY id DESC LIMIT ?")
        values.append(limit)
        with self._lock:
            rows = self.connection.execute(" ".join(query), tuple(values)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            item["active"] = bool(item["active"])
            results.append(item)
        return results

    def deactivate_case(self, guild_id: int, case_id: int) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE cases SET active = 0 WHERE guild_id = ? AND id = ? AND active = 1",
                (guild_id, case_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def deactivate_latest_warning_for_member(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        warning = self.get_latest_member_case(guild_id, user_id, actions=["warn"], active_only=True)
        if warning is None:
            return None
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE cases SET active = 0 WHERE guild_id = ? AND id = ? AND active = 1",
                (guild_id, warning["id"]),
            )
            self.connection.commit()
        return warning if cursor.rowcount > 0 else None

    def clear_active_warnings_for_member(self, guild_id: int, user_id: int) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE cases SET active = 0 WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND active = 1",
                (guild_id, user_id),
            )
            self.connection.commit()
            return int(cursor.rowcount)

    def get_active_warning_points(self, guild_id: int, user_id: int) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT COALESCE(SUM(points), 0) AS total FROM cases WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND active = 1",
                (guild_id, user_id),
            ).fetchone()
        return int(row["total"])

    def save_embed_template(
        self,
        guild_id: int,
        name: str,
        title: str,
        description: str,
        *,
        footer: str | None = None,
        image_url: str | None = None,
        thumbnail_url: str | None = None,
        fields: list[dict[str, Any]] | None = None,
    ) -> None:
        self.ensure_guild(guild_id)
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO embed_templates (
                    guild_id, name, title, description, footer, image_url,
                    thumbnail_url, fields_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, name) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    footer = excluded.footer,
                    image_url = excluded.image_url,
                    thumbnail_url = excluded.thumbnail_url,
                    fields_json = excluded.fields_json
                """,
                (
                    guild_id,
                    name.lower(),
                    title,
                    description,
                    footer,
                    image_url,
                    thumbnail_url,
                    json.dumps(fields or []),
                    utcnow_iso(),
                ),
            )
            self.connection.commit()

    def get_embed_template(self, guild_id: int, name: str) -> dict[str, Any] | None:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM embed_templates WHERE guild_id = ? AND name = ?",
                (guild_id, name.lower()),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["fields_json"] = json.loads(data["fields_json"])
        return data

    def list_embed_templates(self, guild_id: int) -> list[str]:
        self.ensure_guild(guild_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT name FROM embed_templates WHERE guild_id = ? ORDER BY name ASC",
                (guild_id,),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def add_report(
        self,
        guild_id: int,
        kind: str,
        author_id: int,
        target_id: int | None,
        reason: str,
        *,
        case_id: int | None = None,
        evidence_url: str | None = None,
    ) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO reports (
                    guild_id, kind, author_id, target_id, case_id,
                    reason, evidence_url, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (guild_id, kind, author_id, target_id, case_id, reason, evidence_url, utcnow_iso()),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def get_report(self, guild_id: int, report_id: int) -> dict[str, Any] | None:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM reports WHERE guild_id = ? AND id = ?",
                (guild_id, report_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_reports(
        self,
        guild_id: int,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        self.ensure_guild(guild_id)
        query = ["SELECT * FROM reports WHERE guild_id = ?"]
        values: list[Any] = [guild_id]
        if kind is not None:
            query.append("AND kind = ?")
            values.append(kind)
        if status is not None:
            query.append("AND status = ?")
            values.append(status)
        query.append("ORDER BY id DESC LIMIT ?")
        values.append(limit)
        with self._lock:
            rows = self.connection.execute(" ".join(query), tuple(values)).fetchall()
        return [dict(row) for row in rows]

    def get_latest_report_by_author(
        self,
        guild_id: int,
        author_id: int,
        *,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        self.ensure_guild(guild_id)
        query = ["SELECT * FROM reports WHERE guild_id = ? AND author_id = ?"]
        values: list[Any] = [guild_id, author_id]
        if kind is not None:
            query.append("AND kind = ?")
            values.append(kind)
        query.append("ORDER BY created_at DESC LIMIT 1")
        with self._lock:
            row = self.connection.execute(" ".join(query), tuple(values)).fetchone()
        return dict(row) if row is not None else None

    def list_recent_reports_by_author(
        self,
        guild_id: int,
        author_id: int,
        *,
        kind: str | None = None,
        since_iso: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        self.ensure_guild(guild_id)
        query = ["SELECT * FROM reports WHERE guild_id = ? AND author_id = ?"]
        values: list[Any] = [guild_id, author_id]
        if kind is not None:
            query.append("AND kind = ?")
            values.append(kind)
        if since_iso is not None:
            query.append("AND created_at >= ?")
            values.append(since_iso)
        query.append("ORDER BY created_at DESC LIMIT ?")
        values.append(limit)
        with self._lock:
            rows = self.connection.execute(" ".join(query), tuple(values)).fetchall()
        return [dict(row) for row in rows]

    def update_report_status(self, guild_id: int, report_id: int, status: str) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE reports SET status = ? WHERE guild_id = ? AND id = ?",
                (status, guild_id, report_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def add_ticket_abuse_event(
        self,
        guild_id: int,
        author_id: int,
        *,
        kind: str,
        reason: str,
    ) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO ticket_abuse_events (
                    guild_id, author_id, kind, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, author_id, kind, reason, utcnow_iso()),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def count_recent_ticket_abuse_events(
        self,
        guild_id: int,
        author_id: int,
        *,
        since_iso: str,
    ) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM ticket_abuse_events
                WHERE guild_id = ? AND author_id = ? AND created_at >= ?
                """,
                (guild_id, author_id, since_iso),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def schedule_action(
        self,
        guild_id: int,
        user_id: int,
        action: str,
        execute_at: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "INSERT INTO scheduled_actions (guild_id, user_id, action, execute_at, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, action, execute_at, json.dumps(payload or {}), utcnow_iso()),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def list_due_actions(self, before_iso: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM scheduled_actions WHERE execute_at <= ? ORDER BY execute_at ASC, id ASC",
                (before_iso,),
            ).fetchall()
        actions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            actions.append(item)
        return actions

    def delete_scheduled_action(self, action_id: int) -> None:
        with self._lock:
            self.connection.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))
            self.connection.commit()

    def has_intro_acknowledgement(self, guild_id: int, user_id: int) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM intro_acknowledgements WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return row is not None

    def mark_intro_acknowledgement(self, guild_id: int, user_id: int, *, message_id: int | None = None) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO intro_acknowledgements (
                    guild_id, user_id, message_id, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, message_id, utcnow_iso()),
            )
            self.connection.commit()
            return cursor.rowcount > 0

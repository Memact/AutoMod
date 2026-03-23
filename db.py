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
        self._lock = threading.Lock()
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
                    caps_min_length INTEGER NOT NULL DEFAULT 12,
                    mention_threshold INTEGER NOT NULL DEFAULT 5,
                    spam_threshold INTEGER NOT NULL DEFAULT 6,
                    spam_window_seconds INTEGER NOT NULL DEFAULT 12,
                    repeat_threshold INTEGER NOT NULL DEFAULT 3,
                    repeat_window_seconds INTEGER NOT NULL DEFAULT 90,
                    raid_mode INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS scheduled_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    execute_at TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            self.connection.commit()

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
        ):
            data[key] = bool(data[key])
        return data

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

    def deactivate_case(self, guild_id: int, case_id: int) -> bool:
        self.ensure_guild(guild_id)
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE cases SET active = 0 WHERE guild_id = ? AND id = ? AND active = 1",
                (guild_id, case_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

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

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
EMBED_COLOR = 0x00011B
BOT_JOIN_ROLE_ID = 1485762440980336810
MEMBER_JOIN_ROLE_ID = 1485762960851996822
ACTION_LOG_CHANNEL_ID = 1485774247698890903
WELCOME_CHANNEL_ID = 1485763966515941618
INTRO_CHANNEL_ID = 1485764265989246996
TICKET_CHANNEL_ID = 1486012638507827210

REPORT_COOLDOWN_SECONDS = 300
APPEAL_COOLDOWN_SECONDS = 43200
TICKET_COOLDOWN_SECONDS = 600

REPORT_MIN_LENGTH = 20
APPEAL_MIN_LENGTH = 30
TICKET_MIN_LENGTH = 20

DUPLICATE_WINDOW_SECONDS = 3600
ABUSE_STRIKE_WINDOW_SECONDS = 900
ABUSE_STRIKE_THRESHOLD = 3
ABUSE_TIMEOUT_MINUTES = 60

DEFAULT_RULES = [
    ("Be respectful", "No harassment, hate speech, targeted insults, or personal attacks.", 2),
    ("No spam", "Do not flood channels, mass mention people, or post repetitive content.", 1),
    ("No scams or malicious links", "Phishing, malware, token grabbers, and suspicious links are not allowed.", 3),
    ("No NSFW outside allowed spaces", "Keep explicit or shocking content out of general areas unless staff approved it.", 2),
    ("No unsolicited advertising", "Do not self-promote, sell, or post invite links without staff approval.", 1),
    ("Stay on topic", "Use the correct channels and keep names, nicknames, and profile content appropriate.", 1),
    ("Do not evade moderation", "Bypassing a timeout, ban, filter, or alt-account restriction is a serious violation.", 3),
    ("Staff discretion applies", "Staff may act to protect the community even when a case is not listed word-for-word.", 1),
]


@dataclass(frozen=True)
class Settings:
    token: str
    database_path: str
    stream_title: str
    stream_url: str
    dev_guild_id: int | None = None
    application_id: int | None = None


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_env() -> None:
    _load_dotenv()


def get_command_guild_ids() -> list[int] | None:
    _load_dotenv()
    guild_id_raw = os.getenv("MEMACT_GUILD_ID", "").strip()
    if guild_id_raw.isdigit():
        return [int(guild_id_raw)]
    return None


COMMAND_GUILD_IDS = get_command_guild_ids()


def get_application_id_from_token(token: str) -> int | None:
    segment = token.split(".", 1)[0].strip()
    if not segment:
        return None
    padding = "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment + padding).decode("ascii")
    except Exception:
        return None
    return int(decoded) if decoded.isdigit() else None


def load_settings() -> Settings:
    _load_dotenv()

    token = os.getenv("MEMACT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MEMACT_TOKEN is required. Add it to your environment or .env file.")

    database_path_raw = os.getenv("MEMACT_DATABASE", "memact_automod.db").strip() or "memact_automod.db"
    database_path = Path(database_path_raw).expanduser()
    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    guild_id_raw = os.getenv("MEMACT_GUILD_ID", "").strip()
    dev_guild_id = int(guild_id_raw) if guild_id_raw.isdigit() else None
    stream_title = os.getenv("MEMACT_STREAM_TITLE", "Moderating this server").strip() or "Moderating this server"
    stream_url = os.getenv("MEMACT_STREAM_URL", "https://twitch.tv/discord").strip() or "https://twitch.tv/discord"

    return Settings(
        token=token,
        database_path=str(database_path),
        stream_title=stream_title,
        stream_url=stream_url,
        dev_guild_id=dev_guild_id,
        application_id=get_application_id_from_token(token),
    )

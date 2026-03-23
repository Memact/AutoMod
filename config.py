from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
EMBED_COLOR = 0x00011B

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

    return Settings(
        token=token,
        database_path=str(database_path),
        dev_guild_id=dev_guild_id,
        application_id=get_application_id_from_token(token),
    )

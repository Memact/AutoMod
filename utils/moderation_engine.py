from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlparse


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.IGNORECASE)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)", re.IGNORECASE)

SUSPICIOUS_LINK_TOKENS = (
    "discord-gifts",
    "discordgift",
    "nitro-free",
    "free-nitro",
    "steamcomrnunity",
    "steancommunity",
    "claim-prize",
    "airdrop",
    "walletconnect",
)

SAFE_MEDIA_HOSTS = {
    "tenor.com",
    "media.tenor.com",
    "giphy.com",
    "media.giphy.com",
    "imgur.com",
    "i.imgur.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
}

PROMO_CTA_RE = re.compile(
    r"\b("
    r"join (my|our|this)"
    r"|follow (me|us|my|our)"
    r"|subscribe( to)?"
    r"|check out"
    r"|visit (my|our|this)"
    r"|use code"
    r"|limited time"
    r"|free giveaway"
    r"|buy now"
    r"|dm me"
    r"|click (here|this)"
    r")\b",
    re.IGNORECASE,
)

PROMO_WORDS = {
    "promo",
    "promotion",
    "discount",
    "coupon",
    "sale",
    "deal",
    "offer",
    "giveaway",
    "subscribe",
    "follow",
    "server",
    "discord",
    "youtube",
    "twitch",
    "instagram",
    "tiktok",
    "shop",
    "store",
    "merch",
    "commission",
}


@dataclass(frozen=True)
class AutomodSignal:
    kind: str
    label: str
    score: float
    delete: bool = False
    warn_points: int = 0


@dataclass(frozen=True)
class AutomodDecision:
    action: str
    reason: str
    score: float
    signals: tuple[AutomodSignal, ...]
    delete_message: bool = False
    warn_points: int = 0
    soft_strike: bool = False


def normalize_content(content: str) -> str:
    text = ZERO_WIDTH_RE.sub("", content)
    text = text.replace("`", "").replace("*", "").replace("_", "")
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(text.casefold().split())


def extract_urls(content: str) -> list[str]:
    urls = URL_RE.findall(content)
    for label, url in MARKDOWN_LINK_RE.findall(content):
        urls.append(url)
        if label and URL_RE.search(label):
            urls.extend(URL_RE.findall(label))
    return [url.rstrip(".,!?;:") for url in urls]


def is_safe_media_url(url: str) -> bool:
    host = urlparse(url).netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    if host in SAFE_MEDIA_HOSTS:
        return True
    return any(host.endswith(f".{safe_host}") for safe_host in SAFE_MEDIA_HOSTS)


def caps_ratio(content: str) -> tuple[int, float]:
    letters = [char for char in content if char.isalpha()]
    if not letters:
        return 0, 0.0
    uppercase = sum(1 for char in letters if char.isupper())
    return len(letters), uppercase / len(letters)


def member_trust_multiplier(
    *,
    account_age_hours: float,
    joined_age_hours: float,
    raid_mode: bool,
) -> float:
    multiplier = 1.0
    if account_age_hours < 24 or joined_age_hours < 1:
        multiplier += 0.45
    elif account_age_hours < 72 or joined_age_hours < 24:
        multiplier += 0.25
    elif account_age_hours > 720 and joined_age_hours > 168:
        multiplier -= 0.2
    if raid_mode:
        multiplier += 0.2
    return max(0.75, min(multiplier, 1.65))


def evaluate_automod(
    *,
    content: str,
    config: dict,
    blocked_patterns: list[tuple[str, re.Pattern[str]]],
    promo_keywords: set[str],
    mention_count: int,
    account_age_hours: float,
    joined_age_hours: float,
    has_attachments: bool,
    spam_triggered: bool,
    repeat_triggered: bool,
) -> AutomodDecision:
    normalized = normalize_content(content)
    urls = extract_urls(content)
    non_media_urls = [url for url in urls if not is_safe_media_url(url)]
    media_only = bool(urls) and not non_media_urls and not normalized.replace(" ".join(urls).casefold(), "").strip()
    signals: list[AutomodSignal] = []

    if media_only or (has_attachments and not normalized):
        return AutomodDecision("none", "Media-only message ignored.", 0.0, ())

    for word, pattern in blocked_patterns:
        if pattern.search(normalized):
            signals.append(
                AutomodSignal(
                    "blocked_word",
                    f"blocked word `{word}`",
                    3.4,
                    delete=True,
                )
            )
            break

    if urls and any(token in normalized for token in SUSPICIOUS_LINK_TOKENS):
        signals.append(
            AutomodSignal(
                "scam_link",
                "suspicious scam-link pattern",
                8.0,
                delete=True,
                warn_points=3,
            )
        )

    if config.get("invite_filter_enabled") and INVITE_RE.search(normalized):
        signals.append(AutomodSignal("invite", "Discord invite link", 3.6, delete=True))

    if non_media_urls:
        promo_terms = {term.casefold() for term in promo_keywords}
        promo_terms.update(PROMO_WORDS)
        promo_hits = [term for term in promo_terms if term and term in normalized]
        if PROMO_CTA_RE.search(normalized) or len(promo_hits) >= 2:
            signals.append(AutomodSignal("promotion", "promotional link context", 3.0, delete=True))

    if config.get("mention_filter_enabled") and mention_count >= int(config["mention_threshold"]):
        signals.append(
            AutomodSignal(
                "mass_mentions",
                f"{mention_count} mentions",
                6.0,
                delete=True,
                warn_points=1,
            )
        )

    if config.get("caps_filter_enabled"):
        letter_count, ratio = caps_ratio(content)
        min_letters = max(int(config["caps_min_length"]), 24)
        word_count = len(normalized.split())
        if letter_count >= min_letters and word_count >= 4 and ratio >= float(config["caps_ratio"]):
            signals.append(AutomodSignal("caps", f"heavy caps at {ratio:.0%}", 1.1))

    if spam_triggered:
        signals.append(AutomodSignal("message_flood", "message flood threshold", 5.4, delete=True, warn_points=1))

    if repeat_triggered:
        signals.append(AutomodSignal("repeat_spam", "repeated message threshold", 4.8, delete=True, warn_points=1))

    if not signals:
        return AutomodDecision("none", "No automod action.", 0.0, ())

    multiplier = member_trust_multiplier(
        account_age_hours=account_age_hours,
        joined_age_hours=joined_age_hours,
        raid_mode=bool(config.get("raid_mode")),
    )
    score = sum(signal.score for signal in signals) * multiplier
    delete_message = any(signal.delete for signal in signals)
    warn_points = max((signal.warn_points for signal in signals), default=0)

    if score < 2.8 and not delete_message and warn_points <= 0:
        return AutomodDecision("none", "Low-confidence signal ignored.", score, tuple(signals))

    if warn_points > 0 and score >= 4.0:
        action = "warn"
    elif delete_message and score >= 2.8:
        action = "delete"
    else:
        action = "log"
        delete_message = False

    if action == "delete" and any(signal.kind in {"blocked_word", "invite", "promotion"} for signal in signals):
        soft_strike = True
    else:
        soft_strike = False

    signal_text = ", ".join(signal.label for signal in signals)
    reason = f"Automod score {score:.1f}: {signal_text}."
    return AutomodDecision(
        action,
        reason,
        score,
        tuple(signals),
        delete_message=delete_message,
        warn_points=warn_points,
        soft_strike=soft_strike,
    )

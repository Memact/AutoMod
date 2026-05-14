from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import unquote, urlparse


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]{1,120})\]\((https?://[^)]+)\)", re.IGNORECASE)
REPEATED_PUNCT_RE = re.compile(r"([!?])\1{4,}")
EXCESSIVE_MENTIONS_RE = re.compile(r"<@!?\d{15,25}>")

PROTECTED_GROUP_RE = re.compile(
    r"\b("
    r"black people|white people|asian people|jewish people|muslims?|christians?|hindus?|jews|"
    r"gay people|lesbians?|trans people|transgender people|disabled people|immigrants?|"
    r"women|men|girls|boys|refugees?|minorities|lgbtq|queer people"
    r")\b",
    re.IGNORECASE,
)

TARGETED_HATE_RE = re.compile(
    r"\b("
    r"kill|exterminate|wipe out|gas|hang|burn|deport|remove|eradicate|cleanse"
    r")\s+(all\s+)?("
    r"black people|white people|asian people|jewish people|muslims?|christians?|hindus?|jews|"
    r"gay people|lesbians?|trans people|transgender people|disabled people|immigrants?|"
    r"women|men|girls|boys|refugees?|minorities|lgbtq|queer people"
    r")\b",
    re.IGNORECASE,
)

DEHUMANIZATION_RE = re.compile(
    r"\b("
    r"black people|white people|asian people|jewish people|muslims?|christians?|hindus?|jews|"
    r"gay people|lesbians?|trans people|transgender people|disabled people|immigrants?|"
    r"women|men|girls|boys|refugees?|minorities|lgbtq|queer people"
    r")\s+(are|r|is)\s+("
    r"animals|vermin|subhuman|parasites|disease|filth|scum"
    r")\b",
    re.IGNORECASE,
)

HARASSMENT_RE = re.compile(
    r"\b("
    r"kys|kill yourself|end yourself|nobody wants you|go die|die already"
    r")\b",
    re.IGNORECASE,
)

SCAM_TOKENS = {
    "free nitro",
    "discord gifts",
    "discord-gifts",
    "steamcommunity",
    "steamcomrnunity",
    "walletconnect",
    "airdrop",
    "claim reward",
    "claim prize",
    "verify wallet",
}

KNOWN_SAFE_HOSTS = {
    "discord.com",
    "discord.gg",
    "youtube.com",
    "youtu.be",
    "x.com",
    "twitter.com",
    "bsky.app",
    "github.com",
    "tenor.com",
    "giphy.com",
    "imgur.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
}


@dataclass(frozen=True)
class SentinelSignal:
    category: str
    label: str
    severity: int
    confidence: float


@dataclass(frozen=True)
class SentinelDecision:
    severity: int
    confidence: float
    summary: str
    signals: tuple[SentinelSignal, ...]
    content_hash: str
    excerpt: str

    @property
    def should_alert(self) -> bool:
        return self.severity >= 4 and self.confidence >= 0.72


def normalize_text(content: str) -> str:
    text = ZERO_WIDTH_RE.sub("", content)
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(text.casefold().split())


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest()[:16]


def clip_excerpt(content: str, limit: int = 280) -> str:
    text = " ".join(content.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def extract_urls(content: str) -> list[str]:
    urls = URL_RE.findall(content)
    for label, url in MARKDOWN_LINK_RE.findall(content):
        urls.append(url)
        urls.extend(URL_RE.findall(label))
    return [url.rstrip(".,!?;:") for url in urls]


def normalized_host(url: str) -> str:
    host = urlparse(url).netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_known_safe_host(host: str) -> bool:
    return host in KNOWN_SAFE_HOSTS or any(host.endswith(f".{safe}") for safe in KNOWN_SAFE_HOSTS)


def looks_like_homoglyph_domain(host: str) -> bool:
    return any(token in host for token in ("rn", "vv", "0", "1")) and any(
        brand in host for brand in ("discord", "steam", "nitro", "wallet")
    )


def evaluate_message(
    *,
    content: str,
    mention_count: int,
    account_age_hours: float,
    joined_age_hours: float,
    raid_mode: bool,
) -> SentinelDecision | None:
    normalized = normalize_text(content)
    if not normalized:
        return None

    signals: list[SentinelSignal] = []

    if TARGETED_HATE_RE.search(normalized):
        signals.append(SentinelSignal("hate_speech", "violent targeting of a protected group", 5, 0.95))
    elif DEHUMANIZATION_RE.search(normalized):
        signals.append(SentinelSignal("hate_speech", "dehumanizing protected-group language", 5, 0.9))
    elif PROTECTED_GROUP_RE.search(normalized) and HARASSMENT_RE.search(normalized):
        signals.append(SentinelSignal("hate_speech", "protected-group harassment context", 4, 0.78))

    if HARASSMENT_RE.search(normalized):
        signals.append(SentinelSignal("harassment", "self-harm or death harassment phrase", 4, 0.82))

    urls = extract_urls(content)
    if urls:
        unsafe_hosts = [normalized_host(url) for url in urls if not is_known_safe_host(normalized_host(url))]
        if unsafe_hosts and any(token in normalized for token in SCAM_TOKENS):
            signals.append(SentinelSignal("scam", "suspicious link plus scam language", 5, 0.9))
        if any(looks_like_homoglyph_domain(host) for host in unsafe_hosts):
            signals.append(SentinelSignal("scam", "lookalike or obfuscated domain", 4, 0.82))
        for label, url in MARKDOWN_LINK_RE.findall(content):
            label_hosts = [normalized_host(label_url) for label_url in URL_RE.findall(label)]
            target_host = normalized_host(url)
            if label_hosts and any(label_host != target_host for label_host in label_hosts):
                signals.append(SentinelSignal("scam", "misleading markdown link target", 4, 0.84))
                break

    if mention_count >= 5 or len(EXCESSIVE_MENTIONS_RE.findall(content)) >= 5:
        signals.append(SentinelSignal("raid", "message mentions many users", 4, 0.78))

    if REPEATED_PUNCT_RE.search(content) and (account_age_hours < 72 or joined_age_hours < 24):
        signals.append(SentinelSignal("raid", "new-member burst pattern", 3, 0.64))

    if not signals:
        return None

    if raid_mode:
        signals.append(SentinelSignal("context", "raid mode is enabled", 1, 0.7))
    if account_age_hours < 24 or joined_age_hours < 1:
        signals.append(SentinelSignal("context", "very new account/member", 1, 0.72))

    severity = min(5, max(signal.severity for signal in signals) + (1 if len(signals) >= 3 else 0))
    confidence = min(0.99, sum(signal.confidence for signal in signals) / len(signals) + (0.04 if len(signals) >= 2 else 0.0))
    summary = "; ".join(signal.label for signal in signals if signal.category != "context")
    return SentinelDecision(
        severity=severity,
        confidence=confidence,
        summary=summary or "suspicious context",
        signals=tuple(signals),
        content_hash=content_hash(content),
        excerpt=clip_excerpt(content),
    )

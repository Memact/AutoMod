from __future__ import annotations

from dataclasses import dataclass
import re

from utils.sentinel import (
    MARKDOWN_LINK_RE,
    SCAM_TOKENS,
    URL_RE,
    SentinelSignal,
    clip_excerpt,
    content_hash,
    evaluate_message,
    extract_urls,
    is_known_safe_host,
    looks_like_homoglyph_domain,
    normalize_text,
    normalized_host,
)


INVITE_RE = re.compile(r"\b(?:discord\.gg|discord(?:app)?\.com/invite)/([a-z0-9-]{2,32})\b", re.IGNORECASE)
ALLOWED_INVITE_CODES = {"wjkdewugy5"}
PROMO_LINK_RE = re.compile(
    r"\b("
    r"join my|join our|check out my|buy now|cheap|discount|giveaway|casino|crypto|airdrop|"
    r"free followers|free robux|free nitro|onlyfans|telegram|whatsapp"
    r")\b",
    re.IGNORECASE,
)


def _letter_pattern(word: str) -> str:
    pieces = []
    for char in word:
        if char == " ":
            pieces.append(r"[\W_]+")
            continue
        if char == "a":
            piece = r"[a@4]+"
        elif char == "e":
            piece = r"[e3]+"
        elif char == "i":
            piece = r"[i1!|]+"
        elif char == "o":
            piece = r"[o0]+"
        elif char == "s":
            piece = r"[s$5]+"
        elif char == "t":
            piece = r"[t7]+"
        else:
            piece = re.escape(char) + "+"
        pieces.append(piece)
    return r"[\W_]*".join(pieces)


BLOCKED_PROFANITY = (
    "fuck",
    "fucker",
    "fucking",
    "motherfucker",
    "shit",
    "bullshit",
    "bitch",
    "asshole",
    "bastard",
    "dickhead",
    "dick",
    "pussy",
    "cunt",
    "whore",
    "slut",
    "douchebag",
)

OFFENSIVE_REFERENCES = (
    "adolf hitler",
    "hitler",
    "nazi",
    "nazis",
    "joseph stalin",
    "stalin",
    "mussolini",
    "pol pot",
    "saddam hussein",
    "osama bin laden",
)

PROFANITY_PATTERNS = tuple(
    re.compile(rf"(?<![a-z0-9]){_letter_pattern(term)}(?:s|ed|ing|er|ers)?(?![a-z0-9])", re.IGNORECASE)
    for term in BLOCKED_PROFANITY
)
OFFENSIVE_REFERENCE_PATTERNS = tuple(
    re.compile(rf"(?<![a-z0-9]){_letter_pattern(term)}(?![a-z0-9])", re.IGNORECASE)
    for term in OFFENSIVE_REFERENCES
)


@dataclass(frozen=True)
class GuardDecision:
    category: str
    action: str
    severity: int
    confidence: float
    summary: str
    signals: tuple[SentinelSignal, ...]
    content_hash: str
    excerpt: str

    @property
    def should_delete(self) -> bool:
        return self.action == "delete"


def _append_unique(signals: list[SentinelSignal], signal: SentinelSignal) -> None:
    key = (signal.category, signal.label)
    if any((existing.category, existing.label) == key for existing in signals):
        return
    signals.append(signal)


def _category(signals: list[SentinelSignal]) -> str:
    ordered = sorted(
        (signal for signal in signals if signal.category != "context"),
        key=lambda signal: (signal.severity, signal.confidence),
        reverse=True,
    )
    return ordered[0].category if ordered else "context"


def _contains_blocked_profanity(normalized: str) -> bool:
    return any(pattern.search(normalized) for pattern in PROFANITY_PATTERNS)


def _contains_offensive_reference(normalized: str) -> bool:
    return any(pattern.search(normalized) for pattern in OFFENSIVE_REFERENCE_PATTERNS)


def _scam_and_link_signals(content: str, normalized: str, *, is_bot_actor: bool) -> list[SentinelSignal]:
    signals: list[SentinelSignal] = []
    urls = extract_urls(content)
    if not urls:
        return signals

    hosts = [normalized_host(url) for url in urls]
    unsafe_hosts = [host for host in hosts if not is_known_safe_host(host)]
    if unsafe_hosts and any(token in normalized for token in SCAM_TOKENS):
        signals.append(SentinelSignal("scam", "suspicious link plus scam language", 5, 0.93))
    if any(looks_like_homoglyph_domain(host) for host in unsafe_hosts):
        signals.append(SentinelSignal("scam", "lookalike or obfuscated domain", 4, 0.86))
    for label, url in MARKDOWN_LINK_RE.findall(content):
        label_hosts = [normalized_host(label_url) for label_url in URL_RE.findall(label)]
        target_host = normalized_host(url)
        if label_hosts and any(label_host != target_host for label_host in label_hosts):
            signals.append(SentinelSignal("scam", "misleading markdown link target", 4, 0.88))
            break

    for match in INVITE_RE.finditer(normalized):
        invite_code = match.group(1).casefold()
        if invite_code not in ALLOWED_INVITE_CODES:
            confidence = 0.9 if is_bot_actor else 0.78
            severity = 4 if is_bot_actor else 3
            signals.append(SentinelSignal("promo", "unsolicited Discord invite", severity, confidence))
            break

    if PROMO_LINK_RE.search(normalized):
        confidence = 0.9 if is_bot_actor else 0.76
        severity = 4 if is_bot_actor else 3
        signals.append(SentinelSignal("promo", "promotional link pattern", severity, confidence))

    return signals


def evaluate_guard_message(
    *,
    content: str,
    mention_count: int,
    account_age_hours: float,
    joined_age_hours: float,
    raid_mode: bool,
    is_bot_actor: bool,
    is_staff_actor: bool,
    staff_only_channel: bool,
    recent_message_count: int,
    duplicate_message_count: int,
) -> GuardDecision | None:
    normalized = normalize_text(content)
    if not normalized:
        return None

    signals: list[SentinelSignal] = []
    silent_decision = evaluate_message(
        content=content,
        mention_count=mention_count,
        account_age_hours=account_age_hours,
        joined_age_hours=joined_age_hours,
        raid_mode=raid_mode,
    )
    if silent_decision is not None:
        for signal in silent_decision.signals:
            _append_unique(signals, signal)

    if _contains_blocked_profanity(normalized):
        signals.append(SentinelSignal("profanity", "blocked profanity", 2, 0.92))

    if _contains_offensive_reference(normalized):
        signals.append(SentinelSignal("offensive_reference", "dictator or extremist reference", 3, 0.86))

    for signal in _scam_and_link_signals(content, normalized, is_bot_actor=is_bot_actor):
        _append_unique(signals, signal)

    if recent_message_count >= (3 if is_bot_actor else 6):
        signals.append(SentinelSignal("spam", "message flood velocity", 4 if is_bot_actor else 3, 0.82))
    if duplicate_message_count >= (2 if is_bot_actor else 3):
        signals.append(SentinelSignal("spam", "repeated duplicate message", 4 if is_bot_actor else 3, 0.84))

    if not signals:
        return None

    if is_bot_actor:
        signals.append(SentinelSignal("context", "bot or installed app message", 1, 0.75))
    if staff_only_channel:
        signals.append(SentinelSignal("context", "staff-only channel", 1, 0.68))

    severity = min(5, max(signal.severity for signal in signals) + (1 if len(signals) >= 4 else 0))
    confidence = min(0.99, sum(signal.confidence for signal in signals) / len(signals) + (0.04 if len(signals) >= 2 else 0.0))
    category = _category(signals)

    if staff_only_channel and is_staff_actor and category in {"profanity", "offensive_reference", "promo", "spam"} and severity < 4:
        return None

    action = "delete" if severity >= (4 if staff_only_channel and is_staff_actor else 2) else "observe"
    summary = "; ".join(signal.label for signal in signals if signal.category != "context") or "policy signal"
    return GuardDecision(
        category=category,
        action=action,
        severity=severity,
        confidence=confidence,
        summary=summary,
        signals=tuple(signals),
        content_hash=content_hash(content),
        excerpt=clip_excerpt(content),
    )

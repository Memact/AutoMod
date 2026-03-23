from __future__ import annotations

from collections.abc import Iterable
import json
import re
from urllib.request import Request, urlopen


DATASET_PRESETS = {
    "strong_en": {
        "label": "Strong English",
        "sources": [
            {"url": "https://raw.githubusercontent.com/LDNOOBW/naughty-words-js/master/en.json", "format": "json_list"},
            {"url": "https://raw.githubusercontent.com/censor-text/profanity-list/main/list/en.txt", "format": "newline_text"},
            {"url": "https://raw.githubusercontent.com/coffee-and-fun/google-profanity-words/main/data/en.txt", "format": "newline_text"},
        ],
    },
    "strong_en_hi": {
        "label": "Strong English + Hindi",
        "sources": [
            {"url": "https://raw.githubusercontent.com/LDNOOBW/naughty-words-js/master/en.json", "format": "json_list"},
            {"url": "https://raw.githubusercontent.com/censor-text/profanity-list/main/list/en.txt", "format": "newline_text"},
            {"url": "https://raw.githubusercontent.com/coffee-and-fun/google-profanity-words/main/data/en.txt", "format": "newline_text"},
            {"url": "https://raw.githubusercontent.com/LDNOOBW/naughty-words-js/master/hi.json", "format": "json_list"},
        ],
    },
    "ldnoobw_en": {
        "label": "LDNOOBW English",
        "sources": [
            {"url": "https://raw.githubusercontent.com/LDNOOBW/naughty-words-js/master/en.json", "format": "json_list"},
        ],
    },
    "ldnoobw_hi": {
        "label": "LDNOOBW Hindi",
        "sources": [
            {"url": "https://raw.githubusercontent.com/LDNOOBW/naughty-words-js/master/hi.json", "format": "json_list"},
        ],
    },
    "ldnoobw_en_hi": {
        "label": "LDNOOBW English + Hindi",
        "sources": [
            {"url": "https://raw.githubusercontent.com/LDNOOBW/naughty-words-js/master/en.json", "format": "json_list"},
            {"url": "https://raw.githubusercontent.com/LDNOOBW/naughty-words-js/master/hi.json", "format": "json_list"},
        ],
    },
}

SIMPLE_TERM_RE = re.compile(r"^[\w'-]+$", re.UNICODE)
LETTER_SUBSTITUTIONS = {
    "a": "[a4@]",
    "b": "[b8]",
    "e": "[e3]",
    "g": "[g69]",
    "i": "[i1!|l]",
    "l": "[l1!|i]",
    "o": "[o0]",
    "s": "[s5$]",
    "t": "[t7+]",
    "z": "[z2]",
}
INTER_CHAR_SEPARATORS = r"[\W_]*"
INTER_WORD_SEPARATORS = r"[\W_]+"


def normalize_blocked_term(term: str) -> str | None:
    normalized = " ".join(term.strip().casefold().split())
    if not normalized:
        return None
    if normalized.startswith("#") or normalized.startswith("//"):
        return None
    if len(normalized) > 80:
        return None
    return normalized


def normalize_blocked_terms(terms: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    normalized_terms: list[str] = []
    for term in terms:
        normalized = normalize_blocked_term(term)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        normalized_terms.append(normalized)
    return normalized_terms


def compile_blocked_term_pattern(term: str) -> re.Pattern[str]:
    if SIMPLE_TERM_RE.fullmatch(term) or " " in term:
        words = []
        for word in term.split():
            characters = []
            for char in word.casefold():
                characters.append(LETTER_SUBSTITUTIONS.get(char, re.escape(char)))
            words.append(INTER_CHAR_SEPARATORS.join(characters))
        pattern = INTER_WORD_SEPARATORS.join(words)
        return re.compile(rf"(?<!\w){pattern}(?!\w)", re.IGNORECASE)
    escaped = re.escape(term)
    return re.compile(escaped, re.IGNORECASE)


def parse_dataset_terms(payload_text: str, data_format: str) -> list[str]:
    if data_format == "json_list":
        payload = json.loads(payload_text)
        if not isinstance(payload, list):
            raise ValueError("The blocked-word dataset returned an unexpected format.")
        return [str(item) for item in payload]

    if data_format == "newline_text":
        terms = [line.strip() for line in payload_text.splitlines() if line.strip()]
        if not terms:
            raise ValueError("The blocked-word dataset did not contain any terms.")
        return terms

    raise ValueError("Unsupported blocked-word dataset format.")


def fetch_dataset_terms_sync(preset: str) -> list[str]:
    dataset = DATASET_PRESETS.get(preset)
    if dataset is None:
        raise ValueError("Unknown blocked-word dataset.")
    all_terms: list[str] = []
    for source in dataset["sources"]:
        url = source["url"]
        request = Request(
            url,
            headers={"User-Agent": "MemactAutoMod/1.0"},
        )
        with urlopen(request, timeout=20) as response:
            payload_text = response.read().decode("utf-8")
        all_terms.extend(parse_dataset_terms(payload_text, source["format"]))
    return normalize_blocked_terms(all_terms)

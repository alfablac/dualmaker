from __future__ import annotations

import re

_ALIASES = {
    "alb": "sq",
    "sqi": "sq",
    "ara": "ar",
    "arm": "hy",
    "hye": "hy",
    "baq": "eu",
    "eus": "eu",
    "ben": "bn",
    "bos": "bs",
    "bul": "bg",
    "cat": "ca",
    "chi": "zh",
    "zho": "zh",
    "cze": "cs",
    "ces": "cs",
    "dan": "da",
    "dut": "nl",
    "nld": "nl",
    "en": "en",
    "eng": "en",
    "en-us": "en-US",
    "en-gb": "en-GB",
    "est": "et",
    "fin": "fi",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "gre": "el",
    "ell": "el",
    "heb": "he",
    "hin": "hi",
    "hrv": "hr",
    "hun": "hu",
    "ice": "is",
    "isl": "is",
    "ind": "id",
    "ita": "it",
    "jpn": "ja",
    "kor": "ko",
    "lav": "lv",
    "lit": "lt",
    "mac": "mk",
    "mkd": "mk",
    "may": "ms",
    "msa": "ms",
    "nor": "no",
    "per": "fa",
    "fas": "fa",
    "pol": "pl",
    "pt": "pt",
    "por": "pt",
    "pob": "pt-BR",
    "pb": "pt-BR",
    "pt-br": "pt-BR",
    "pt-bra": "pt-BR",
    "pt-pt": "pt-PT",
    "rum": "ro",
    "ron": "ro",
    "rus": "ru",
    "slo": "sk",
    "slk": "sk",
    "slv": "sl",
    "spa": "es",
    "srp": "sr",
    "swe": "sv",
    "tha": "th",
    "tur": "tr",
    "ukr": "uk",
    "urd": "ur",
    "vie": "vi",
    "wel": "cy",
    "cym": "cy",
    "und": "und",
    "": "und",
}


def normalize_language(value: str | None) -> str:
    raw = re.sub(r"_", "-", (value or "und").strip()).lower()
    if raw in _ALIASES:
        return _ALIASES[raw]
    parts = raw.split("-")
    base = _ALIASES.get(parts[0], parts[0])
    if len(parts) > 1:
        normalized_parts = [base]
        for part in parts[1:]:
            if len(part) == 4 and part.isalpha():
                normalized_parts.append(part.title())
            elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
                normalized_parts.append(part.upper())
            else:
                normalized_parts.append(part)
        return "-".join(normalized_parts)
    return base or "und"


def base_language(value: str | None) -> str:
    return normalize_language(value).split("-", 1)[0]


def is_portuguese(value: str | None) -> bool:
    return base_language(value) == "pt"


def is_english(value: str | None) -> bool:
    return base_language(value) == "en"


def languages_match(left: str | None, right: str | None) -> bool:
    left_normalized = normalize_language(left)
    right_normalized = normalize_language(right)
    if "und" in (left_normalized, right_normalized):
        return False
    return left_normalized == right_normalized or base_language(left) == base_language(right)

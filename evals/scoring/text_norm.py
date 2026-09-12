"""Text normalization for speech and intent evaluation in iShop (Drake).

Enforces:
- T-25: Unicode and diacritics preserved under primary ishop-unicode-v1;
        transparent organizer-compatible supplementary normalization.
"""

from __future__ import annotations

import re
import unicodedata

PUNCTUATION_PATTERN = re.compile(r"[\.,!?:;\"'()\[\]{}–—\-]+")
COMBINING_CHAR_PATTERN = re.compile(r"[\u0300-\u036f]")


def normalize_unicode_v1(text: str) -> str:
    """Primary text normalization (ishop-unicode-v1).

    Preserves tone marks and diacritics (e.g. Yoruba ẹ, ọ, ṣ, á, à)
    using Unicode NFC normalization and case folding.
    """
    if not text:
        return ""
    # 1. Unicode NFC form
    nfc_text = unicodedata.normalize("NFC", text)
    # 2. Case folding (handles complex Unicode case pairs)
    folded = nfc_text.casefold()
    # 3. Strip declared punctuation
    clean_punct = PUNCTUATION_PATTERN.sub(" ", folded)
    # 4. Collapse and trim whitespace
    return " ".join(clean_punct.split())


def normalize_organizer_compatible(text: str) -> str:
    """Supplementary normalization mimicking baseline organizers.

    Strips combining diacritics and converts non-ASCII characters.
    """
    if not text:
        return ""
    # 1. Decompose to NFD to separate base characters and combining marks
    nfd_text = unicodedata.normalize("NFD", text)
    # 2. Remove combining diacritics
    stripped = COMBINING_CHAR_PATTERN.sub("", nfd_text)
    # 3. Lowercase and strip punctuation
    folded = stripped.lower()
    clean_punct = PUNCTUATION_PATTERN.sub(" ", folded)
    return " ".join(clean_punct.split())


def normalize_text(text: str, version: str = "ishop-unicode-v1") -> str:
    if version == "ishop-unicode-v1":
        return normalize_unicode_v1(text)
    elif version == "organizer-compatible":
        return normalize_organizer_compatible(text)
    else:
        raise ValueError(f"Unknown text normalization version: '{version}'")

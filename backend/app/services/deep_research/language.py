"""Domain-general output-language resolution + consistency checking for Deep Research.

The deep research engine is universal (any domain/any language). Workers and the
final synthesizer must produce a SINGLE output language so reports never come back
half-Chinese / half-English. This module:

- resolves the target language from an explicit `output_language` override or, failing
  that, by detecting the dominant script of the research question;
- exposes a human-readable label to inject into prompts;
- checks a finished report for *paragraph-level* language mixing. Paragraph-level (not
  token-level) is deliberate: a correct Chinese report legitimately contains inline
  English entity names (BlackRock, SEC, BUIDL) and numbers — those must NOT be flagged.
  Only whole paragraphs written in the wrong language count as a violation.
"""

from __future__ import annotations

import re
from typing import Any

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA_RE = re.compile(r"[぀-ゟ゠-ヿ]")
_HANGUL_RE = re.compile(r"[가-힣]")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")

# Markup stripped before language detection on a paragraph.
_SRC_TOKEN_RE = re.compile(r"`?src_[a-zA-Z0-9_]+`?")
_FOOTNOTE_RE = re.compile(r"\[\^?\d+\]")
_URL_RE = re.compile(r"https?://\S+")
_MD_MARKUP_RE = re.compile(r"[#>*`|_\-]+")

_LANGUAGE_LABELS: dict[str, str] = {
    "zh": "Chinese (简体中文)",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "ru": "Russian (Русский)",
    "en": "English",
}

# Explicit-override aliases → canonical code.
_OVERRIDE_ALIASES: dict[str, str] = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "chinese": "zh",
    "中文": "zh",
    "简体中文": "zh",
    "简体": "zh",
    "mandarin": "zh",
    "en": "en",
    "english": "en",
    "英文": "en",
    "英语": "en",
    "ja": "ja",
    "japanese": "ja",
    "日本語": "ja",
    "日语": "ja",
    "ko": "ko",
    "korean": "ko",
    "한국어": "ko",
    "韩语": "ko",
    "ru": "ru",
    "russian": "ru",
    "русский": "ru",
}


def detect_language_code(text: str) -> str:
    """Best-effort dominant-script detection. Defaults to English."""
    if not text:
        return "en"
    cjk = len(_CJK_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    hangul = len(_HANGUL_RE.findall(text))
    cyr = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_WORD_RE.findall(text))

    # Kana/Hangul/Cyrillic are unambiguous scripts; a meaningful amount wins.
    if kana >= 2 and kana >= cjk:
        return "ja"
    if hangul >= 2:
        return "ko"
    if cyr >= 4 and cyr >= latin:
        return "ru"
    # CJK present at all and not clearly Japanese → treat as Chinese.
    if cjk >= 1 and cjk * 4 >= latin:
        return "zh"
    return "en"


def language_label(code: str) -> str:
    return _LANGUAGE_LABELS.get(code, "English")


def resolve_output_language_code(request: Any) -> str:
    override = str(getattr(request, "output_language", "") or "").strip()
    if override:
        canonical = _OVERRIDE_ALIASES.get(override.casefold())
        if canonical:
            return canonical
        return detect_language_code(override)
    return detect_language_code(str(getattr(request, "question", "") or ""))


def resolve_output_language_label(request: Any) -> str:
    return language_label(resolve_output_language_code(request))


def _strip_for_language(paragraph: str) -> str:
    body = _URL_RE.sub(" ", paragraph)
    body = _SRC_TOKEN_RE.sub(" ", body)
    body = _FOOTNOTE_RE.sub(" ", body)
    body = _MD_MARKUP_RE.sub(" ", body)
    return body


def _paragraph_is_foreign(paragraph: str, target: str) -> bool:
    """True only when the WHOLE paragraph is in a language other than the target.

    Inline foreign tokens (entity names, tickers, numbers) inside an otherwise
    on-target paragraph never trip this — we require the paragraph to be wholly
    in the wrong script.
    """
    body = _strip_for_language(paragraph)
    cjk = len(_CJK_RE.findall(body))
    kana = len(_KANA_RE.findall(body))
    hangul = len(_HANGUL_RE.findall(body))
    cyr = len(_CYRILLIC_RE.findall(body))
    latin_words = len(_LATIN_WORD_RE.findall(body))

    if target == "zh":
        # A full English/Latin paragraph with no CJK at all is foreign.
        return cjk == 0 and kana == 0 and latin_words >= 12
    if target == "en":
        # A real CJK/Kana/Hangul sentence inside an English report is foreign.
        return (cjk + kana + hangul) >= 8
    if target == "ja":
        return kana == 0 and cjk == 0 and latin_words >= 12
    if target == "ko":
        return hangul == 0 and latin_words >= 12
    if target == "ru":
        return cyr == 0 and latin_words >= 12
    return False


def paragraph_language_consistency(report: str, target: str) -> tuple[bool, int]:
    """Return (ok, foreign_paragraph_count).

    ok is False when >= 2 whole paragraphs are written in a non-target language —
    the signature of a stitched-from-multiple-workers report. A single stray block
    is tolerated to avoid false positives on, e.g., a quoted source line.
    """
    if not report:
        return True, 0
    foreign = 0
    for block in re.split(r"\n\s*\n", report):
        stripped = _strip_for_language(block).strip()
        # Skip short blocks, headings, and pure-markup/ledger lines.
        if len(stripped) < 40:
            continue
        if _paragraph_is_foreign(block, target):
            foreign += 1
    return foreign < 2, foreign

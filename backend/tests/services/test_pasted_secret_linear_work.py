"""Preserve paste-policy bytes while bounding string assembly and regex work."""

from itertools import product
from random import Random
import re
from string import ascii_letters, digits, punctuation, whitespace

import pytest

from app.services.privacy_layer import PrivacyLayer


_OLD_VERSION_RE = re.compile(r"v?\d+(?:\.\d+)+[A-Za-z0-9.-]*")


def _reference_redaction(text, spans):
    for start, end in reversed(spans):
        text = text[:start] + "[REDACTED_SECRET]" + text[end:]
    return text


@pytest.mark.parametrize("count", [1, 64, 4_096])
@pytest.mark.parametrize("separator", [" ", "\n普通说明🙂\n"])
def test_many_spans_match_the_original_slice_reference(count, separator):
    text = "保留🙂 " + ("secret=Aa1aaaaa" + separator) * count + " end."
    spans = PrivacyLayer._credential_spans(text)
    assert len(spans) == count

    result = PrivacyLayer.redact_pasted_credentials(text)

    assert result.value.encode("utf-8") == _reference_redaction(text, spans).encode("utf-8")
    assert result.redacted_count == count


@pytest.mark.parametrize("overlap", [False, True], ids=["adjacent", "overlapping"])
def test_adjacent_and_overlapping_matches_keep_the_original_merge_and_bytes(overlap):
    class PatternLayer(PrivacyLayer):
        _CREDENTIAL_PATTERNS = (re.compile(r"AB\d+"), re.compile(r"CD\d+")) + (
            (re.compile(r"123CD"),) if overlap else ()
        )

    text = "AB123CD456|" * 1_024
    spans = PatternLayer._credential_spans(text)
    expected_spans = [
        span
        for start in range(0, len(text), 11)
        for span in ([(start, start + 10)] if overlap else [(start, start + 5), (start + 5, start + 10)])
    ]
    assert spans == expected_spans

    result = PatternLayer.redact_pasted_credentials(text)

    assert result.value.encode("utf-8") == _reference_redaction(text, spans).encode("utf-8")
    assert result.redacted_count == len(spans)
    assert result.value == ("[REDACTED_SECRET]" * (1 if overlap else 2) + "|") * 1_024


class _SliceCountedText(str):
    """Count sliced characters across concatenations, not elapsed wall time."""

    def __new__(cls, value, copied):
        instance = super().__new__(cls, value)
        instance.copied = copied
        return instance

    def __getitem__(self, key):
        result = super().__getitem__(key)
        if isinstance(key, slice):
            self.copied[0] += len(result)
            return type(self)(result, self.copied)
        return result

    def __add__(self, other):
        return type(self)(super().__add__(other), self.copied)


def test_many_spans_slice_at_most_the_original_string_length():
    copied = [0]
    text = _SliceCountedText("secret=Aa1aaaaa " * 1_024, copied)

    result = PrivacyLayer.redact_pasted_credentials(text)

    assert result.value == "secret=[REDACTED_SECRET] " * 1_024
    assert result.redacted_count == 1_024
    assert copied[0] <= len(text)


@pytest.mark.parametrize("content", ["", "普通文字🙂", "secret: v1.2.3-beta4"])
def test_no_spans_and_nested_structural_fields_stay_unchanged(content):
    payload = {
        "content": content,
        "parts": ({"text": "secret=Aa1aaaaa", "path": "secret=Aa1aaaaa"},),
        "input_id": "secret=Aa1aaaaa",
        "number": 12,
        "empty": None,
    }

    result = PrivacyLayer.redact_pasted_credentials(payload)

    assert result.value == {
        **payload,
        "parts": [{"text": "secret=[REDACTED_SECRET]", "path": "secret=Aa1aaaaa"}],
    }
    assert result.redacted_count == 1
    assert payload["parts"][0]["text"] == "secret=Aa1aaaaa"


def _version_values():
    # Exhaustive short strings include invalid prefixes, punctuation and Unicode digits.
    for length in range(6):
        for chars in product("v01.a-!١", repeat=length):
            yield "".join(chars)
    for prefix in ("", "v", "V"):
        for count in range(1, 4):
            for components in product(("0", "1", "01", "123", "١", "１２"), repeat=count):
                for suffix in ("", ".", "..", ".0", ".1a", "0", "001", "a", "Z9", "-", "-rc1", "!", "\n"):
                    yield prefix + ".".join(components) + suffix
    for count in (8, 64, 512):
        for prefix in ("", "v"):
            for suffix in ("", "!", ".", "..", ".1", ".1!", "0", "001", "a", "a1", "-", "-rc1", "-.", "-!"):
                yield prefix + "1." * count + "1" + suffix


def test_version_regex_matches_exactly_the_old_language_on_generated_values():
    for value in set(_version_values()):
        assert bool(PrivacyLayer._VERSION_RE.fullmatch(value)) == bool(_OLD_VERSION_RE.fullmatch(value)), repr(value)


@pytest.mark.parametrize("count", [8, 64, 512])
@pytest.mark.parametrize("suffix", ["-rc1", ".a1", "0a1", "!a1"])
def test_long_versions_and_near_versions_keep_original_redaction_bytes(count, suffix):
    value = "1." * count + "1" + suffix
    # Each value has letters and is long enough to be a credential unless it is a version.
    expected = value if _OLD_VERSION_RE.fullmatch(value) else "[REDACTED_SECRET]"

    result = PrivacyLayer.redact_pasted_credentials("password=" + value)

    assert result.value == "password=" + expected
    assert result.redacted_count == int(expected != value)


def test_version_pattern_keeps_its_possessive_group():
    assert PrivacyLayer._VERSION_RE.pattern == r"v?\d+(?:\.\d+)++[A-Za-z0-9.-]*"


_TOKEN_LEFT = r"(?<![A-Za-z0-9_])"
_TOKEN_RIGHT = r"(?![A-Za-z0-9_./+=-])"
_TOKEN_FORMS = (
    (
        "sk-", r"sk-[A-Za-z0-9_\-]{20,}",
        r"sk-(?:(?P<ok>[A-Za-z0-9_\-]{20,}+)(?![./+=])|[A-Za-z0-9_\-]*+)",
    ),
    (
        "xox", r"xox[baprs]-[A-Za-z0-9-]{20,}",
        r"xox[baprs]-(?:(?P<ok>[A-Za-z0-9-]{20,}+)(?![_./+=])|[A-Za-z0-9-]*+)",
    ),
    (
        "glpat-", r"glpat-[A-Za-z0-9_-]{20,}",
        r"glpat-(?:(?P<ok>[A-Za-z0-9_-]{20,}+)(?![./+=])|[A-Za-z0-9_-]*+)",
    ),
    (
        "eyJ", r"eyJ[A-Za-z0-9_+/=-]+\.[A-Za-z0-9_+/=-]+\.[A-Za-z0-9_+/=-]{16,}",
        r"eyJ(?:(?P<ok>[A-Za-z0-9_+/=-]++\.[A-Za-z0-9_+/=-]++\.[A-Za-z0-9_+/=-]{16,}+)(?!\.)|[A-Za-z0-9_+/=-]*+)",
    ),
)
_TOKEN_SEPARATORS = ("", *punctuation, *whitespace, "A", "0", "é", "中", "🙂")
_TOKEN_ENDINGS = ("", *ascii_letters, *digits, *punctuation, *whitespace, "é", "中", "🙂")
_REAL_TOKENS = ("sk-" + "Ab3_" * 8, "xoxb-" + "Cd4-" * 8, "glpat-" + "Ef5_" * 8, "eyJAb3+/=.Cd4-/=." + "Ef5_" * 4)
_TOKEN_RUNS = (("sk-",), ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"), ("glpat-",), ("eyJ-", "eyJ+", "eyJ/", "eyJ="))


def _token_pattern(prefix):
    return next(pattern for pattern in PrivacyLayer._CREDENTIAL_PATTERNS if pattern.pattern.startswith(_TOKEN_LEFT + prefix))


@pytest.mark.parametrize("prefix,old_body,linear_body", _TOKEN_FORMS)
def test_token_pattern_keeps_its_once_only_form(prefix, old_body, linear_body):
    assert _token_pattern(prefix).pattern == _TOKEN_LEFT + linear_body


def _generated_token_texts(index):
    token = _REAL_TOKENS[index]
    for unit in _TOKEN_RUNS[index]:
        for count in (0, 1, 4, 8, 32):
            for separator in _TOKEN_SEPARATORS:
                for ending in _TOKEN_ENDINGS:
                    yield unit * count + separator + token + ending
    # Pin the exact minimum lengths, missing JWT segments and extra dots too.
    for length in (0, 1, 15, 16, 19, 20, 21):
        prefix = ("sk-", "xoxb-", "glpat-", "eyJ")[index]
        for body in ("A" * length, "A." + "B" * length, "A.B." + "C" * length):
            for ending in _TOKEN_ENDINGS:
                yield prefix + body + ending


def _random_token_texts():
    rng = Random(0x50E)
    chunks = (*_REAL_TOKENS, *_TOKEN_SEPARATORS, *_TOKEN_ENDINGS, "ordinary text", "password=Ab3!Cd4ef")
    units = ("sk-", "xoxb-", "xoxp-", "glpat-", "eyJ-", "eyJ+", "eyJ/", "eyJ=")
    for _ in range(10_000):
        left = "".join(rng.choices(chunks, k=rng.randrange(5)))
        run = rng.choice(units) * rng.randrange(33)
        right = "".join(rng.choices(chunks, k=rng.randrange(5)))
        yield left + run + rng.choice(_TOKEN_SEPARATORS) + rng.choice(_REAL_TOKENS) + right


def _assert_token_spans_equal(prefix, old_body, texts):
    old_pattern = re.compile(_TOKEN_LEFT + old_body + _TOKEN_RIGHT)

    class TokenLayer(PrivacyLayer):
        _CREDENTIAL_PATTERNS = (_token_pattern(prefix),)

    for text in texts:
        expected = [match.span() for match in old_pattern.finditer(text)]
        assert TokenLayer._credential_spans(text) == expected, repr(text)


@pytest.mark.parametrize("index", range(len(_TOKEN_FORMS)), ids=[form[0] for form in _TOKEN_FORMS])
def test_token_spans_match_old_patterns_on_generated_boundaries(index):
    prefix, old_body, _ = _TOKEN_FORMS[index]
    _assert_token_spans_equal(prefix, old_body, _generated_token_texts(index))


@pytest.mark.parametrize("prefix,old_body,linear_body", _TOKEN_FORMS)
def test_token_spans_match_old_patterns_on_seeded_random_mixed_text(prefix, old_body, linear_body):
    _assert_token_spans_equal(prefix, old_body, _random_token_texts())

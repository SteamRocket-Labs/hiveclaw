"""Preserve paste-policy bytes while bounding string assembly and regex work."""

from itertools import product
import re

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
    for count in (8, 64, 512, 4_096, 8_000):
        for prefix in ("", "v"):
            for suffix in ("", "!", ".", "..", ".1", ".1!", "0", "001", "a", "a1", "-", "-rc1", "-.", "-!"):
                yield prefix + "1." * count + "1" + suffix


def test_version_regex_matches_exactly_the_old_language_on_generated_values():
    for value in set(_version_values()):
        assert bool(PrivacyLayer._VERSION_RE.fullmatch(value)) == bool(_OLD_VERSION_RE.fullmatch(value)), repr(value)


@pytest.mark.parametrize("count", [8, 512, 8_000])
@pytest.mark.parametrize("suffix", ["-rc1", ".a1", "0a1", "!a1"])
def test_long_versions_and_near_versions_keep_original_redaction_bytes(count, suffix):
    value = "1." * count + "1" + suffix
    # Each value has letters and is long enough to be a credential unless it is a version.
    expected = value if _OLD_VERSION_RE.fullmatch(value) else "[REDACTED_SECRET]"

    result = PrivacyLayer.redact_pasted_credentials("password=" + value)

    assert result.value == "password=" + expected
    assert result.redacted_count == int(expected != value)

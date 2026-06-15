"""Reusable validators for user-supplied display text.

These guard short identity/label fields (organization names, user names, team
and role names, descriptions, etc.) against stored-XSS / HTML injection by
rejecting markup and control characters before the value is persisted.

This is defense-in-depth: clients must still escape on render. It exists because
an attacker once created an organization whose ``name`` was an HTML/JS XSS
payload, which then sat in the database and would have fired anywhere the value
was rendered unescaped (emails, PDF exports, admin tooling, etc.).

Design notes:
- We reject ``<`` and ``>`` outright. These are never needed in a person/org
  name, title or short description, and blocking them defeats every tag-based
  XSS vector while still allowing normal punctuation (apostrophes, ampersands,
  accents, emoji, ...).
- We reject control characters (other than the whitespace explicitly allowed),
  which are sometimes used to smuggle payloads past naive filters.
- Use the ``Annotated`` aliases below in Pydantic v2 schemas, e.g.
  ``name: SafeLabel`` or ``description: OptionalSafeText``.
"""

import re
from typing import Annotated, Optional

from pydantic import AfterValidator

__all__ = [
    "MAX_LABEL_LENGTH",
    "MAX_TEXT_LENGTH",
    "validate_safe_text",
    "SafeLabel",
    "OptionalSafeLabel",
    "SafeText",
    "OptionalSafeText",
]

# Short identity labels: names, titles, job titles, etc.
MAX_LABEL_LENGTH = 255
# Longer free text: descriptions, bios, "about" blurbs.
MAX_TEXT_LENGTH = 2000

# Control characters with no place in display text. We deliberately exclude the
# whitespace characters callers may opt into (\t, \n, \r) and handle those
# separately so single-line fields can forbid line breaks.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_safe_text(
    value: Optional[str],
    *,
    max_length: int = MAX_LABEL_LENGTH,
    allow_newlines: bool = False,
    allow_empty: bool = False,
) -> Optional[str]:
    """Validate and normalize a user-supplied display string.

    Returns the stripped value, or ``None`` when ``value`` is ``None``.

    Raises:
        ValueError: if the value contains HTML markup characters, control
            characters, disallowed line breaks, or violates the length bounds.
            Pydantic turns this into a 422 with the offending field location.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string")

    stripped = value.strip()

    if not stripped and not allow_empty:
        raise ValueError("must not be empty")

    if len(stripped) > max_length:
        raise ValueError(f"must be at most {max_length} characters")

    if "<" in stripped or ">" in stripped:
        raise ValueError("must not contain '<' or '>' characters")

    if not allow_newlines and any(ch in stripped for ch in ("\n", "\r", "\t")):
        raise ValueError("must not contain line breaks")

    # When newlines are allowed, exclude them from the control-character check.
    candidate = (
        stripped.replace("\n", "").replace("\r", "").replace("\t", "")
        if allow_newlines
        else stripped
    )
    if _CONTROL_CHARS_RE.search(candidate):
        raise ValueError("must not contain control characters")

    return stripped


def _label(value: Optional[str]) -> Optional[str]:
    return validate_safe_text(value, max_length=MAX_LABEL_LENGTH, allow_newlines=False)


def _optional_label(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return validate_safe_text(
        value,
        max_length=MAX_LABEL_LENGTH,
        allow_newlines=False,
        allow_empty=True,
    )


def _text(value: Optional[str]) -> Optional[str]:
    return validate_safe_text(value, max_length=MAX_TEXT_LENGTH, allow_newlines=True)


def _optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return validate_safe_text(
        value,
        max_length=MAX_TEXT_LENGTH,
        allow_newlines=True,
        allow_empty=True,
    )


# Required, single-line label (e.g. organization name on create). Empty rejected.
SafeLabel = Annotated[str, AfterValidator(_label)]
# Optional single-line label (e.g. name on update / nullable profile fields).
OptionalSafeLabel = Annotated[Optional[str], AfterValidator(_optional_label)]
# Required multi-line free text.
SafeText = Annotated[str, AfterValidator(_text)]
# Optional multi-line free text (descriptions, bios, ...).
OptionalSafeText = Annotated[Optional[str], AfterValidator(_optional_text)]

import math

from orchestra.db.dao.api_message_dao import _without_nul
from orchestra.web.api.log.python2SQL.helpers import (
    MAX_TOKENS_PER_INPUT,
    _truncate_to_token_budget,
    count_tokens_per_utf_byte,
)


def test_truncate_keeps_text_within_budget():
    text = "word " * 40_000
    cut = _truncate_to_token_budget(text, MAX_TOKENS_PER_INPUT)
    assert math.ceil(count_tokens_per_utf_byte(cut)) <= MAX_TOKENS_PER_INPUT
    assert text.startswith(cut)


def test_truncate_leaves_small_text_unmodified():
    text = "a short text"
    assert _truncate_to_token_budget(text, MAX_TOKENS_PER_INPUT) == text


def test_without_nul_strips_nested_nul_characters():
    payload = {
        "message": "hel\x00lo",
        "tags": ["a\x00", {"k": "b\x00c"}],
        "count": 3,
    }
    assert _without_nul(payload) == {
        "message": "hello",
        "tags": ["a", {"k": "bc"}],
        "count": 3,
    }

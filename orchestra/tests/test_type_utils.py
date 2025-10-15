"""Tests for type_utils module - these don't require database."""

import pytest

from orchestra.web.api.log.utils.type_utils import (
    get_base_storage_type,
    get_display_type,
    is_image_type,
    normalize_type_string,
    parse_nested_type,
)


@pytest.mark.anyio
async def test_normalize_simple_types():
    """Test normalization of simple types."""
    assert normalize_type_string("int") == "int"
    assert normalize_type_string("INT") == "int"
    assert normalize_type_string("str") == "str"
    assert normalize_type_string("STR") == "str"
    assert normalize_type_string("float") == "float"
    assert normalize_type_string("FLOAT") == "float"


@pytest.mark.anyio
async def test_normalize_nested_list_types():
    """Test normalization of List types."""
    assert normalize_type_string("List[int]") == "List[int]"
    assert normalize_type_string("LIST[INT]") == "List[int]"
    assert normalize_type_string("list[int]") == "List[int]"
    assert normalize_type_string("List[str]") == "List[str]"
    assert normalize_type_string("List[float]") == "List[float]"
    assert normalize_type_string("List[image]") == "List[image]"


@pytest.mark.anyio
async def test_normalize_nested_dict_types():
    """Test normalization of Dict types."""
    assert normalize_type_string("Dict[str, float]") == "Dict[str, float]"
    assert normalize_type_string("DICT[STR, FLOAT]") == "Dict[str, float]"
    assert normalize_type_string("dict[str, float]") == "Dict[str, float]"
    assert normalize_type_string("Dict[str, int]") == "Dict[str, int]"


@pytest.mark.anyio
async def test_parse_simple_types():
    """Test parsing of simple types."""
    base, inner = parse_nested_type("int")
    assert base == "int"
    assert inner is None

    base, inner = parse_nested_type("str")
    assert base == "str"
    assert inner is None


@pytest.mark.anyio
async def test_parse_nested_list_types():
    """Test parsing of List types."""
    base, inner = parse_nested_type("List[int]")
    assert base == "List"
    assert inner == ["int"]

    base, inner = parse_nested_type("List[str]")
    assert base == "List"
    assert inner == ["str"]


@pytest.mark.anyio
async def test_parse_nested_dict_types():
    """Test parsing of Dict types."""
    base, inner = parse_nested_type("Dict[str, float]")
    assert base == "Dict"
    assert inner == ["str", "float"]

    base, inner = parse_nested_type("Dict[str, int]")
    assert base == "Dict"
    assert inner == ["str", "int"]


@pytest.mark.anyio
async def test_is_image_type():
    """Test image type detection."""
    assert is_image_type("image") is True
    assert is_image_type("Image") is True
    assert is_image_type("IMAGE") is True
    assert is_image_type("List[image]") is True
    assert is_image_type("List[Image]") is True

    assert is_image_type("str") is False
    assert is_image_type("int") is False
    assert is_image_type("List[int]") is False
    assert is_image_type("Dict[str, float]") is False


@pytest.mark.anyio
async def test_get_base_storage_type():
    """Test getting base storage type."""
    # Simple types return as-is
    assert get_base_storage_type("int") == "int"
    assert get_base_storage_type("str") == "str"
    assert get_base_storage_type("float") == "float"
    assert get_base_storage_type("image") == "image"

    # Nested types return the outer type in lowercase
    assert get_base_storage_type("List[int]") == "list"
    assert get_base_storage_type("List[str]") == "list"
    assert get_base_storage_type("List[image]") == "list"
    assert get_base_storage_type("Dict[str, float]") == "dict"
    assert get_base_storage_type("Dict[str, int]") == "dict"


@pytest.mark.anyio
async def test_get_display_type():
    """Test getting display type."""
    # When explicit type is provided, return it normalized
    assert get_display_type("List[int]", "list") == "List[int]"
    assert get_display_type("LIST[INT]", "list") == "List[int]"
    assert get_display_type("str", "str") == "str"
    assert get_display_type("STR", "str") == "str"

    # When no explicit type, return stored type
    assert get_display_type(None, "int") == "int"
    assert get_display_type(None, "list") == "list"

    # When no type info at all, return DEFAULT_FIELD_TYPE ("Any")
    from orchestra.web.api.log.utils.type_utils import DEFAULT_FIELD_TYPE

    assert get_display_type(None, None) == DEFAULT_FIELD_TYPE


@pytest.mark.anyio
async def test_normalize_with_spaces():
    """Test normalization handles spaces in nested types."""
    assert normalize_type_string("Dict[str,float]") == "Dict[str, float]"
    assert normalize_type_string("Dict[ str , float ]") == "Dict[str, float]"
    assert normalize_type_string("List[ int ]") == "List[int]"


@pytest.mark.anyio
async def test_case_insensitivity_comprehensive():
    """Test comprehensive case insensitivity."""
    test_cases = [
        ("list[int]", "List[int]"),
        ("LIST[INT]", "List[int]"),
        ("List[Int]", "List[int]"),
        ("LiSt[InT]", "List[int]"),
        ("dict[str, float]", "Dict[str, float]"),
        ("DICT[STR, FLOAT]", "Dict[str, float]"),
        ("Dict[Str, Float]", "Dict[str, float]"),
    ]

    for input_type, expected_output in test_cases:
        assert normalize_type_string(input_type) == expected_output

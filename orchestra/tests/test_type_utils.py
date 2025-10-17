"""Tests for type_utils module - these don't require database."""

import pytest

from orchestra.web.api.log.utils.type_utils import (
    get_base_storage_type,
    get_display_type,
    is_image_type,
    is_valid_field_type,
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
async def test_normalize_bare_containers_remain_lowercase():
    """Bare container names should remain lowercase when no type args are provided."""
    assert normalize_type_string("list") == "list"
    assert normalize_type_string("dict") == "dict"
    assert normalize_type_string("set") == "set"
    assert normalize_type_string("tuple") == "tuple"
    assert normalize_type_string("List") == "list"
    assert normalize_type_string("Dict") == "dict"
    assert normalize_type_string("Set") == "set"
    assert normalize_type_string("Tuple") == "tuple"


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


@pytest.mark.anyio
async def test_nested_deep():
    s = "List[Dict[str, List[int]]]"
    assert normalize_type_string(s) == "List[Dict[str, List[int]]]"


@pytest.mark.anyio
async def test_optional_and_allowed_union_with_none():
    # Optional[T] is allowed and normalizes to Union[T, NoneType]
    assert normalize_type_string("Optional[int]") == "Union[int, NoneType]"
    # Direct Union[T, NoneType] is allowed
    assert normalize_type_string("Union[str, NoneType]") == "Union[str, NoneType]"
    assert is_valid_field_type("Union[int, NoneType]") is True
    # Union with more than NoneType is invalid
    assert is_valid_field_type("Union[int, str]") is False
    # The | operator is not supported; should be invalid overall
    assert is_valid_field_type("int | str") is False


@pytest.mark.anyio
async def test_tuple_and_variadic_tuple():
    assert normalize_type_string("Tuple[int, float]") == "Tuple[int, float]"
    assert normalize_type_string("tuple[int, ...]") == "Tuple[int, ...]"


@pytest.mark.anyio
async def test_set_and_builtin_generics():
    assert normalize_type_string("set[int]") == "Set[int]"
    assert normalize_type_string("dict[str, list[int]]") == "Dict[str, List[int]]"


@pytest.mark.anyio
async def test_parse_nested_type_preserves_inner_commas():
    base, inner = parse_nested_type("Dict[str, List[int]]")
    assert base == "Dict"
    assert inner == ["str", "List[int]"]


@pytest.mark.anyio
async def test_is_valid_field_type_recursive():
    assert is_valid_field_type("List[Dict[str, int]]") is True
    assert is_valid_field_type("Dict[str, List[image]]") is True
    assert is_valid_field_type("Tuple[int, ...]") is True
    # invalid: wrong arity for Dict
    assert is_valid_field_type("Dict[int]") is False
    # invalid: List with multiple args
    assert is_valid_field_type("List[int, str]") is False
    # invalid: Set with multiple args
    assert is_valid_field_type("Set[int, str]") is False
    # invalid: Union without NoneType
    assert is_valid_field_type("Union[float, int]") is False
    # invalid: nested Union on non-None side
    assert is_valid_field_type("Union[Union[int, NoneType], NoneType]") is False


@pytest.mark.anyio
async def test_is_image_type_deep():
    assert is_image_type("List[Dict[str, image]]") is True
    assert is_image_type("Dict[str, List[int]]") is False


@pytest.mark.anyio
async def test_get_base_storage_type_union_with_none_only():
    # base storage type for allowed union
    assert get_base_storage_type("Union[int, NoneType]") == "union"


@pytest.mark.anyio
async def test_invalid_strings_remain_invalid():
    # Ensure obviously invalid type strings fail validation
    assert is_valid_field_type("int | str") is False
    assert is_valid_field_type("WeirdType[foo, bar]") is False

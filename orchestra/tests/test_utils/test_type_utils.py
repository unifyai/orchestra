"""Tests for type_utils module - these don't require database."""

from datetime import date, datetime, time, timedelta

import pytest

from orchestra.web.api.log.utils.type_utils import (
    DEFAULT_FIELD_TYPE,
    get_base_storage_type,
    get_display_type,
    infer_type_from_value,
    is_image_type,
    is_untyped_field,
    is_valid_field_type,
    normalize_type_string,
    parse_nested_type,
    types_match,
)

# ------------------------------------------------------------
# Helpers used in assertions (order-agnostic inner type checks)
# ------------------------------------------------------------


def _inner_types_equal_ignoring_order(type_str, expected_inner_set):
    base, inner = parse_nested_type(type_str)
    assert inner is not None, f"Type {type_str} expected to be nested"
    assert set(inner) == set(expected_inner_set), f"Inner mismatch for {type_str}"


# -----------------
# Normalization
# -----------------


@pytest.mark.anyio
async def test_normalize_simple_types():
    """Test normalization of simple types."""
    assert normalize_type_string("int") == "int"
    assert normalize_type_string("INT") == "int"
    assert normalize_type_string("str") == "str"
    assert normalize_type_string("STR") == "str"
    assert normalize_type_string("float") == "float"
    assert normalize_type_string("FLOAT") == "float"
    assert normalize_type_string("ANY") == "Any"
    assert normalize_type_string("nonetype") == "NoneType"
    assert normalize_type_string("ENUM") == "enum"


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
async def test_normalize_nested_list_types():
    """Test normalization of List types."""
    assert normalize_type_string("List[int]") == "List[int]"
    assert normalize_type_string("LIST[INT]") == "List[int]"
    assert normalize_type_string("list[int]") == "List[int]"
    assert normalize_type_string("List[str]") == "List[str]"
    assert normalize_type_string("List[float]") == "List[float]"
    assert normalize_type_string("List[image]") == "List[image]"
    # heterogeneous list normalization
    assert normalize_type_string("list[int, Str, IMAGE]") == "List[int, str, image]"


@pytest.mark.anyio
async def test_normalize_nested_dict_types():
    """Test normalization of Dict types."""
    assert normalize_type_string("Dict[str, float]") == "Dict[str, float]"
    assert normalize_type_string("DICT[STR, FLOAT]") == "Dict[str, float]"
    assert normalize_type_string("dict[str, float]") == "Dict[str, float]"
    assert normalize_type_string("Dict[str, int]") == "Dict[str, int]"


@pytest.mark.anyio
async def test_normalize_with_spaces():
    """Test normalization handles spaces in nested types."""
    assert normalize_type_string("Dict[str,float]") == "Dict[str, float]"
    assert normalize_type_string("Dict[ str , float ]") == "Dict[str, float]"
    assert normalize_type_string("List[ int , str ]") == "List[int, str]"


@pytest.mark.anyio
async def test_nested_deep_normalization():
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


@pytest.mark.anyio
async def test_tuple_and_variadic_tuple():
    assert normalize_type_string("Tuple[int, float]") == "Tuple[int, float]"
    assert normalize_type_string("tuple[int, ...]") == "Tuple[int, ...]"


@pytest.mark.anyio
async def test_set_and_builtin_generics():
    assert normalize_type_string("set[int]") == "Set[int]"
    assert normalize_type_string("dict[str, list[int]]") == "Dict[str, List[int]]"


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


# -----------------
# Parsing
# -----------------


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
    """Test parsing of List types including heterogeneous."""
    base, inner = parse_nested_type("List[int]")
    assert base == "List"
    assert inner == ["int"]

    base, inner = parse_nested_type("List[str, int]")
    assert base == "List"
    assert inner == ["str", "int"]


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
async def test_parse_nested_type_preserves_inner_commas():
    base, inner = parse_nested_type("Dict[str, List[int]]")
    assert base == "Dict"
    assert inner == ["str", "List[int]"]


# -----------------
# Validation
# -----------------


@pytest.mark.anyio
async def test_is_valid_field_type_recursive_and_policy():
    # Valid
    assert is_valid_field_type("List[Dict[str, int]]") is True
    assert is_valid_field_type("List[int, str]") is True
    assert is_valid_field_type("Set[int, str]") is True
    assert is_valid_field_type("Dict[str, List[image]]") is True
    assert is_valid_field_type("Tuple[int, ...]") is True
    assert is_valid_field_type("Union[int, NoneType]") is True

    # Invalid
    assert is_valid_field_type("Dict[int]") is False  # wrong arity
    assert is_valid_field_type("Union[float, int]") is False  # not Optional-like
    assert (
        is_valid_field_type("Union[Union[int, NoneType], NoneType]") is False
    )  # nested
    assert is_valid_field_type("int | str") is False  # '|' not supported
    assert is_valid_field_type("WeirdType[foo, bar]") is False  # unknown container


# -----------------
# Image detection
# -----------------


@pytest.mark.anyio
async def test_is_image_type():
    """Test image type detection."""
    assert is_image_type("image") is True
    assert is_image_type("Image") is True
    assert is_image_type("IMAGE") is True
    assert is_image_type("List[image]") is True
    assert is_image_type("List[Image]") is True
    assert is_image_type("Dict[str, List[image]]") is True

    assert is_image_type("str") is False
    assert is_image_type("int") is False
    assert is_image_type("List[int]") is False
    assert is_image_type("Dict[str, float]") is False


# -----------------
# Base storage type
# -----------------


@pytest.mark.anyio
async def test_get_base_storage_type():
    """Test getting base storage type."""
    # Simple types return as-is
    assert get_base_storage_type("int") == "int"
    assert get_base_storage_type("str") == "str"
    assert get_base_storage_type("float") == "float"
    assert get_base_storage_type("image") == "image"
    assert get_base_storage_type("NoneType") == "NoneType"
    assert get_base_storage_type("Any") == "Any"

    # Nested types return the outer type in lowercase
    assert get_base_storage_type("List[int]") == "list"
    assert get_base_storage_type("List[str, int]") == "list"
    assert get_base_storage_type("List[image]") == "list"
    assert get_base_storage_type("Dict[str, float]") == "dict"
    assert get_base_storage_type("Dict[str, int]") == "dict"
    assert get_base_storage_type("Tuple[int, ...]") == "tuple"
    assert get_base_storage_type("Set[int, str]") == "set"
    assert get_base_storage_type("Union[int, NoneType]") == "union"


# -----------------
# Display type
# -----------------


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
    assert get_display_type(None, None) == DEFAULT_FIELD_TYPE


@pytest.mark.anyio
async def test_is_untyped_field():
    assert is_untyped_field("Any") is True
    assert is_untyped_field("any") is True
    assert is_untyped_field("int") is False
    assert is_untyped_field("List[int]") is False


# -----------------
# types_match behavior
# -----------------


@pytest.mark.anyio
async def test_types_match_basic_and_nested():
    # exact / case-insensitive
    assert types_match("int", "int") is True
    assert types_match("INT", "int") is True

    # enum stored as str
    assert types_match("enum", "str") is True

    # NoneType matches anything
    assert types_match("NoneType", "int") is True
    assert types_match("List[int]", "NoneType") is True

    # base match: nested vs base
    assert types_match("List[int]", "list") is True
    assert types_match("Dict[str, int]", "dict") is True

    # mismatch
    assert types_match("List[int]", "dict") is False
    assert types_match("int", "str") is False


# -----------------
# Value → Type inference
# -----------------


@pytest.mark.anyio
async def test_infer_type_from_value_scalars_and_temporal():
    assert infer_type_from_value(None) == "NoneType"
    assert infer_type_from_value(True) == "bool"
    assert infer_type_from_value(3) == "int"
    assert infer_type_from_value(3.14) == "float"
    assert infer_type_from_value(datetime(2024, 1, 2, 3, 4, 5)) == "datetime"
    assert infer_type_from_value(date(2024, 1, 2)) == "date"
    assert infer_type_from_value(time(3, 4, 5)) == "time"
    assert infer_type_from_value(timedelta(seconds=5)) == "timedelta"

    # strings
    assert infer_type_from_value("2024-02-02") == "date"
    assert infer_type_from_value("12:03:04") == "time"
    assert infer_type_from_value("01:02:03") == "time"  # HH:MM:SS
    assert infer_type_from_value("P1Y2M3DT4H5M6S") == "timedelta"
    assert infer_type_from_value("2024-02-02T01:02:03") == "datetime"
    assert infer_type_from_value("hello") == "str"
    assert infer_type_from_value("") == "str"


@pytest.mark.anyio
async def test_infer_type_from_value_media_detector_hook():
    # Fake detector that returns "image" for strings starting with "IMG:"
    def _fake_detector(s: str):
        return "image" if s.startswith("IMG:") else None

    assert infer_type_from_value("IMG:abcdef", media_detector=_fake_detector) == "image"
    assert infer_type_from_value("NOPE", media_detector=_fake_detector) == "str"


@pytest.mark.anyio
async def test_infer_type_from_value_list_heterogeneous_and_none():
    out = infer_type_from_value([1, "a", None, 2])
    # Heterogeneous allowed; NoneType appears as inner type too
    base, inner = parse_nested_type(out)
    assert base == "List"
    assert set(inner) == {"int", "str", "NoneType"}


@pytest.mark.anyio
async def test_infer_type_from_value_set_heterogeneous_and_empty():
    out = infer_type_from_value({1, "x"})
    base, inner = parse_nested_type(out)
    assert base == "Set"
    assert set(inner) == {"int", "str"}

    out2 = infer_type_from_value(set())
    assert out2 == "Set[Any]"


@pytest.mark.anyio
async def test_infer_type_from_value_tuple_variadic_and_fixed():
    # homogeneous > 1 -> variadic
    out = infer_type_from_value((1, 2, 3))
    assert out == "Tuple[int, ...]"

    # single element -> fixed Tuple[T]
    out2 = infer_type_from_value((1,))
    assert out2 == "Tuple[int]"

    # heterogeneous -> fixed expanded
    out3 = infer_type_from_value((1, "x", None))
    base, inner = parse_nested_type(out3)
    assert base == "Tuple"
    assert inner == ["int", "str", "NoneType"]


@pytest.mark.anyio
async def test_infer_type_from_value_dict_simple_and_mixed():
    # simple
    out = infer_type_from_value({"a": 1, "b": 2})
    assert out == "Dict[str, int]"

    # heterogeneous keys -> Any (coalesce)
    out2 = infer_type_from_value({1: "x", "y": "z"})
    assert out2 == "Dict[Any, str]"

    # heterogeneous values -> Any (coalesce)
    out3 = infer_type_from_value({"a": 1, "b": "z"})
    assert out3 == "Dict[str, Any]"

    # values with None -> keep non-NoneType if exactly one non-None
    out4 = infer_type_from_value({"a": None, "b": 5})
    assert out4 == "Dict[str, int]"

    # both sides very mixed -> Any
    out5 = infer_type_from_value({1: "x", "y": 2.0, None: None})
    # keys: {int, str, NoneType} -> Any; values: {str, float, NoneType} -> Any
    assert out5 == "Dict[Any, Any]"


# -----------------
# Round-trip & memberships
# -----------------


@pytest.mark.anyio
async def test_round_trip_parse_then_render_preserves_intent():
    raw = "List[Dict[str, List[int, image]], Set[str, int], Tuple[int, ...]]"
    norm = normalize_type_string(raw)
    # Must be parsable back and stay equivalent once normalized
    base, inner = parse_nested_type(norm)
    assert base == "List"
    # Ensure essential pieces exist (order-agnostic)
    assert "Dict[str, List[int, image]]" in inner
    assert "Set[str, int]" in inner
    assert "Tuple[int, ...]" in inner


@pytest.mark.anyio
async def test_order_agnostic_hetero_inner_types_for_lists_sets():
    t = infer_type_from_value([1, "x", 3.14, "y"])
    _inner_types_equal_ignoring_order(t, {"int", "str", "float"})
    t2 = infer_type_from_value({"a", 1, 2.0})
    _inner_types_equal_ignoring_order(t2, {"str", "int", "float"})


# -----------------
# Invalid inputs
# -----------------


@pytest.mark.anyio
async def test_invalid_strings_remain_invalid():
    # The "|" operator isn't supported by the parser
    assert is_valid_field_type("int | str") is False
    # Unknown containers are invalid
    assert is_valid_field_type("WeirdType[foo, bar]") is False
    # Bad bracket/structure
    assert is_valid_field_type("List[int") is False
    assert is_valid_field_type("Dict[str,]") is False
    assert is_valid_field_type("Tuple[]") is False


# ================================================================================
# Pydantic JSON Schema Support Tests
# ================================================================================

import json as json_lib
from typing import List as TypingList
from typing import Optional as TypingOptional

try:
    from pydantic import BaseModel, Field, RootModel

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False

from orchestra.web.api.log.utils.type_utils import (
    infer_simple_type_from_pydantic_schema,
    is_pydantic_schema,
    normalize_pydantic_schema,
    pydantic_schema_to_string,
    validate_value_against_pydantic_schema,
)

# ================================================================================
# Define Pydantic Models for Testing
# ================================================================================

if PYDANTIC_AVAILABLE:
    # Simple model with basic fields
    class Person(BaseModel):
        """A simple person model."""

        name: str = Field(description="Person's name")
        age: int = Field(description="Person's age")
        email: TypingOptional[str] = Field(None, description="Optional email")

    # Model with nested structure
    class Address(BaseModel):
        """An address model."""

        street: str
        city: str
        zip_code: str

    class PersonWithAddress(BaseModel):
        """Person with address."""

        name: str
        age: int
        address: Address

    # Model with list of items
    class Team(BaseModel):
        """A team with multiple members."""

        team_name: str
        members: TypingList[Person]

    # RootModel with list (similar to AnnotatedImageRefs)
    class RawImageRef(BaseModel):
        """Raw image reference."""

        url: str = Field(description="Image URL")
        width: TypingOptional[int] = None
        height: TypingOptional[int] = None

    class AnnotatedImageRef(BaseModel):
        """Annotated image reference."""

        raw_image_ref: RawImageRef = Field(
            description="Reference to the underlying raw image",
        )
        annotation: str = Field(description="Context-specific annotation")

    class AnnotatedImageRefs(RootModel[TypingList[AnnotatedImageRef]]):
        """Container for a list of annotated image references."""

    # Model with union types
    class Product(BaseModel):
        """A product."""

        name: str
        price: float

    class Service(BaseModel):
        """A service."""

        name: str
        hourly_rate: float

    # Model with optional nested structure
    class Order(BaseModel):
        """An order."""

        order_id: str
        customer: Person
        items: TypingList[Product]
        notes: TypingOptional[str] = None


@pytest.mark.anyio
async def test_is_pydantic_schema_with_json_schemas():
    """Test detection of Pydantic JSON schemas."""
    # Get schema from Pydantic model
    person_schema = Person.model_json_schema()
    assert is_pydantic_schema(person_schema) is True

    # Pure JSON schema (what we receive over REST API)
    pure_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert is_pydantic_schema(pure_schema) is True

    # Schema with JSON schema keys
    schema_array = {
        "type": "array",
        "items": {"type": "integer"},
    }
    assert is_pydantic_schema(schema_array) is True

    # Not a schema
    assert is_pydantic_schema(["foo", "bar"]) is False
    assert is_pydantic_schema({"name", "John"}) is False


@pytest.mark.anyio
async def test_is_not_pydantic_schema():
    """Test detection of Pydantic schemas as JSON strings."""
    # Get schema from Pydantic model and convert to JSON string
    person_schema = Person.model_json_schema()
    schema_str = json_lib.dumps(person_schema)
    assert is_pydantic_schema(schema_str) is True

    # Not a JSON schema string
    assert is_pydantic_schema("int") is False
    assert is_pydantic_schema("List[int]") is False


@pytest.mark.anyio
async def test_normalize_pydantic_schema_from_real_model():
    """Test normalization of Pydantic schemas from real models (pure JSON schemas)."""
    # Simple model - pure JSON schema from model_json_schema()
    person_schema = Person.model_json_schema()
    normalized = normalize_pydantic_schema(person_schema)
    assert "properties" in normalized
    assert "name" in normalized["properties"]
    assert "age" in normalized["properties"]
    # Schema should be pure (no custom markers added)
    assert "$pydantic_schema" not in normalized
    assert "_pydantic_model_class" not in normalized

    # Nested model with $defs
    person_with_address_schema = PersonWithAddress.model_json_schema()
    normalized2 = normalize_pydantic_schema(person_with_address_schema)
    assert "properties" in normalized2
    assert "address" in normalized2["properties"]
    assert "$defs" in normalized2  # Pydantic generates this for nested models


@pytest.mark.anyio
async def test_normalize_pydantic_schema_from_json_string():
    """Test normalization of Pydantic schemas from JSON strings (REST API)."""
    # Get schema and convert to string (simulating REST API transmission)
    person_schema = Person.model_json_schema()
    schema_str = json_lib.dumps(person_schema)

    normalized = normalize_pydantic_schema(schema_str)
    assert "properties" in normalized
    # Should be pure schema without custom markers
    assert "$pydantic_schema" not in normalized
    assert "_pydantic_model_class" not in normalized


@pytest.mark.anyio
async def test_pydantic_schema_to_string():
    """Test conversion of Pydantic schema to JSON string for storage."""
    person_schema = Person.model_json_schema()

    schema_str = pydantic_schema_to_string(person_schema)
    assert isinstance(schema_str, str)

    # Should be valid JSON
    parsed = json_lib.loads(schema_str)
    assert "properties" in parsed


@pytest.mark.anyio
async def test_validate_value_against_pydantic_schema_simple_model():
    """Test validation of values against simple Pydantic model schemas (backwards compat)."""
    person_schema = Person.model_json_schema()

    # Valid value with all fields
    valid_value = {"name": "John", "age": 30, "email": "john@example.com"}
    is_valid, error = validate_value_against_pydantic_schema(valid_value, person_schema)
    assert is_valid is True
    assert error is None

    # Valid value without optional field
    valid_value2 = {"name": "Jane", "age": 25}
    is_valid2, error2 = validate_value_against_pydantic_schema(
        valid_value2,
        person_schema,
    )
    assert is_valid2 is True
    assert error2 is None

    # Invalid value - wrong type for age
    invalid_value = {"name": "John", "age": "thirty"}
    is_valid3, error3 = validate_value_against_pydantic_schema(
        invalid_value,
        person_schema,
    )
    assert is_valid3 is False
    assert error3 is not None

    # Invalid value - missing required field
    invalid_value2 = {"age": 30}
    is_valid4, error4 = validate_value_against_pydantic_schema(
        invalid_value2,
        person_schema,
    )
    assert is_valid4 is False
    assert error4 is not None


@pytest.mark.anyio
async def test_validate_value_against_pydantic_schema_with_list():
    """Test validation of list values against Pydantic model with list."""
    team_schema = Team.model_json_schema()

    # Valid value
    valid_value = {
        "team_name": "Engineering",
        "members": [
            {"name": "Alice", "age": 30, "email": "alice@example.com"},
            {"name": "Bob", "age": 25},
        ],
    }
    is_valid, error = validate_value_against_pydantic_schema(valid_value, team_schema)
    assert is_valid is True
    assert error is None

    # Invalid value - wrong item type in list
    invalid_value = {
        "team_name": "Engineering",
        "members": [
            {"name": "Alice", "age": "thirty"},  # age should be int
        ],
    }
    is_valid2, error2 = validate_value_against_pydantic_schema(
        invalid_value,
        team_schema,
    )
    assert is_valid2 is False
    assert error2 is not None


@pytest.mark.anyio
async def test_validate_value_against_pydantic_schema_nested():
    """Test validation of nested structures with RootModel (similar to AnnotatedImageRefs)."""
    annotated_refs_schema = AnnotatedImageRefs.model_json_schema()

    # Valid value (similar to AnnotatedImageRefs)
    valid_value = [
        {
            "raw_image_ref": {
                "url": "https://example.com/image1.jpg",
                "width": 800,
                "height": 600,
            },
            "annotation": "This is image 1",
        },
        {
            "raw_image_ref": {"url": "https://example.com/image2.jpg"},
            "annotation": "This is image 2",
        },
    ]
    is_valid, error = validate_value_against_pydantic_schema(
        valid_value,
        annotated_refs_schema,
    )
    assert is_valid is True
    assert error is None

    # Invalid value - missing annotation
    invalid_value = [
        {
            "raw_image_ref": {"url": "https://example.com/image1.jpg"},
            # missing annotation field
        },
    ]
    is_valid2, error2 = validate_value_against_pydantic_schema(
        invalid_value,
        annotated_refs_schema,
    )
    assert is_valid2 is False
    assert error2 is not None

    # Invalid value - missing url in raw_image_ref
    invalid_value3 = [
        {
            "raw_image_ref": {"width": 800},  # missing required url
            "annotation": "Image without URL",
        },
    ]
    is_valid3, error3 = validate_value_against_pydantic_schema(
        invalid_value3,
        annotated_refs_schema,
    )
    assert is_valid3 is False
    assert error3 is not None


@pytest.mark.anyio
async def test_infer_simple_type_from_pydantic_schema_rootmodel_list():
    """Test inferring simple type from RootModel with list."""
    # AnnotatedImageRefs is a RootModel[List[AnnotatedImageRef]]
    schema = AnnotatedImageRefs.model_json_schema()
    simple_type = infer_simple_type_from_pydantic_schema(schema)
    # Use satisfying policy: the inferred type should satisfy list family
    assert types_match("list", simple_type) is True


@pytest.mark.anyio
async def test_infer_simple_type_from_pydantic_schema_object():
    """Test inferring simple type from object schemas."""
    person_schema = Person.model_json_schema()
    simple_type = infer_simple_type_from_pydantic_schema(person_schema)
    assert types_match("dict", simple_type) is True

    # Nested object
    person_with_address_schema = PersonWithAddress.model_json_schema()
    simple_type2 = infer_simple_type_from_pydantic_schema(person_with_address_schema)
    assert types_match("dict", simple_type2) is True


@pytest.mark.anyio
async def test_infer_simple_type_from_pydantic_schema_with_list_field():
    """Test inferring simple type from model with list field."""
    team_schema = Team.model_json_schema()
    simple_type = infer_simple_type_from_pydantic_schema(team_schema)
    # Team is an object with properties; satisfy dict family
    assert types_match("dict", simple_type) is True


@pytest.mark.anyio
async def test_infer_simple_type_from_pydantic_schema_complex_nested():
    """Test inferring simple type from complex nested schemas."""
    order_schema = Order.model_json_schema()
    simple_type = infer_simple_type_from_pydantic_schema(order_schema)
    # Order has nested Person and list of Product; satisfy dict family
    assert types_match("dict", simple_type) is True


@pytest.mark.anyio
async def test_infer_simple_type_from_pydantic_schema_primitives():
    """Test inferring simple type from primitive schemas."""
    assert infer_simple_type_from_pydantic_schema({"type": "string"}) == "str"
    assert infer_simple_type_from_pydantic_schema({"type": "integer"}) == "int"
    assert infer_simple_type_from_pydantic_schema({"type": "number"}) == "float"
    assert infer_simple_type_from_pydantic_schema({"type": "boolean"}) == "bool"
    assert infer_simple_type_from_pydantic_schema({"type": "null"}) == "NoneType"


@pytest.mark.anyio
async def test_infer_simple_type_from_pydantic_schema_array():
    """Test inferring simple type from array schemas."""
    schema = {
        "type": "array",
        "items": {"type": "integer"},
    }
    simple_type = infer_simple_type_from_pydantic_schema(schema)
    assert simple_type == "List[int]"

    # Array without items
    schema2 = {"type": "array"}
    simple_type2 = infer_simple_type_from_pydantic_schema(schema2)
    assert simple_type2 == "list"


@pytest.mark.anyio
async def test_is_valid_field_type_accepts_pydantic_schemas():
    """Test that is_valid_field_type accepts Pydantic schemas."""
    # Valid Pydantic schema from real model
    person_schema = Person.model_json_schema()
    assert is_valid_field_type(person_schema) is True

    # Valid Pydantic schema as JSON string
    team_schema = Team.model_json_schema()
    schema_str = json_lib.dumps(team_schema)
    assert is_valid_field_type(schema_str) is True

    # Invalid schema (not a dict or JSON string)
    assert is_valid_field_type([1, 2, 3]) is False


@pytest.mark.anyio
async def test_types_dont_match_with_pydantic_schemas():
    """Test types_match function does not match with Pydantic schemas."""
    # RootModel with list should match "list" inferred type (List[dict] base is list)
    annotated_refs_schema = AnnotatedImageRefs.model_json_schema()
    assert types_match(annotated_refs_schema, "list") is False
    assert types_match(annotated_refs_schema, "List[dict]") is False

    # Object schema should match "dict" inferred type
    person_schema = Person.model_json_schema()
    assert types_match(person_schema, "dict") is False

    # Team (with nested list) should match "dict"
    team_schema = Team.model_json_schema()
    assert types_match(team_schema, "dict") is False

    # Should not match mismatched types
    assert types_match(person_schema, "list") is False
    assert types_match(annotated_refs_schema, "dict") is False


# ================================================================================
# Integration Tests with LogDAO.infer_type
# ================================================================================

from orchestra.db.dao.log_event_dao import LogEventDAO

# Backward-compat alias for tests that still reference LogDAO
LogDAO = LogEventDAO


@pytest.mark.anyio
async def test_logdao_infer_type_with_pydantic_schema_valid():
    """Test LogDAO.infer_type with valid Pydantic schema (backwards compat)."""
    person_schema = Person.model_json_schema()

    # Valid person value
    valid_value = {"name": "Alice", "age": 30, "email": "alice@example.com"}

    # With explicit Pydantic schema, infer_type returns schema JSON; schema vs pythonic types should not match
    inferred = LogDAO.infer_type("person", valid_value, explicit_type=person_schema)
    assert is_pydantic_schema(inferred) is True
    assert types_match("dict", inferred) is False


@pytest.mark.anyio
async def test_logdao_infer_type_with_pydantic_schema_invalid():
    """Test LogDAO.infer_type with invalid value against Pydantic schema."""
    person_schema = Person.model_json_schema()

    # Invalid person value (age should be int)
    invalid_value = {"name": "Bob", "age": "thirty"}

    # Should raise ValueError
    with pytest.raises(ValueError) as exc_info:
        LogDAO.infer_type("person", invalid_value, explicit_type=person_schema)

    assert "does not match Pydantic schema" in str(exc_info.value)


@pytest.mark.anyio
async def test_logdao_infer_type_with_rootmodel_schema():
    """Test LogDAO.infer_type with RootModel Pydantic schema."""
    annotated_refs_schema = AnnotatedImageRefs.model_json_schema()

    # Valid annotated image refs
    valid_value = [
        {
            "raw_image_ref": {"url": "https://example.com/img1.jpg"},
            "annotation": "First image",
        },
        {
            "raw_image_ref": {"url": "https://example.com/img2.jpg", "width": 800},
            "annotation": "Second image",
        },
    ]

    inferred = LogDAO.infer_type(
        "images",
        valid_value,
        explicit_type=annotated_refs_schema,
    )
    assert is_pydantic_schema(inferred) is True
    assert types_match("List[dict]", inferred) is False


@pytest.mark.anyio
async def test_logdao_infer_type_with_nested_pydantic_schema():
    """Test LogDAO.infer_type with nested Pydantic schema."""
    order_schema = Order.model_json_schema()

    # Valid order with nested person and products
    valid_value = {
        "order_id": "ORD-123",
        "customer": {"name": "Charlie", "age": 35},
        "items": [
            {"name": "Widget", "price": 19.99},
            {"name": "Gadget", "price": 29.99},
        ],
        "notes": "Rush delivery",
    }

    inferred = LogDAO.infer_type("order", valid_value, explicit_type=order_schema)
    assert is_pydantic_schema(inferred) is True
    assert types_match("dict", inferred) is False


@pytest.mark.anyio
async def test_logdao_infer_type_with_pydantic_schema_as_json_string():
    """Test LogDAO.infer_type with Pydantic schema as JSON string."""
    person_schema = Person.model_json_schema()
    schema_str = json_lib.dumps(person_schema)

    valid_value = {"name": "Diana", "age": 28}

    inferred = LogDAO.infer_type("person", valid_value, explicit_type=schema_str)
    assert is_pydantic_schema(inferred) is True
    assert types_match("dict", inferred) is False


@pytest.mark.anyio
async def test_logdao_infer_type_without_explicit_type_still_works():
    """Test that LogDAO.infer_type without explicit type still works (backwards compat)."""
    # No explicit type - should infer from value
    value = {"name": "Eve", "age": 32}

    inferred = LogDAO.infer_type("data", value)
    # Mixed value types should satisfy dict family
    assert types_match("dict", inferred) is True

    # List value
    list_value = [1, 2, 3, 4]
    inferred2 = LogDAO.infer_type("numbers", list_value)
    assert inferred2 == "List[int]"


# ================================================================================
# Round-Trip and Type Recreation Tests
# ================================================================================


@pytest.mark.anyio
async def test_pydantic_schema_roundtrip_simple_model():
    """
    Test that we can:
    1. Get a Pydantic schema from a model
    2. Store it as JSON
    3. Recreate it and validate values
    4. Infer correct simple types from it
    """
    # Step 1: Get schema from Pydantic model
    original_schema = Person.model_json_schema()

    # Step 2: Convert to JSON string (simulating storage)
    schema_json = pydantic_schema_to_string(original_schema)

    # Step 3: Recreate from JSON string
    recreated_schema = normalize_pydantic_schema(schema_json)

    # Step 4: Validate values against recreated schema
    valid_value = {"name": "Alice", "age": 30}
    is_valid, error = validate_value_against_pydantic_schema(
        valid_value,
        recreated_schema,
    )
    assert is_valid is True
    assert error is None

    # Step 5: Infer simple type matches original
    simple_type = infer_simple_type_from_pydantic_schema(recreated_schema)
    assert types_match("dict", simple_type) is True

    # Step 6: Type matching policy: schema vs pythonic type should NOT match
    assert types_match(recreated_schema, "dict") is False


@pytest.mark.anyio
async def test_pydantic_schema_roundtrip_nested_model():
    """Test round-trip for nested Pydantic models with $ref and $defs."""
    # PersonWithAddress has nested Address model
    original_schema = PersonWithAddress.model_json_schema()

    # Verify schema has $defs for nested model
    assert "$defs" in original_schema
    assert "Address" in original_schema["$defs"]

    # Convert to JSON and back
    schema_json = pydantic_schema_to_string(original_schema)
    recreated_schema = normalize_pydantic_schema(schema_json)

    # Validate complex nested value
    valid_value = {
        "name": "Bob",
        "age": 35,
        "address": {
            "street": "123 Main St",
            "city": "Springfield",
            "zip_code": "12345",
        },
    }
    is_valid, error = validate_value_against_pydantic_schema(
        valid_value,
        recreated_schema,
    )
    assert is_valid is True

    # Invalid nested value should fail
    invalid_value = {
        "name": "Bob",
        "age": 35,
        "address": {
            "street": "123 Main St",
            # missing required city field
            "zip_code": "12345",
        },
    }
    is_valid2, error2 = validate_value_against_pydantic_schema(
        invalid_value,
        recreated_schema,
    )
    assert is_valid2 is False
    assert error2 is not None


@pytest.mark.anyio
async def test_pydantic_schema_roundtrip_rootmodel_with_refs():
    """Test round-trip for RootModel with nested $refs (like AnnotatedImageRefs)."""
    # AnnotatedImageRefs is RootModel[List[AnnotatedImageRef]]
    # which has nested references
    original_schema = AnnotatedImageRefs.model_json_schema()

    # Verify complex structure with $defs
    assert "$defs" in original_schema
    assert "AnnotatedImageRef" in original_schema["$defs"]
    assert "RawImageRef" in original_schema["$defs"]
    assert original_schema["type"] == "array"

    # Convert to JSON and back
    schema_json = pydantic_schema_to_string(original_schema)
    recreated_schema = normalize_pydantic_schema(schema_json)

    # Infer simple type correctly resolves $ref
    simple_type = infer_simple_type_from_pydantic_schema(recreated_schema)
    assert types_match("List[dict]", simple_type) is True

    # Validate complex nested structure
    valid_value = [
        {
            "raw_image_ref": {
                "url": "https://example.com/img1.jpg",
                "width": 800,
                "height": 600,
            },
            "annotation": "First image",
        },
        {
            "raw_image_ref": {"url": "https://example.com/img2.jpg"},
            "annotation": "Second image",
        },
    ]
    is_valid, error = validate_value_against_pydantic_schema(
        valid_value,
        recreated_schema,
    )
    assert is_valid is True

    # Type matching policy: schema vs pythonic types should NOT match
    assert types_match(recreated_schema, "list") is False
    assert types_match(recreated_schema, "List[dict]") is False


@pytest.mark.anyio
async def test_pydantic_schema_future_value_validation():
    """
    Test that future incoming values are validated against stored schemas.
    This simulates the workflow where:
    1. A field is created with a Pydantic schema
    2. Future logs try to add values
    3. We validate them against the original schema
    """
    # Step 1: Define field with Pydantic schema
    order_schema = Order.model_json_schema()
    stored_schema_json = pydantic_schema_to_string(order_schema)

    # Step 2: Future log attempts to add value
    # Valid value
    future_valid_value = {
        "order_id": "ORD-456",
        "customer": {"name": "Charlie", "age": 40},
        "items": [
            {"name": "Widget", "price": 9.99},
        ],
    }

    # Recreate schema from storage and validate
    loaded_schema = normalize_pydantic_schema(stored_schema_json)
    is_valid, error = validate_value_against_pydantic_schema(
        future_valid_value,
        loaded_schema,
    )
    assert is_valid is True

    # Invalid future value should be rejected
    future_invalid_value = {
        "order_id": "ORD-457",
        "customer": {"name": "Dave"},  # missing required age
        "items": [],
    }
    is_valid2, error2 = validate_value_against_pydantic_schema(
        future_invalid_value,
        loaded_schema,
    )
    assert is_valid2 is False
    assert "age" in error2.lower() or "required" in error2.lower()


@pytest.mark.anyio
async def test_arbitrary_pydantic_nesting_depth():
    """Test that arbitrarily deep nesting is handled correctly."""
    # Create a deeply nested Pydantic model
    class Level3(BaseModel):
        value: str

    class Level2(BaseModel):
        items: TypingList[Level3]

    class Level1(BaseModel):
        nested: Level2

    class Root(BaseModel):
        data: Level1

    # Get schema
    schema = Root.model_json_schema()

    # Verify $defs contains all levels
    assert "$defs" in schema
    assert "Level1" in schema["$defs"]
    assert "Level2" in schema["$defs"]
    assert "Level3" in schema["$defs"]

    # Validate deeply nested value
    valid_value = {
        "data": {
            "nested": {
                "items": [
                    {"value": "a"},
                    {"value": "b"},
                ],
            },
        },
    }
    is_valid, error = validate_value_against_pydantic_schema(valid_value, schema)
    assert is_valid is True

    # Infer simple type
    simple_type = infer_simple_type_from_pydantic_schema(schema)
    assert types_match("dict", simple_type) is True


# ================================================================================
# Fallback Validation Tests (without jsonschema library)
# ================================================================================


@pytest.mark.anyio
async def test_fallback_validation_simple_model():
    """Test fallback validation without jsonschema library."""
    from orchestra.web.api.log.utils.type_utils import _validate_against_schema_fallback

    person_schema = Person.model_json_schema()

    # Valid value
    valid_value = {"name": "Alice", "age": 30}
    is_valid, error = _validate_against_schema_fallback(valid_value, person_schema)
    assert is_valid is True
    assert error is None
    # jsonschema path should agree
    is_valid_js, _ = validate_value_against_pydantic_schema(valid_value, person_schema)
    assert is_valid_js is is_valid

    # Invalid - missing required field
    invalid_value = {"name": "Bob"}
    is_valid, error = _validate_against_schema_fallback(invalid_value, person_schema)
    assert is_valid is False
    assert "age" in error or "required" in error
    is_valid_js, _ = validate_value_against_pydantic_schema(
        invalid_value,
        person_schema,
    )
    assert is_valid_js is is_valid

    # Invalid - wrong type
    invalid_value2 = {"name": "Charlie", "age": "thirty"}
    is_valid2, error2 = _validate_against_schema_fallback(invalid_value2, person_schema)
    assert is_valid2 is False
    assert "age" in error2 or "integer" in error2
    is_valid2_js, _ = validate_value_against_pydantic_schema(
        invalid_value2,
        person_schema,
    )
    assert is_valid2_js is is_valid2


@pytest.mark.anyio
async def test_fallback_validation_nested_with_refs():
    """Test fallback validation with nested models and $ref resolution."""
    from orchestra.web.api.log.utils.type_utils import _validate_against_schema_fallback

    person_with_address_schema = PersonWithAddress.model_json_schema()

    # Valid nested value
    valid_value = {
        "name": "Bob",
        "age": 35,
        "address": {"street": "123 Main St", "city": "NYC", "zip_code": "10001"},
    }
    is_valid, error = _validate_against_schema_fallback(
        valid_value,
        person_with_address_schema,
    )
    assert is_valid is True
    is_valid_js, _ = validate_value_against_pydantic_schema(
        valid_value,
        person_with_address_schema,
    )
    assert is_valid_js is is_valid

    # Invalid - missing nested required field
    invalid_value = {
        "name": "Charlie",
        "age": 40,
        "address": {"street": "456 Elm St"},  # missing city
    }
    is_valid, error = _validate_against_schema_fallback(
        invalid_value,
        person_with_address_schema,
    )
    assert is_valid is False
    assert "city" in error
    is_valid_js, _ = validate_value_against_pydantic_schema(
        invalid_value,
        person_with_address_schema,
    )
    assert is_valid_js is is_valid


@pytest.mark.anyio
async def test_fallback_validation_array_types():
    """Test fallback validation with array/list types."""
    from orchestra.web.api.log.utils.type_utils import _validate_against_schema_fallback

    team_schema = Team.model_json_schema()

    # Valid array
    valid_value = {
        "team_name": "Engineering",
        "members": [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ],
    }
    is_valid, error = _validate_against_schema_fallback(valid_value, team_schema)
    assert is_valid is True
    is_valid_js, _ = validate_value_against_pydantic_schema(valid_value, team_schema)
    assert is_valid_js is is_valid

    # Invalid - wrong type in array
    invalid_value = {
        "team_name": "Engineering",
        "members": [
            {"name": "Alice", "age": "thirty"},  # age should be int
        ],
    }
    is_valid, error = _validate_against_schema_fallback(invalid_value, team_schema)
    assert is_valid is False
    is_valid_js, _ = validate_value_against_pydantic_schema(invalid_value, team_schema)
    assert is_valid_js is is_valid


@pytest.mark.anyio
async def test_fallback_validation_root_model_list():
    """Test fallback validation with RootModel (list at root)."""
    from orchestra.web.api.log.utils.type_utils import _validate_against_schema_fallback

    annotated_refs_schema = AnnotatedImageRefs.model_json_schema()

    # Valid root list
    valid_value = [
        {
            "raw_image_ref": {"url": "http://example.com/img1.jpg"},
            "annotation": "Image 1",
        },
    ]
    is_valid, error = _validate_against_schema_fallback(
        valid_value,
        annotated_refs_schema,
    )
    assert is_valid is True
    is_valid_js, _ = validate_value_against_pydantic_schema(
        valid_value,
        annotated_refs_schema,
    )
    assert is_valid_js is is_valid

    # Invalid - missing required field in nested object
    invalid_value = [
        {
            "raw_image_ref": {"url": "http://example.com/img1.jpg"},
            # missing annotation
        },
    ]
    is_valid, error = _validate_against_schema_fallback(
        invalid_value,
        annotated_refs_schema,
    )
    assert is_valid is False
    is_valid_js, _ = validate_value_against_pydantic_schema(
        invalid_value,
        annotated_refs_schema,
    )
    assert is_valid_js is is_valid


# ================================================================================
# Complex Type Inference Tests
# ================================================================================


@pytest.mark.anyio
async def test_infer_type_from_complex_nested_dict():
    """Test type inference from complex nested dictionaries."""
    # Nested dict with mixed types
    value = {
        "metadata": {
            "id": 123,
            "name": "test",
            "tags": ["tag1", "tag2"],
        },
        "items": [
            {"x": 1, "y": 2},
            {"x": 3, "y": 4},
        ],
    }
    inferred = infer_type_from_value(value)
    # Should satisfy dict family for complex nested structure
    assert types_match("dict", inferred) is True


@pytest.mark.anyio
async def test_infer_type_from_list_of_dicts():
    """Test type inference from list of dictionaries."""
    value = [
        {"name": "Alice", "age": 30},
        {"name": "Bob", "age": 25},
    ]
    inferred = infer_type_from_value(value)
    # Should satisfy List[dict]
    assert types_match("List[dict]", inferred) is True


@pytest.mark.anyio
async def test_infer_type_from_nested_lists():
    """Test type inference from nested lists."""
    value = [[1, 2, 3], [4, 5, 6]]
    inferred = infer_type_from_value(value)
    # List of lists
    base, inner = parse_nested_type(inferred)
    assert base == "List"


@pytest.mark.anyio
async def test_infer_type_from_mixed_type_list():
    """Test type inference from list with multiple types."""
    value = [1, "string", 3.14, True, None]
    inferred = infer_type_from_value(value)
    base, inner = parse_nested_type(inferred)
    assert base == "List"
    # Should have multiple inner types
    assert len(inner) > 1
    assert set(inner) >= {"int", "str", "float", "bool", "NoneType"}


# ================================================================================
# Explicit Type with Pydantic Schema Tests
# ================================================================================


@pytest.mark.anyio
async def test_logdao_explicit_pydantic_schema_validation():
    """Test LogDAO with explicit Pydantic schema enforces validation."""
    person_schema = Person.model_json_schema()

    # Valid value passes
    valid_value = {"name": "Alice", "age": 30}
    inferred = LogDAO.infer_type("person", valid_value, explicit_type=person_schema)
    assert is_pydantic_schema(inferred) is True
    assert types_match("dict", inferred) is False

    # Invalid value raises error
    invalid_value = {"name": "Bob"}  # missing age
    with pytest.raises(ValueError) as exc_info:
        LogDAO.infer_type("person", invalid_value, explicit_type=person_schema)
    assert (
        "does not match" in str(exc_info.value)
        or "missing" in str(exc_info.value).lower()
    )


@pytest.mark.anyio
async def test_logdao_explicit_pydantic_schema_nested():
    """Test LogDAO with nested Pydantic schema."""
    order_schema = Order.model_json_schema()

    valid_value = {
        "order_id": "ORD-001",
        "customer": {"name": "Alice", "age": 30},
        "items": [
            {"name": "Widget", "price": 9.99},
            {"name": "Gadget", "price": 19.99},
        ],
    }
    inferred = LogDAO.infer_type("order", valid_value, explicit_type=order_schema)
    assert is_pydantic_schema(inferred) is True
    assert types_match("dict", inferred) is False

    # Invalid nested value
    invalid_value = {
        "order_id": "ORD-002",
        "customer": {"name": "Bob"},  # missing age
        "items": [],
    }
    with pytest.raises(ValueError):
        LogDAO.infer_type("order", invalid_value, explicit_type=order_schema)


@pytest.mark.anyio
async def test_logdao_explicit_pydantic_schema_as_string():
    """Test LogDAO with Pydantic schema as JSON string."""
    person_schema = Person.model_json_schema()
    schema_str = json_lib.dumps(person_schema)

    valid_value = {"name": "Charlie", "age": 28}
    inferred = LogDAO.infer_type("person", valid_value, explicit_type=schema_str)
    assert is_pydantic_schema(inferred) is True
    assert types_match("dict", inferred) is False


@pytest.mark.anyio
async def test_logdao_explicit_pydantic_schema_rootmodel():
    """Test LogDAO with RootModel schema."""
    annotated_refs_schema = AnnotatedImageRefs.model_json_schema()

    valid_value = [
        {
            "raw_image_ref": {"url": "http://example.com/img.jpg"},
            "annotation": "Test image",
        },
    ]
    inferred = LogDAO.infer_type(
        "images",
        valid_value,
        explicit_type=annotated_refs_schema,
    )
    assert is_pydantic_schema(inferred) is True
    assert types_match("List[dict]", inferred) is False


# ================================================================================
# Type Matching with Pydantic Schemas Tests
# ================================================================================


@pytest.mark.anyio
async def test_types_match_pydantic_schema_vs_inferred():
    """Test that types_match works with Pydantic schemas."""
    person_schema = Person.model_json_schema()

    # Policy: schema vs pythonic type should NOT match
    assert types_match(person_schema, "dict") is False

    # Policy: schema vs pythonic types should NOT match
    annotated_refs_schema = AnnotatedImageRefs.model_json_schema()
    assert types_match(annotated_refs_schema, "list") is False
    assert types_match(annotated_refs_schema, "List[dict]") is False

    # Should not match mismatched types
    assert types_match(person_schema, "list") is False
    assert types_match(annotated_refs_schema, "str") is False


# ================================================================================
# Additional Edge Cases
# ================================================================================


@pytest.mark.anyio
async def test_pydantic_schema_with_optional_fields():
    """Test schemas with optional fields."""

    class OptionalModel(BaseModel):
        required_field: str
        optional_field: TypingOptional[int] = None

    schema = OptionalModel.model_json_schema()

    # Valid with optional field
    value1 = {"required_field": "test", "optional_field": 42}
    is_valid, error = validate_value_against_pydantic_schema(value1, schema)
    assert is_valid is True

    # Valid without optional field
    value2 = {"required_field": "test"}
    is_valid, error = validate_value_against_pydantic_schema(value2, schema)
    assert is_valid is True

    # Invalid - missing required field
    value3 = {"optional_field": 42}
    is_valid, error = validate_value_against_pydantic_schema(value3, schema)
    assert is_valid is False


@pytest.mark.anyio
async def test_pydantic_schema_with_union_types():
    """Test schemas with union types (anyOf)."""

    class UnionModel(BaseModel):
        value: str | int

    schema = UnionModel.model_json_schema()

    # Valid with string
    value1 = {"value": "test"}
    is_valid, error = validate_value_against_pydantic_schema(value1, schema)
    assert is_valid is True

    # Valid with int
    value2 = {"value": 42}
    is_valid, error = validate_value_against_pydantic_schema(value2, schema)
    assert is_valid is True

    # Invalid with wrong type
    value3 = {"value": [1, 2, 3]}
    is_valid, error = validate_value_against_pydantic_schema(value3, schema)
    assert is_valid is False


@pytest.mark.anyio
async def test_infer_type_from_empty_containers():
    """Test type inference from empty containers."""
    assert infer_type_from_value([]) == "List[Any]"
    assert types_match("dict", infer_type_from_value({})) is True
    assert infer_type_from_value(set()) == "Set[Any]"
    assert infer_type_from_value(()) == "Tuple[Any]"

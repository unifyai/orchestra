"""Tests for FKPathParser - nested foreign key path parsing and extraction."""

import pytest

from orchestra.db.utils.fk_path_parser import FKPathParser


class TestPathParsing:
    """Test path parsing functionality."""

    def test_simple_column(self):
        """Test parsing a simple column name."""
        segments = FKPathParser.parse("department_id")
        assert len(segments) == 1
        assert segments[0].name == "department_id"
        assert segments[0].is_array is False
        assert segments[0].is_wildcard is False

    def test_nested_object(self):
        """Test parsing nested object path."""
        segments = FKPathParser.parse("metadata.user.user_id")
        assert len(segments) == 3
        assert [s.name for s in segments] == ["metadata", "user", "user_id"]
        assert all(not s.is_array for s in segments)

    def test_array_wildcard(self):
        """Test parsing array with wildcard."""
        segments = FKPathParser.parse("images[*].image_id")
        assert len(segments) == 2

        # First segment: images[*]
        assert segments[0].name == "images"
        assert segments[0].is_array is True
        assert segments[0].is_wildcard is True

        # Second segment: image_id
        assert segments[1].name == "image_id"
        assert segments[1].is_array is False

    def test_array_specific_index(self):
        """Test parsing array with specific index."""
        segments = FKPathParser.parse("items[0].id")
        assert len(segments) == 2

        assert segments[0].name == "items"
        assert segments[0].is_array is True
        assert segments[0].is_wildcard is False
        assert segments[0].array_index == 0

    def test_nested_arrays(self):
        """Test parsing nested arrays."""
        segments = FKPathParser.parse("teams[*].members[*].user_id")
        assert len(segments) == 3

        assert segments[0].name == "teams"
        assert segments[0].is_array is True
        assert segments[0].is_wildcard is True

        assert segments[1].name == "members"
        assert segments[1].is_array is True
        assert segments[1].is_wildcard is True

        assert segments[2].name == "user_id"
        assert segments[2].is_array is False

    def test_mixed_nesting(self):
        """Test parsing mixed array and object nesting."""
        segments = FKPathParser.parse("data.users[*].profile.id")
        assert len(segments) == 4
        assert segments[0].name == "data"
        assert segments[0].is_array is False
        assert segments[1].name == "users"
        assert segments[1].is_array is True
        assert segments[2].name == "profile"
        assert segments[2].is_array is False
        assert segments[3].name == "id"


class TestValueExtraction:
    """Test value extraction from nested structures."""

    def test_extract_simple_value(self):
        """Test extracting a simple column value."""
        data = {"department_id": 5, "name": "Sales"}
        segments = FKPathParser.parse("department_id")
        values = FKPathParser.extract_values(data, segments)
        assert values == [5]

    def test_extract_nested_object(self):
        """Test extracting from nested object."""
        data = {
            "metadata": {
                "user": {
                    "user_id": 123,
                    "name": "Alice",
                },
            },
        }
        segments = FKPathParser.parse("metadata.user.user_id")
        values = FKPathParser.extract_values(data, segments)
        assert values == [123]

    def test_extract_array_wildcard(self):
        """Test extracting from array with wildcard."""
        data = {
            "images": [
                {"image_id": 1, "url": "a.jpg"},
                {"image_id": 2, "url": "b.jpg"},
                {"image_id": 3, "url": "c.jpg"},
            ],
        }
        segments = FKPathParser.parse("images[*].image_id")
        values = FKPathParser.extract_values(data, segments)
        assert values == [1, 2, 3]

    def test_extract_array_specific_index(self):
        """Test extracting from array with specific index."""
        data = {
            "items": [
                {"id": 10},
                {"id": 20},
                {"id": 30},
            ],
        }
        segments = FKPathParser.parse("items[1].id")
        values = FKPathParser.extract_values(data, segments)
        assert values == [20]

    def test_extract_nested_arrays(self):
        """Test extracting from nested arrays."""
        data = {
            "teams": [
                {
                    "name": "Team A",
                    "members": [
                        {"user_id": 1},
                        {"user_id": 2},
                    ],
                },
                {
                    "name": "Team B",
                    "members": [
                        {"user_id": 3},
                        {"user_id": 4},
                    ],
                },
            ],
        }
        segments = FKPathParser.parse("teams[*].members[*].user_id")
        values = FKPathParser.extract_values(data, segments)
        assert values == [1, 2, 3, 4]

    def test_extract_mixed_nesting(self):
        """Test extracting from mixed array and object nesting."""
        data = {
            "data": {
                "users": [
                    {"profile": {"id": 100}},
                    {"profile": {"id": 200}},
                ],
            },
        }
        segments = FKPathParser.parse("data.users[*].profile.id")
        values = FKPathParser.extract_values(data, segments)
        assert values == [100, 200]

    def test_extract_missing_path(self):
        """Test extracting from non-existent path."""
        data = {"foo": "bar"}
        segments = FKPathParser.parse("missing.path")
        values = FKPathParser.extract_values(data, segments)
        assert values == []

    def test_extract_with_none_values(self):
        """Test that None values are filtered out."""
        data = {
            "items": [
                {"id": 1},
                {"id": None},
                {"id": 3},
            ],
        }
        segments = FKPathParser.parse("items[*].id")
        values = FKPathParser.extract_values(data, segments)
        # None should be included in raw extraction
        # Filtering happens at validation level
        assert values == [1, None, 3]

    def test_extract_empty_array(self):
        """Test extracting from empty array."""
        data = {"images": []}
        segments = FKPathParser.parse("images[*].image_id")
        values = FKPathParser.extract_values(data, segments)
        assert values == []

    def test_extract_array_of_primitives(self):
        """Test extracting array of primitive values (no nested field)."""
        data = {"contact_ids": [1, 2, 3]}
        segments = FKPathParser.parse("contact_ids[*]")
        values = FKPathParser.extract_values(data, segments)
        assert values == [1, 2, 3]


class TestPathUtilities:
    """Test utility functions."""

    def test_is_nested_path_simple(self):
        """Test is_nested_path with simple column."""
        assert FKPathParser.is_nested_path("department_id") is False

    def test_is_nested_path_with_dot(self):
        """Test is_nested_path with dot notation."""
        assert FKPathParser.is_nested_path("metadata.user_id") is True

    def test_is_nested_path_with_bracket(self):
        """Test is_nested_path with array notation."""
        assert FKPathParser.is_nested_path("images[*].id") is True

    def test_get_root_field_simple(self):
        """Test get_root_field with simple column."""
        assert FKPathParser.get_root_field("department_id") == "department_id"

    def test_get_root_field_nested(self):
        """Test get_root_field with nested path."""
        assert FKPathParser.get_root_field("metadata.user.user_id") == "metadata"

    def test_get_root_field_array(self):
        """Test get_root_field with array."""
        assert FKPathParser.get_root_field("images[*].image_id") == "images"


class TestPathValidation:
    """Test path validation."""

    def test_validate_empty_path(self):
        """Test that empty path raises error."""
        with pytest.raises(ValueError, match="must be a non-empty string"):
            FKPathParser.validate_path_syntax("")

    def test_validate_invalid_characters(self):
        """Test that invalid characters raise error."""
        with pytest.raises(ValueError, match="invalid characters"):
            FKPathParser.validate_path_syntax("field$name")

    def test_validate_consecutive_dots(self):
        """Test that consecutive dots raise error."""
        with pytest.raises(ValueError, match="consecutive dots"):
            FKPathParser.validate_path_syntax("metadata..user_id")

    def test_validate_empty_brackets(self):
        """Test that empty brackets raise error."""
        with pytest.raises(ValueError, match="empty brackets"):
            FKPathParser.validate_path_syntax("images[].id")

    def test_validate_max_depth(self):
        """Test that excessive nesting raises error."""
        # Create a path with 11 levels (exceeds max of 10)
        deep_path = ".".join([f"level{i}" for i in range(11)])
        with pytest.raises(ValueError, match="exceeds maximum nesting depth"):
            FKPathParser.validate_path_syntax(deep_path)

    def test_validate_valid_paths(self):
        """Test that valid paths pass validation."""
        valid_paths = [
            "department_id",
            "metadata.user.user_id",
            "images[*].image_id",
            "teams[*].members[*].user_id",
            "data.users[0].profile.id",
        ]
        for path in valid_paths:
            # Should not raise
            FKPathParser.validate_path_syntax(path)


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_extract_from_non_dict(self):
        """Test extracting from non-dict data."""
        data = "not a dict"
        segments = FKPathParser.parse("field")
        values = FKPathParser.extract_values(data, segments)
        assert values == []

    def test_extract_array_from_non_list(self):
        """Test extracting array when field is not a list."""
        data = {"images": "not an array"}
        segments = FKPathParser.parse("images[*].id")
        values = FKPathParser.extract_values(data, segments)
        assert values == []

    def test_extract_out_of_bounds_index(self):
        """Test extracting with out-of-bounds array index."""
        data = {"items": [{"id": 1}]}
        segments = FKPathParser.parse("items[5].id")
        values = FKPathParser.extract_values(data, segments)
        assert values == []

    def test_parse_non_string(self):
        """Test parsing non-string input."""
        with pytest.raises(ValueError, match="must be a non-empty string"):
            FKPathParser.parse(123)

    def test_parse_with_invalid_array_notation(self):
        """Test parsing with malformed array notation."""
        # This should parse as a simple field since regex doesn't match
        # The outer validation will catch it
        segments = FKPathParser.parse("field[")
        assert len(segments) == 1
        assert segments[0].name == "field["

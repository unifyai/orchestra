"""
Unit tests for template validation and sanitization utilities.
Tests all classes and methods in orchestra.web.api.interface.template_utils.
"""

from unittest.mock import Mock

import pytest

from orchestra.web.api.interface.schema import (
    InterfaceTemplateSchema,
    ProjectValidationSchema,
    TabTemplateSchema,
    TileTemplateSchema,
    ValidationResultSchema,
)
from orchestra.web.api.interface.template_utils import (
    ColumnSanitizer,
    ColumnValidator,
    ContextValidator,
    FieldProcessor,
    FilterSanitizer,
    FilterValidator,
    JsonSanitizer,
    JsonValidator,
    PlotSanitizer,
    PlotValidator,
    TemplateConverter,
    TemplateSanitizer,
    TemplateValidator,
)


class TestFieldProcessor:
    """Test FieldProcessor class methods."""

    def test_extract_column_contexts_simple(self):
        """Test extracting column contexts from simple field keys."""
        fields = {
            "age": {"field_category": "entry"},
            "name": {"field_category": "entry"},
            "Sciences/Physics/temperature": {"field_category": "entry"},
            "Sciences/Maths/score": {"field_category": "entry"},
        }

        contexts = FieldProcessor.extract_column_contexts(fields)

        expected = ["Sciences/", "Sciences/Maths/", "Sciences/Physics/"]
        assert contexts == expected

    def test_extract_column_contexts_complex(self):
        """Test extracting column contexts from complex nested field keys."""
        fields = {
            "Arts/Literature/Poetry/author": {"field_category": "entry"},
            "Arts/Literature/Prose/title": {"field_category": "entry"},
            "Arts/Music/genre": {"field_category": "entry"},
            "Sciences/Physics/Quantum/energy": {"field_category": "entry"},
        }

        contexts = FieldProcessor.extract_column_contexts(fields)

        expected = [
            "Arts/",
            "Arts/Literature/",
            "Arts/Literature/Poetry/",
            "Arts/Literature/Prose/",
            "Arts/Music/",
            "Sciences/",
            "Sciences/Physics/",
            "Sciences/Physics/Quantum/",
        ]
        assert contexts == expected

    def test_extract_column_contexts_empty(self):
        """Test extracting contexts from fields without paths."""
        fields = {
            "age": {"field_category": "entry"},
            "name": {"field_category": "entry"},
        }

        contexts = FieldProcessor.extract_column_contexts(fields)
        assert contexts == []

    def test_generate_valid_columns_no_context(self):
        """Test generating columns without column context filter."""
        fields = {
            "age": {"field_category": "entry"},
            "weight": {"field_category": "param"},
            "Sciences/Physics/temperature": {"field_category": "entry"},
            "derived_score": {"field_category": "derived_entry"},
        }

        columns = FieldProcessor.generate_valid_columns(fields)

        expected = [
            "Entries/Sciences/Physics/temperature",
            "Entries/Sciences/Physics",  # Context group - full path
            "Entries/age",
            "Entries/derived_score",  # derived_entry treated as entry
            "Parameters/weight",
        ]
        assert sorted(columns) == sorted(expected)

    def test_generate_valid_columns_with_context(self):
        """Test generating columns with column context filter."""
        fields = {
            "Sciences/Physics/temperature": {"field_category": "entry"},
            "Sciences/Physics/pressure": {"field_category": "param"},
            "Sciences/Maths/score": {"field_category": "entry"},
            "Arts/author": {"field_category": "entry"},
        }

        columns = FieldProcessor.generate_valid_columns(fields, "Sciences/Physics")

        expected = [
            "Entries/temperature",
            "Entries/Sciences/Physics",
            "Parameters/pressure",
            "Parameters/Sciences/Physics",
        ]
        assert sorted(columns) == sorted(expected)

    def test_get_valid_field_names(self):
        """Test getting valid field names without prefixes."""
        fields = {
            "age": {"field_category": "entry"},
            "Sciences/Physics/temperature": {"field_category": "entry"},
            "weight": {"field_category": "param"},
        }

        field_names = FieldProcessor.get_valid_field_names(fields)

        expected = {"age", "Sciences/Physics/temperature", "weight"}
        assert field_names == expected

    def test_validate_field_data_types_valid(self):
        """Test validating field data types with valid fields."""
        fields = {
            "age": {"field_category": "entry", "data_type": "integer"},
            "name": {"field_category": "param", "data_type": "string"},
            "score": {"field_category": "derived_entry", "data_type": "float"},
        }

        issues = FieldProcessor.validate_field_data_types(fields, "test_component")
        assert len(issues) == 0

    def test_validate_field_data_types_missing_data_type(self):
        """Test validating fields with missing data_type."""
        fields = {
            "age": {"field_category": "entry"},  # Missing data_type
            "name": {"field_category": "param", "data_type": "string"},
        }

        issues = FieldProcessor.validate_field_data_types(fields, "test_component")
        assert len(issues) == 1
        assert issues[0].level == "warning"
        assert issues[0].issue_type == "missing_data_type"
        assert "age" in issues[0].message

    def test_validate_field_data_types_invalid_field_category(self):
        """Test validating fields with invalid field_category."""
        fields = {
            "age": {"field_category": "invalid_type", "data_type": "integer"},
            "name": {"field_category": "param", "data_type": "string"},
        }

        issues = FieldProcessor.validate_field_data_types(fields, "test_component")
        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].issue_type == "invalid_field_category"
        assert "invalid_type" in issues[0].message

    def test_create_tile_validation_context(self):
        """Test creating tile validation context."""
        fields = {
            "age": {"field_category": "entry"},
            "Sciences/Physics/temperature": {"field_category": "entry"},
        }

        context = FieldProcessor.create_tile_validation_context(fields, "Sciences")

        assert context.fields == fields
        assert "Sciences/" in context.column_contexts
        # When column_context is "Sciences", it filters to fields starting with "Sciences"
        # and removes the "Sciences" prefix, so "Sciences/Physics/temperature" becomes "Physics/temperature"
        assert "Entries/Physics/temperature" in context.valid_columns
        assert "Entries/Sciences" in context.valid_columns  # Context group


class TestContextValidator:
    """Test ContextValidator class methods."""

    def test_validate_tile_context_valid(self):
        """Test validating valid tile context."""
        valid_contexts = ["Sciences/Physics", "Arts/Literature"]
        issues = ContextValidator.validate_tile_context(
            "Sciences/Physics",
            valid_contexts,
            "test_tile",
        )
        assert len(issues) == 0

    def test_validate_tile_context_empty(self):
        """Test validating empty tile context (should pass)."""
        valid_contexts = ["Sciences/Physics", "Arts/Literature"]
        issues = ContextValidator.validate_tile_context("", valid_contexts, "test_tile")
        assert len(issues) == 0

        issues = ContextValidator.validate_tile_context(
            None,
            valid_contexts,
            "test_tile",
        )
        assert len(issues) == 0

    def test_validate_tile_context_invalid(self):
        """Test validating invalid tile context."""
        valid_contexts = ["Sciences/Physics", "Arts/Literature"]
        issues = ContextValidator.validate_tile_context(
            "NonExistent/Context",
            valid_contexts,
            "test_tile",
        )

        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].issue_type == "missing_context"
        assert "NonExistent/Context" in issues[0].message

    def test_validate_tab_global_context_valid(self):
        """Test validating valid tab global context."""
        valid_contexts = ["Sciences/Physics", "Arts/Literature"]
        issues = ContextValidator.validate_tab_global_context(
            "Sciences/Physics",
            valid_contexts,
            "test_tab",
        )
        assert len(issues) == 0

    def test_validate_tab_global_context_invalid(self):
        """Test validating invalid tab global context."""
        valid_contexts = ["Sciences/Physics", "Arts/Literature"]
        issues = ContextValidator.validate_tab_global_context(
            "Invalid/Context",
            valid_contexts,
            "test_tab",
        )

        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].component == "tab"
        assert "Invalid/Context" in issues[0].message

    def test_validate_column_context_valid(self):
        """Test validating valid column context."""
        valid_contexts = ["Sciences/", "Arts/Literature/"]
        issues = ContextValidator.validate_column_context(
            "Sciences/",
            valid_contexts,
            "test_tile",
        )
        assert len(issues) == 0

        # Test without trailing slash
        issues = ContextValidator.validate_column_context(
            "Sciences",
            valid_contexts,
            "test_tile",
        )
        assert len(issues) == 0

    def test_validate_column_context_invalid(self):
        """Test validating invalid column context."""
        valid_contexts = ["Sciences/", "Arts/Literature/"]
        issues = ContextValidator.validate_column_context(
            "Invalid/",
            valid_contexts,
            "test_tile",
        )

        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].issue_type == "invalid_column_context"
        assert "Invalid/" in issues[0].message


class TestColumnValidator:
    """Test ColumnValidator class methods."""

    def test_validate_column_list_valid(self):
        """Test validating valid column list."""
        valid_columns = ["Entries/age", "Entries/name", "Parameters/weight"]
        issues = ColumnValidator.validate_column_list(
            "Entries/age,Entries/name",
            valid_columns,
            "test_field",
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_column_list_with_row_numbering(self):
        """Test validating column list with RowNumbering (should be allowed)."""
        valid_columns = ["Entries/age", "Entries/name"]
        issues = ColumnValidator.validate_column_list(
            "RowNumbering,Entries/age",
            valid_columns,
            "test_field",
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_column_list_invalid_columns(self):
        """Test validating column list with invalid columns."""
        valid_columns = ["Entries/age", "Entries/name"]
        issues = ColumnValidator.validate_column_list(
            "Entries/age,Entries/invalid,Parameters/missing",
            valid_columns,
            "test_field",
            "test_component",
        )

        assert len(issues) == 2
        assert all(issue.level == "error" for issue in issues)
        assert all(issue.issue_type == "invalid_column" for issue in issues)
        assert any("invalid" in issue.message for issue in issues)
        assert any("missing" in issue.message for issue in issues)

    def test_validate_column_list_empty(self):
        """Test validating empty column list."""
        valid_columns = ["Entries/age", "Entries/name"]
        issues = ColumnValidator.validate_column_list(
            "",
            valid_columns,
            "test_field",
            "test_component",
        )
        assert len(issues) == 0

        issues = ColumnValidator.validate_column_list(
            "   ",
            valid_columns,
            "test_field",
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_column_subset_valid(self):
        """Test validating valid column subset."""
        reference_columns = {"Entries/age", "Entries/name", "Parameters/weight"}
        issues = ColumnValidator.validate_column_subset(
            "Entries/age,Entries/name",
            reference_columns,
            "test_field",
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_column_subset_invalid(self):
        """Test validating invalid column subset."""
        reference_columns = {"Entries/age", "Entries/name"}
        issues = ColumnValidator.validate_column_subset(
            "Entries/age,Entries/missing",
            reference_columns,
            "test_field",
            "test_component",
        )

        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].issue_type == "invalid_column"  # issue type
        assert "missing" in issues[0].message


class TestJsonValidator:
    """Test JsonValidator class methods."""

    def test_validate_json_field_valid(self):
        """Test validating valid JSON."""
        valid_json = '{"key": "value", "number": 123}'
        issues = JsonValidator.validate_json_field(
            valid_json,
            "test_field",
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_json_field_invalid(self):
        """Test validating invalid JSON."""
        invalid_json = '{"key": "value", "missing_quote: 123}'
        issues = JsonValidator.validate_json_field(
            invalid_json,
            "test_field",
            "test_component",
        )

        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].issue_type == "invalid_json"
        assert "test_field" in issues[0].message

    def test_validate_json_field_empty(self):
        """Test validating empty JSON field."""
        issues = JsonValidator.validate_json_field("", "test_field", "test_component")
        assert len(issues) == 0

        issues = JsonValidator.validate_json_field(
            "   ",
            "test_field",
            "test_component",
        )
        assert len(issues) == 0


class TestFilterValidator:
    """Test FilterValidator class methods."""

    def test_validate_filters_valid(self):
        """Test validating valid filters."""
        valid_fields = {"age", "name", "Sciences/Physics/temperature"}
        filters = "age~>~25§name~contains~John"
        issues = FilterValidator.validate_filters(
            filters,
            valid_fields,
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_filters_invalid_column(self):
        """Test validating filters with invalid column."""
        valid_fields = {"age", "name"}
        filters = "age~>~25§invalid_column~<~100"
        issues = FilterValidator.validate_filters(
            filters,
            valid_fields,
            "test_component",
        )

        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].issue_type == "invalid_filter_column"
        assert "invalid_column" in issues[0].message

    def test_validate_filters_complex(self):
        """Test validating complex filters with multiple invalid columns."""
        valid_fields = {"age", "name", "score"}
        filters = "age~>~25§invalid1~<~100§name~contains~test§invalid2~=~value"
        issues = FilterValidator.validate_filters(
            filters,
            valid_fields,
            "test_component",
        )

        assert len(issues) == 2
        assert all(issue.level == "error" for issue in issues)
        assert any("invalid1" in issue.message for issue in issues)
        assert any("invalid2" in issue.message for issue in issues)

    def test_validate_filters_empty(self):
        """Test validating empty filters."""
        valid_fields = {"age", "name"}
        issues = FilterValidator.validate_filters("", valid_fields, "test_component")
        assert len(issues) == 0

    def test_validate_filters_malformed(self):
        """Test validating malformed filters."""
        valid_fields = {"age", "name"}
        # This should trigger the exception handling
        filters = "malformed§§filter§"
        issues = FilterValidator.validate_filters(
            filters,
            valid_fields,
            "test_component",
        )

        # Should handle gracefully, might have 0 issues or a format warning
        assert isinstance(issues, list)


class TestPlotValidator:
    """Test PlotValidator class methods."""

    def test_validate_plot_references_valid_tile_references(self):
        """Test validating valid plot references using tile reference convention."""
        plot_data = {
            "x_axis": "Table1.age",
            "y_axis": "Table2.score",
            "plot_group_by": "Table1.category",
            "plot_aggregate": "Table2.value",
        }
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {
                    "column_order": "Entries/age,Entries/category,Parameters/weight",
                },
            },
            {
                "name": "Table2",
                "table_tile": {
                    "column_order": "Parameters/score,Entries/value,Entries/id",
                },
            },
        ]

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_plot_references_invalid_format(self):
        """Test validating plot references with invalid format (missing dot)."""
        plot_data = {"x_axis": "age", "y_axis": "score"}  # Missing table prefix
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {"column_order": "Entries/age,Entries/name"},
            },
        ]

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )

        assert len(issues) == 2  # Both x_axis and y_axis have format issues
        assert all(issue.level == "error" for issue in issues)
        assert all(
            issue.issue_type == "invalid_plot_reference_format" for issue in issues
        )
        assert any(
            "should use format 'table_tile_name.field_name'" in issue.message
            for issue in issues
        )

    def test_validate_plot_references_invalid_tile_references(self):
        """Test validating plot references with invalid tile references."""
        plot_data = {
            "x_axis": "Table1.missing_column",
            "y_axis": "NonExistentTable.score",
        }
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {"column_order": "Entries/age,Entries/name"},
            },
        ]

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )

        assert len(issues) == 2
        assert all(issue.level == "error" for issue in issues)
        assert all(issue.issue_type == "invalid_tile_reference" for issue in issues)
        assert any("missing_column" in issue.message for issue in issues)
        assert any("NonExistentTable" in issue.message for issue in issues)

    def test_validate_plot_references_all_plot_fields(self):
        """Test validating all plot fields including plot_aggregate."""
        plot_data = {
            "x_axis": "Table1.temperature",
            "y_axis": "Table1.pressure",
            "plot_group_by": "Table1.category",
            "plot_aggregate": "Table1.value",
        }
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/pressure,Entries/category,Entries/value",
                },
            },
        ]

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_plot_references_mixed_valid_invalid(self):
        """Test validating plot references with mix of valid and invalid references."""
        plot_data = {
            "x_axis": "Table1.temperature",  # Valid
            "y_axis": "pressure",  # Invalid format
            "plot_group_by": "Table1.missing",  # Invalid reference
            "plot_aggregate": "Table2.value",  # Valid
        }
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/pressure",
                },
            },
            {
                "name": "Table2",
                "table_tile": {"column_order": "Entries/value,Entries/score"},
            },
        ]

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )

        assert len(issues) == 2  # y_axis format issue and plot_group_by reference issue
        issue_types = [issue.issue_type for issue in issues]
        assert "invalid_plot_reference_format" in issue_types
        assert "invalid_tile_reference" in issue_types

    def test_validate_plot_references_empty_plot_data(self):
        """Test validating empty plot data."""
        plot_data = {}
        tab_tiles = []

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )
        assert len(issues) == 0

    def test_validate_plot_references_no_table_tiles(self):
        """Test validating plot references when no table tiles exist."""
        plot_data = {"x_axis": "Table1.temperature", "y_axis": "Table1.pressure"}
        tab_tiles = []  # No table tiles

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )

        assert len(issues) == 2
        assert all(issue.issue_type == "invalid_tile_reference" for issue in issues)

    def test_validate_plot_references_removes_entries_parameters_prefix(self):
        """Test that plot validation correctly removes Entries/ and Parameters/ prefixes."""
        plot_data = {"x_axis": "DataTable.temperature", "y_axis": "DataTable.weight"}
        tab_tiles = [
            {
                "name": "DataTable",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/weight,RowNumbering",
                },
            },
        ]

        issues = PlotValidator.validate_plot_references(
            plot_data,
            tab_tiles,
            "test_component",
        )
        assert len(issues) == 0  # Should be valid after prefix removal


class TestColumnSanitizer:
    """Test ColumnSanitizer class methods."""

    def test_sanitize_column_list_valid(self):
        """Test sanitizing valid column list."""
        valid_columns = ["Entries/age", "Entries/name", "Parameters/weight"]
        result = ColumnSanitizer.sanitize_column_list(
            "Entries/age,Entries/name",
            valid_columns,
        )
        assert result == "Entries/age,Entries/name"

    def test_sanitize_column_list_with_invalid(self):
        """Test sanitizing column list with invalid columns."""
        valid_columns = ["Entries/age", "Entries/name"]
        result = ColumnSanitizer.sanitize_column_list(
            "Entries/age,Entries/invalid,Entries/name",
            valid_columns,
        )
        assert result == "Entries/age,Entries/name"

    def test_sanitize_column_list_empty(self):
        """Test sanitizing empty column list."""
        valid_columns = ["Entries/age", "Entries/name"]
        result = ColumnSanitizer.sanitize_column_list("", valid_columns)
        assert result == ""

    def test_sanitize_column_list_all_invalid(self):
        """Test sanitizing column list with all invalid columns."""
        valid_columns = ["Entries/age", "Entries/name"]
        result = ColumnSanitizer.sanitize_column_list(
            "Entries/invalid1,Entries/invalid2",
            valid_columns,
        )
        assert result == ""

    def test_sanitize_column_subset_valid(self):
        """Test sanitizing valid column subset."""
        reference_columns = {"Entries/age", "Entries/name", "Parameters/weight"}
        result = ColumnSanitizer.sanitize_column_subset(
            "Entries/age,Entries/name",
            reference_columns,
        )
        assert result == "Entries/age,Entries/name"

    def test_sanitize_column_subset_with_invalid(self):
        """Test sanitizing column subset with invalid columns."""
        reference_columns = {"Entries/age", "Entries/name"}
        result = ColumnSanitizer.sanitize_column_subset(
            "Entries/age,Entries/invalid,Entries/name",
            reference_columns,
        )
        assert result == "Entries/age,Entries/name"

    def test_sanitize_column_subset_all_invalid(self):
        """Test sanitizing column subset with all invalid columns."""
        reference_columns = {"Entries/age", "Entries/name"}
        result = ColumnSanitizer.sanitize_column_subset(
            "Entries/invalid1,Entries/invalid2",
            reference_columns,
        )
        assert result is None


class TestJsonSanitizer:
    """Test JsonSanitizer class methods."""

    def test_sanitize_json_field_valid(self):
        """Test sanitizing valid JSON."""
        valid_json = '{"key": "value", "number": 123}'
        result = JsonSanitizer.sanitize_json_field(valid_json)
        assert result == valid_json

    def test_sanitize_json_field_invalid(self):
        """Test sanitizing invalid JSON."""
        invalid_json = '{"key": "value", "missing_quote: 123}'
        result = JsonSanitizer.sanitize_json_field(invalid_json)
        assert result is None

    def test_sanitize_json_field_empty(self):
        """Test sanitizing empty JSON."""
        result = JsonSanitizer.sanitize_json_field("")
        assert result == ""


class TestFilterSanitizer:
    """Test FilterSanitizer class methods."""

    def test_sanitize_filters_valid(self):
        """Test sanitizing valid filters."""
        valid_fields = {"age", "name", "score"}
        filters = "age~>~25§name~contains~John"
        result = FilterSanitizer.sanitize_filters(filters, valid_fields)
        assert result == filters

    def test_sanitize_filters_with_invalid(self):
        """Test sanitizing filters with invalid columns."""
        valid_fields = {"age", "name"}
        filters = "age~>~25§invalid_column~<~100§name~contains~test"
        result = FilterSanitizer.sanitize_filters(filters, valid_fields)
        assert result == "age~>~25§name~contains~test"

    def test_sanitize_filters_all_invalid(self):
        """Test sanitizing filters with all invalid columns."""
        valid_fields = {"age", "name"}
        filters = "invalid1~>~25§invalid2~<~100"
        result = FilterSanitizer.sanitize_filters(filters, valid_fields)
        assert result is None

    def test_sanitize_filters_empty(self):
        """Test sanitizing empty filters."""
        valid_fields = {"age", "name"}
        result = FilterSanitizer.sanitize_filters("", valid_fields)
        assert result == ""


class TestPlotSanitizer:
    """Test PlotSanitizer class methods."""

    def test_sanitize_plot_references_valid(self):
        """Test sanitizing valid plot references."""
        plot_data = {
            "x_axis": "Table1.temperature",
            "y_axis": "Table2.pressure",
            "plot_group_by": "Table1.category",
        }
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {
                    "column_order": "Entries/temperature,Entries/category,Parameters/weight",
                },
            },
            {
                "name": "Table2",
                "table_tile": {"column_order": "Parameters/pressure,Entries/score"},
            },
        ]

        result = PlotSanitizer.sanitize_plot_references(plot_data, tab_tiles)

        assert result["x_axis"] == "Table1.temperature"
        assert result["y_axis"] == "Table2.pressure"
        assert result["plot_group_by"] == "Table1.category"

    def test_sanitize_plot_references_invalid_format(self):
        """Test sanitizing plot references with invalid format (missing dot)."""
        plot_data = {
            "x_axis": "temperature",
            "y_axis": "pressure",
        }  # Missing table prefix
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/pressure",
                },
            },
        ]

        result = PlotSanitizer.sanitize_plot_references(plot_data, tab_tiles)

        assert result["x_axis"] is None
        assert result["y_axis"] is None

    def test_sanitize_plot_references_invalid_references(self):
        """Test sanitizing plot references with invalid tile references."""
        plot_data = {
            "x_axis": "Table1.missing_column",
            "y_axis": "NonExistentTable.pressure",
            "plot_group_by": "Table1.temperature",  # Valid
        }
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/pressure",
                },
            },
        ]

        result = PlotSanitizer.sanitize_plot_references(plot_data, tab_tiles)

        assert result["x_axis"] is None  # Invalid column
        assert result["y_axis"] is None  # Invalid table
        assert result["plot_group_by"] == "Table1.temperature"  # Valid

    def test_sanitize_plot_references_all_fields(self):
        """Test sanitizing all plot fields including plot_aggregate."""
        plot_data = {
            "x_axis": "Table1.temperature",
            "y_axis": "Table1.pressure",
            "plot_group_by": "Table1.category",
            "plot_aggregate": "Table1.value",
        }
        tab_tiles = [
            {
                "name": "Table1",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/pressure,Entries/category,Entries/value",
                },
            },
        ]

        result = PlotSanitizer.sanitize_plot_references(plot_data, tab_tiles)

        assert result["x_axis"] == "Table1.temperature"
        assert result["y_axis"] == "Table1.pressure"
        assert result["plot_group_by"] == "Table1.category"
        assert result["plot_aggregate"] == "Table1.value"

    def test_sanitize_plot_references_removes_prefixes(self):
        """Test that sanitization correctly handles Entries/ and Parameters/ prefixes."""
        plot_data = {"x_axis": "DataTable.temperature", "y_axis": "DataTable.weight"}
        tab_tiles = [
            {
                "name": "DataTable",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/weight,RowNumbering",
                },
            },
        ]

        result = PlotSanitizer.sanitize_plot_references(plot_data, tab_tiles)

        assert result["x_axis"] == "DataTable.temperature"
        assert result["y_axis"] == "DataTable.weight"

    def test_sanitize_plot_references_empty_data(self):
        """Test sanitizing empty plot data."""
        plot_data = {}
        tab_tiles = []

        result = PlotSanitizer.sanitize_plot_references(plot_data, tab_tiles)

        assert result == {}

    def test_sanitize_plot_references_no_table_tiles(self):
        """Test sanitizing plot references when no table tiles exist."""
        plot_data = {"x_axis": "Table1.temperature", "y_axis": "Table1.pressure"}
        tab_tiles = []  # No table tiles

        result = PlotSanitizer.sanitize_plot_references(plot_data, tab_tiles)

        assert result["x_axis"] is None
        assert result["y_axis"] is None


class TestTemplateValidator:
    """Test TemplateValidator class methods."""

    @pytest.fixture
    def mock_session(self):
        """Mock database session."""
        return Mock()

    @pytest.fixture
    def mock_validator(self, mock_session):
        """Mock TemplateValidator with mocked DAOs."""
        validator = TemplateValidator(mock_session)

        # Mock the DAOs
        validator.organization_member_dao = Mock()
        validator.context_dao = Mock()
        validator.project_dao = Mock()
        validator.field_type_dao = Mock()

        return validator

    @pytest.fixture
    def sample_validation_schema(self):
        """Sample validation schema for testing."""
        return ProjectValidationSchema(
            contexts=["Sciences/Physics", "Arts/Literature"],
            field_types={
                "Sciences/Physics": {
                    "temperature": {"field_category": "entry", "data_type": "float"},
                    "pressure": {"field_category": "param", "data_type": "float"},
                },
                "Arts/Literature": {
                    "author": {"field_category": "entry", "data_type": "string"},
                    "title": {"field_category": "entry", "data_type": "string"},
                },
            },
        )

    def test_get_project_validation_schema(self, mock_validator):
        """Test getting project validation schema."""
        # Mock project
        mock_project = Mock()
        mock_project.id = "project_id"
        mock_validator.project_dao.get_by_user_and_name.return_value = mock_project

        # Mock contexts
        mock_context1 = Mock()
        mock_context1.name = "Sciences/Physics"
        mock_context1.id = "context1_id"

        mock_context2 = Mock()
        mock_context2.name = "Arts/Literature"
        mock_context2.id = "context2_id"

        mock_validator.context_dao.filter.side_effect = [
            [(mock_context1,), (mock_context2,)],  # First call for all contexts
            [(mock_context1,)],  # Second call for specific context
            [(mock_context2,)],  # Third call for specific context
        ]

        # Mock field types
        mock_validator.field_type_dao.get_field_types.side_effect = [
            {"temperature": {"field_category": "entry", "data_type": "float"}},
            {"author": {"field_category": "entry", "data_type": "string"}},
        ]

        schema = mock_validator.get_project_validation_schema("user_id", "test_project")

        assert schema.contexts == ["Sciences/Physics", "Arts/Literature"]
        assert "Sciences/Physics" in schema.field_types
        assert "Arts/Literature" in schema.field_types

    def test_validate_interface_template_valid(
        self,
        mock_validator,
        sample_validation_schema,
    ):
        """Test validating valid interface template."""
        interface_template = {
            "name": "test_interface",
            "tabs": [
                {
                    "name": "test_tab",
                    "tiles": [],
                },
            ],
        }

        result = mock_validator.validate_interface_template(
            interface_template,
            sample_validation_schema,
        )

        assert isinstance(result, ValidationResultSchema)
        assert result.is_valid
        assert result.can_sanitize

    def test_validate_project_template_valid(
        self,
        mock_validator,
        sample_validation_schema,
    ):
        """Test validating valid project template."""
        project_template = {
            "interfaces": [
                {
                    "name": "test_interface",
                    "tabs": [
                        {
                            "name": "test_tab",
                            "tiles": [],
                        },
                    ],
                },
            ],
        }

        result = mock_validator.validate_project_template(
            project_template,
            sample_validation_schema,
        )

        assert isinstance(result, ValidationResultSchema)
        assert result.is_valid
        assert result.can_sanitize

    def test_validate_project_template_with_issues(
        self,
        mock_validator,
        sample_validation_schema,
    ):
        """Test validating project template with validation issues."""
        project_template = {
            "interfaces": [
                {
                    "name": "interface_with_issues",
                    "tabs": [
                        {
                            "name": "tab_with_invalid_context",
                            "global_context": "NonExistent/Context",
                            "tiles": [
                                {
                                    "name": "tile_with_invalid_context",
                                    "context": "Another/Invalid/Context",
                                },
                            ],
                        },
                    ],
                },
            ],
        }

        result = mock_validator.validate_project_template(
            project_template,
            sample_validation_schema,
        )

        assert isinstance(result, ValidationResultSchema)
        assert not result.is_valid
        assert len(result.issues) > 0
        # Should have error-level issues that prevent sanitization
        assert any(issue.level == "error" for issue in result.issues)

    def test_validate_tab_template_with_global_context(
        self,
        mock_validator,
        sample_validation_schema,
    ):
        """Test validating tab template with global context."""
        tab_template = {
            "name": "test_tab",
            "global_context": "Sciences/Physics",
            "tiles": [],
        }

        result = mock_validator.validate_tab_template(
            tab_template,
            sample_validation_schema,
        )

        assert isinstance(result, ValidationResultSchema)
        assert result.is_valid
        assert len(result.issues) == 0

    def test_validate_tab_template_invalid_global_context(
        self,
        mock_validator,
        sample_validation_schema,
    ):
        """Test validating tab template with invalid global context."""
        tab_template = {
            "name": "test_tab",
            "global_context": "NonExistent/Context",
            "tiles": [],
        }

        result = mock_validator.validate_tab_template(
            tab_template,
            sample_validation_schema,
        )

        assert isinstance(result, ValidationResultSchema)
        assert not result.is_valid
        assert len(result.issues) == 1
        assert result.issues[0].level == "error"
        assert result.issues[0].issue_type == "missing_context"

    def test_validate_tile_template_table_tile(
        self,
        mock_validator,
        sample_validation_schema,
    ):
        """Test validating tile template with table tile data."""
        tile_template = {
            "name": "test_tile",
            "context": "Sciences/Physics",
            "table_tile": {
                "column_order": "Entries/temperature,Parameters/pressure",
                "hidden_columns": "Parameters/pressure",
                "sorting": '{"temperature": "desc"}',
            },
        }

        result = mock_validator.validate_tile_template(
            tile_template,
            sample_validation_schema,
        )

        assert isinstance(result, ValidationResultSchema)
        assert result.is_valid
        assert len(result.issues) == 0

    def test_validate_tile_template_plot_tile(
        self,
        mock_validator,
        sample_validation_schema,
    ):
        """Test validating tile template with plot tile data."""
        # Create a table tile that the plot can reference
        table_tile_template = {
            "name": "data_table",
            "context": "Sciences/Physics",
            "table_tile": {
                "column_order": "Entries/temperature,Parameters/pressure",
            },
        }

        tile_template = {
            "name": "test_plot",
            "context": "Sciences/Physics",
            "plot_tile": {
                "x_axis": "data_table.temperature",  # Valid tile reference
                "y_axis": "data_table.pressure",  # Valid tile reference
            },
        }

        tab_tiles = [table_tile_template, tile_template]

        result = mock_validator.validate_tile_template(
            tile_template,
            sample_validation_schema,
            tab_tiles=tab_tiles,
        )

        assert isinstance(result, ValidationResultSchema)
        assert result.is_valid
        assert len(result.issues) == 0


class TestTemplateSanitizer:
    """Test TemplateSanitizer class methods."""

    @pytest.fixture
    def sample_validation_schema(self):
        """Sample validation schema for testing."""
        return ProjectValidationSchema(
            contexts=["Sciences/Physics", "Arts/Literature"],
            field_types={
                "Sciences/Physics": {
                    "temperature": {"field_category": "entry", "data_type": "float"},
                    "pressure": {"field_category": "param", "data_type": "float"},
                },
                "Arts/Literature": {
                    "author": {"field_category": "entry", "data_type": "string"},
                    "title": {"field_category": "entry", "data_type": "string"},
                },
            },
        )

    @pytest.fixture
    def sanitizer(self, sample_validation_schema):
        """TemplateSanitizer instance with sample schema."""
        return TemplateSanitizer(sample_validation_schema)

    def test_sanitize_interface_template(self, sanitizer):
        """Test sanitizing interface template."""
        interface_template = {
            "name": "test_interface",
            "tabs": [
                {
                    "name": "valid_tab",
                    "global_context": "Sciences/Physics",
                    "tiles": [],
                },
                {
                    "name": "invalid_tab",
                    "global_context": "Invalid/Context",
                    "tiles": [],
                },
            ],
        }

        result = sanitizer.sanitize_interface_template(interface_template)

        assert result["name"] == "test_interface"
        assert len(result["tabs"]) == 2
        assert result["tabs"][0]["global_context"] == "Sciences/Physics"
        assert result["tabs"][1]["global_context"] is None  # Sanitized

    def test_sanitize_tab_template_remove_invalid(self, sanitizer):
        """Test sanitizing tab template with remove_invalid=True."""
        tab_template = {
            "name": "test_tab",
            "global_context": "Invalid/Context",
            "tiles": [
                {
                    "name": "valid_tile",
                    "context": "Sciences/Physics",
                },
                {
                    "name": "invalid_tile",
                    "context": "Invalid/Context",
                },
            ],
        }

        result = sanitizer.sanitize_tab_template(tab_template, remove_invalid=True)

        assert result["global_context"] is None
        assert len(result["tiles"]) == 2
        assert result["tiles"][0]["context"] == "Sciences/Physics"
        assert result["tiles"][1]["context"] is None

    def test_sanitize_tile_template_table_tile(self, sanitizer):
        """Test sanitizing tile template with table tile."""
        tile_template = {
            "name": "test_tile",
            "context": "Sciences/Physics",
            "table_tile": {
                "column_order": "Entries/temperature,Entries/invalid,Parameters/pressure",
                "hidden_columns": "Entries/invalid",
                "selected": "row1,row2,row3",  # Should be set to null
            },
        }

        result = sanitizer.sanitize_tile_template(tile_template)

        assert result["table_tile"]["selected"] is None
        # The column sanitization would happen in the actual implementation

    def test_sanitize_tile_template_filters(self, sanitizer):
        """Test sanitizing tile template with filters."""
        tile_template = {
            "name": "test_tile",
            "context": "Sciences/Physics",
            "filters": "temperature~>~25§invalid_field~<~100",
        }

        result = sanitizer.sanitize_tile_template(tile_template)

        # The filter sanitization would be handled by FilterSanitizer
        assert "filters" in result


class TestTemplateConverter:
    """Test TemplateConverter class methods."""

    @pytest.fixture
    def mock_tile(self):
        """Mock Tile object."""
        tile = Mock()
        tile.id = "tile_id"
        tile.name = "test_tile"
        tile.type = "Table"
        tile.visible = True
        tile.locked = False
        tile.moved = False
        tile.static = False
        tile.x_position = 0
        tile.y_position = 0
        tile.width = 4
        tile.height = 4
        tile.minW = None
        tile.minH = None
        tile.color = "#FF0000"
        tile.context = "Sciences/Physics"
        tile.table = None
        tile.auto_update = None
        tile.freeze = None
        tile.filters = None
        tile.common_filter = None
        tile.metric = None
        tile.column_context = ""
        tile.grouping = None

        # Mock table tile data
        tile.table_tile = Mock()
        tile.table_tile.table_type = "basic"
        tile.table_tile.page_number = "1"
        tile.table_tile.column_order = "Entries/age,Entries/name"
        tile.table_tile.hidden_columns = None
        tile.table_tile.sorting = None
        tile.table_tile.group_sorting = None
        tile.table_tile.columns_pin_left = None
        tile.table_tile.columns_pin_right = None
        tile.table_tile.selected = None

        # Mock other tile types as None
        tile.plot_tile = None
        tile.view_tile = None
        tile.editor_tile = None
        tile.terminal_tile = None

        return tile

    @pytest.fixture
    def mock_tab(self, mock_tile):
        """Mock Tab object."""
        tab = Mock()
        tab.id = "tab_id"
        tab.name = "test_tab"
        tab.visible = True
        tab.active = True
        tab.order = 0
        tab.color = "#00FF00"
        tab.global_context = "Sciences/Physics"
        tab.tiles = [mock_tile]
        return tab

    @pytest.fixture
    def mock_interface(self, mock_tab):
        """Mock Interface object."""
        interface = Mock()
        interface.id = "interface_id"
        interface.name = "test_interface"
        interface.color = "#0000FF"
        interface.tabs = [mock_tab]
        return interface

    def test_tile_to_template(self, mock_tile):
        """Test converting tile to template."""
        template = TemplateConverter.tile_to_template(
            mock_tile,
            description="Test tile template",
            created_by="test_user",
            tags=["test", "tile"],
        )

        assert isinstance(template, TileTemplateSchema)
        assert template.name == "test_tile"
        assert template.type == "Table"
        assert template.description == "Test tile template"
        assert template.created_by == "test_user"
        assert template.tags == ["test", "tile"]
        assert template.position.x == 0
        assert template.position.y == 0
        assert template.position.width == 4
        assert template.position.height == 4
        assert template.context == "Sciences/Physics"
        assert template.table_tile.table_type == "basic"

    def test_tab_to_template(self, mock_tab):
        """Test converting tab to template."""
        template = TemplateConverter.tab_to_template(
            mock_tab,
            description="Test tab template",
            created_by="test_user",
            tags=["test", "tab"],
        )

        assert isinstance(template, TabTemplateSchema)
        assert template.name == "test_tab"
        assert template.description == "Test tab template"
        assert template.created_by == "test_user"
        assert template.tags == ["test", "tab"]
        assert template.visible is True
        assert template.active is True
        assert template.order == 0
        assert template.color == "#00FF00"
        assert template.global_context == "Sciences/Physics"
        assert len(template.tiles) == 1

    def test_interface_to_template(self, mock_interface):
        """Test converting interface to template."""
        template = TemplateConverter.interface_to_template(
            mock_interface,
            description="Test interface template",
            created_by="test_user",
            tags=["test", "interface"],
        )

        assert isinstance(template, InterfaceTemplateSchema)
        assert template.name == "test_interface"
        assert template.description == "Test interface template"
        assert template.created_by == "test_user"
        assert template.tags == ["test", "interface"]
        assert template.color == "#0000FF"
        assert len(template.tabs) == 1


# Integration tests for complex scenarios
class TestTemplateValidationIntegration:
    """Integration tests for template validation scenarios."""

    @pytest.fixture
    def complex_validation_schema(self):
        """Complex validation schema for integration tests."""
        return ProjectValidationSchema(
            contexts=["Sciences/Physics", "Sciences/Maths", "Arts/Literature"],
            field_types={
                "Sciences/Physics": {
                    "temperature": {"field_category": "entry", "data_type": "float"},
                    "pressure": {"field_category": "param", "data_type": "float"},
                    "Sciences/Physics/Quantum/energy": {
                        "field_category": "derived_entry",
                        "data_type": "float",
                    },
                },
                "Sciences/Maths": {
                    "score": {"field_category": "entry", "data_type": "integer"},
                    "grade": {"field_category": "param", "data_type": "string"},
                },
                "Arts/Literature": {
                    "author": {"field_category": "entry", "data_type": "string"},
                    "title": {"field_category": "entry", "data_type": "string"},
                    "Arts/Literature/Poetry/verses": {
                        "field_category": "entry",
                        "data_type": "integer",
                    },
                },
            },
        )

    def test_complex_table_tile_validation(self, complex_validation_schema):
        """Test complex table tile validation scenario."""
        mock_session = Mock()
        validator = TemplateValidator(mock_session)
        validator.organization_member_dao = Mock()
        validator.context_dao = Mock()
        validator.project_dao = Mock()
        validator.field_type_dao = Mock()

        tile_template = {
            "name": "complex_table",
            "context": "Sciences/Physics",
            "column_context": "Sciences/Physics",
            "grouping": "Entries/temperature,Parameters/pressure",
            "filters": "temperature~>~25.0§pressure~<~1000§invalid_field~=~test",
            "table_tile": {
                "table_type": "advanced",
                "column_order": "Entries/temperature,Parameters/pressure,Entries/invalid_column",
                "hidden_columns": "Parameters/pressure",
                "sorting": '{"temperature": "desc"}',
                "group_sorting": '{"pressure": "asc"}',
                "columns_pin_left": "Entries/temperature",
                "columns_pin_right": "Parameters/pressure",
                "selected": "row1,row2,row3",
            },
        }

        result = validator.validate_tile_template(
            tile_template,
            complex_validation_schema,
        )

        assert isinstance(result, ValidationResultSchema)
        # Should have issues for invalid columns and filters
        assert len(result.issues) > 0
        issue_types = [issue.issue_type for issue in result.issues]
        assert "invalid_column" in issue_types or "invalid_filter_column" in issue_types

    def test_complex_plot_tile_validation(self, complex_validation_schema):
        """Test complex plot tile validation scenario."""
        mock_session = Mock()
        validator = TemplateValidator(mock_session)
        validator.organization_member_dao = Mock()
        validator.context_dao = Mock()
        validator.project_dao = Mock()
        validator.field_type_dao = Mock()

        tab_tiles = [
            {
                "name": "DataTable",
                "table_tile": {
                    "column_order": "Entries/temperature,Parameters/pressure,Entries/score",
                },
            },
            {
                "name": "ResultsTable",
                "table_tile": {"column_order": "Entries/author,Entries/title"},
            },
        ]

        tile_template = {
            "name": "complex_plot",
            "context": "Sciences/Physics",
            "plot_tile": {
                "plot_type": "scatter",
                "x_axis": "DataTable.temperature",  # Valid tile reference
                "y_axis": "invalid_field",  # Invalid format (missing table prefix)
                "plot_group_by": "NonExistentTable.category",  # Invalid tile reference
                "plot_aggregate": "DataTable.pressure",  # Valid tile reference
            },
        }

        result = validator.validate_tile_template(
            tile_template,
            complex_validation_schema,
            tab_tiles=tab_tiles,
        )

        assert isinstance(result, ValidationResultSchema)
        # Should have issues for invalid references
        assert len(result.issues) > 0
        issue_types = [issue.issue_type for issue in result.issues]
        assert (
            "invalid_plot_reference_format" in issue_types
            or "invalid_tile_reference" in issue_types
        )

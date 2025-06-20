"""
Utility functions for interface template operations.
Handles validation, sanitization, and conversion between regular objects and templates.
"""

import json
from typing import Dict, List, Optional, Set, Union

from fastapi import Request
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.db.models.orchestra_models import Interface, Tab, Tile
from orchestra.web.api.interface.schema import (
    InterfaceTemplateSchema,
    ProjectTemplateSchema,
    ProjectValidationSchema,
    TabTemplateSchema,
    TileTemplateSchema,
    TileValidationContext,
    ValidationIssue,
    ValidationResultSchema,
)


class FieldProcessor:
    """Helper class for processing field-related data."""

    @staticmethod
    def extract_column_contexts(fields: Dict[str, dict]) -> List[str]:
        """Extract column contexts from field keys using the frontend logic."""
        prefixes = []
        for key in fields.keys():
            if "/" in key:
                prefix = "/".join(key.split("/")[:-1])
                prefixes.append(prefix)

        # Generate all possible context paths
        column_contexts = set()
        for prefix in prefixes:
            if prefix:
                parts = prefix.split("/")
                context = ""
                for part in parts:
                    context += part + "/"
                    column_contexts.add(context)

        return sorted(list(column_contexts))

    @staticmethod
    def generate_valid_columns(
        fields: Dict[str, dict],
        column_context: str = "",
    ) -> List[str]:
        """Generate valid column names based on fields and column_context."""
        valid_columns = set()

        for field_key, field_info in fields.items():
            field_category = field_info.get("field_category", "entry")
            # Handle derived_entry as entry
            prefix = (
                "Entries/"
                if field_category in ["entry", "derived_entry"]
                else "Parameters/"
            )

            if column_context:
                # Filter fields by column_context
                if field_key.startswith(column_context.rstrip("/")):
                    # Extract the field name after the column_context
                    remaining_key = field_key[len(column_context.rstrip("/")) :]
                    if remaining_key.startswith("/"):
                        field_name = remaining_key[1:]
                    else:
                        field_name = field_key.split("/")[-1]

                    valid_columns.add(f"{prefix}{field_name}")
                    valid_columns.add(
                        f"{prefix}{column_context.rstrip('/')}",
                    )  # Context group
            else:
                # No column_context, include all fields
                valid_columns.add(f"{prefix}{field_key}")
                # Also add context groups
                if "/" in field_key:
                    context_part = "/".join(field_key.split("/")[:-1])
                    valid_columns.add(f"{prefix}{context_part}")

        return sorted(list(valid_columns))

    @staticmethod
    def get_valid_field_names(fields: Dict[str, dict]) -> Set[str]:
        """Get set of valid field names (without prefixes) for filter validation."""
        return set(fields.keys())

    @staticmethod
    def validate_field_data_types(
        fields: Dict[str, dict],
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate field data types are properly defined."""
        issues = []

        for field_name, field_info in fields.items():
            # Check if data_type is present and valid
            data_type_labels = ["data_type", "field_type"]
            data_type = None
            for data_type_label in data_type_labels:
                data_type = field_info.get(data_type_label)
                if data_type:
                    break
            if not data_type:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        component="field",
                        component_name=component_path,
                        issue_type="missing_data_type",
                        message=f"Field '{field_name}' missing data_type",
                        suggested_fix=f"Ensure field '{field_name}' has a valid data_type",
                    ),
                )

            # Check if field_category is present and valid
            field_category = field_info.get("field_category")
            if not field_category:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        component="field",
                        component_name=component_path,
                        issue_type="missing_field_category",
                        message=f"Field '{field_name}' missing field_category",
                        suggested_fix=f"Ensure field '{field_name}' has a valid field_category",
                    ),
                )
            elif field_category not in ["entry", "derived_entry", "param"]:
                issues.append(
                    ValidationIssue(
                        level="error",
                        component="field",
                        component_name=component_path,
                        issue_type="invalid_field_category",
                        message=f"Field '{field_name}' has invalid field_category: {field_category}",
                        suggested_fix=f"Use valid field_category: entry, derived_entry, or param",
                    ),
                )

        return issues

    @staticmethod
    def create_tile_validation_context(
        fields: Dict[str, dict],
        column_context: str = "",
    ) -> TileValidationContext:
        """Create validation context for a tile."""
        column_contexts = FieldProcessor.extract_column_contexts(fields)
        valid_columns = FieldProcessor.generate_valid_columns(fields, column_context)
        return TileValidationContext(
            fields=fields,
            column_contexts=column_contexts,
            valid_columns=valid_columns,
        )


class ContextValidator:
    """Handles context-related validations."""

    @staticmethod
    def validate_tile_context(
        tile_context: str,
        valid_contexts: List[str],
        tile_name: str,
    ) -> List[ValidationIssue]:
        """Validate tile context against project contexts."""
        if not tile_context or tile_context in valid_contexts:
            return []

        return [
            ValidationIssue(
                level="error",
                component="tile",
                component_name=tile_name,
                issue_type="missing_context",
                message=f"Context '{tile_context}' not found in target project",
                suggested_fix=f"Remove context reference or create context '{tile_context}'",
            ),
        ]

    @staticmethod
    def validate_tab_global_context(
        global_context: str,
        valid_contexts: List[str],
        tab_name: str,
    ) -> List[ValidationIssue]:
        """Validate tab global context against project contexts."""
        if not global_context or global_context in valid_contexts:
            return []

        return [
            ValidationIssue(
                level="error",
                component="tab",
                component_name=tab_name,
                issue_type="missing_context",
                message=f"Global context '{global_context}' not found in target project",
                suggested_fix=f"Remove global_context or create context '{global_context}'",
            ),
        ]

    @staticmethod
    def validate_column_context(
        column_context: str,
        valid_column_contexts: List[str],
        tile_name: str,
    ) -> List[ValidationIssue]:
        """Validate column context against valid column contexts."""
        if not column_context:
            return []

        # Allow both "question/" and "question" formats
        normalized_context = column_context.rstrip("/") + "/"
        valid_contexts = valid_column_contexts + [
            ctx.rstrip("/") for ctx in valid_column_contexts
        ]

        if (
            column_context in valid_contexts
            or normalized_context in valid_column_contexts
        ):
            return []

        return [
            ValidationIssue(
                level="error",
                component="tile",
                component_name=tile_name,
                issue_type="invalid_column_context",
                message=f"Column context '{column_context}' not found in valid contexts",
                suggested_fix=f"Use one of: {', '.join(valid_column_contexts)}",
            ),
        ]


class ColumnValidator:
    """Handles column-related validations."""

    @staticmethod
    def validate_column_list(
        column_list_str: str,
        valid_columns: List[str],
        field_name: str,
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate a comma-separated list of column names."""
        if not column_list_str or not column_list_str.strip():
            return []

        issues = []
        columns = [col.strip() for col in column_list_str.split(",") if col.strip()]

        for column in columns:
            if column == "RowNumbering":  # Special case
                continue

            if column not in valid_columns:
                issues.append(
                    ValidationIssue(
                        level="error",
                        component="table_tile",
                        component_name=component_path,
                        issue_type="invalid_column",
                        message=f"Column '{column}' in {field_name} not found in valid fields",
                        suggested_fix=f"Remove column '{column}' or ensure it exists in the project",
                    ),
                )

        return issues

    @staticmethod
    def validate_column_subset(
        column_list_str: str,
        reference_columns: Set[str],
        field_name: str,
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate that columns are a subset of reference columns (e.g., column_order)."""
        if not column_list_str or not column_list_str.strip():
            return []

        issues = []
        columns = [col.strip() for col in column_list_str.split(",") if col.strip()]

        for column in columns:
            if column not in reference_columns:
                issues.append(
                    ValidationIssue(
                        level="error",
                        component="table_tile",
                        component_name=component_path,
                        issue_type="invalid_column",
                        message=f"Column '{column}' in {field_name} not found in column_order",
                        suggested_fix=f"Remove column '{column}' or add it to column_order",
                    ),
                )

        return issues


class JsonValidator:
    """Handles JSON field validations."""

    @staticmethod
    def validate_json_field(
        json_str: str,
        field_name: str,
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate that a field contains valid JSON."""
        if not json_str or not json_str.strip():
            return []

        try:
            json.loads(json_str)
            return []
        except json.JSONDecodeError:
            return [
                ValidationIssue(
                    level="error",
                    component="table_tile",
                    component_name=component_path,
                    issue_type="invalid_json",
                    message=f"Invalid JSON in {field_name}",
                    suggested_fix=f"Fix JSON format in {field_name}",
                ),
            ]


class FilterValidator:
    """Handles filter-related validations."""

    @staticmethod
    def validate_filters(
        filters_str: str,
        valid_field_names: Set[str],
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate filters string by checking column existence."""
        if not filters_str or not filters_str.strip():
            return []

        issues = []

        try:
            # Parse filter expression: split by § then by ~ to get column names
            filter_parts = filters_str.split("§")

            for filter_part in filter_parts:
                if not filter_part.strip():
                    continue

                # Split by ~ to get [column, fn, value]
                parts = filter_part.split("~")
                if len(parts) >= 1:
                    column_name = parts[0].strip()

                    # Check if column exists in valid fields
                    if column_name and column_name not in valid_field_names:
                        issues.append(
                            ValidationIssue(
                                level="error",
                                component="tile",
                                component_name=component_path,
                                issue_type="invalid_filter_column",
                                message=f"Filter column '{column_name}' not found in valid fields",
                                suggested_fix=f"Remove filter on '{column_name}' or ensure the column exists",
                            ),
                        )

        except Exception as e:
            # If parsing fails, add a general warning
            issues.append(
                ValidationIssue(
                    level="error",
                    component="tile",
                    component_name=component_path,
                    issue_type="invalid_filter_format",
                    message=f"Invalid filter format: {str(e)}",
                    suggested_fix="Check filter syntax",
                ),
            )

        return issues


class PlotValidator:
    """Handles plot tile specific validations."""

    @staticmethod
    def validate_plot_references(
        plot_data: dict,
        tab_tiles: List[dict],
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate all plot field references to tiles in the same tab using the {table_tile_name}.{field_name} convention."""
        issues = []

        # Build map of available tile references
        available_tile_refs = set()
        for tile in tab_tiles:
            tile_name = tile.get("name", "")
            if not tile_name:
                continue

            # For table tiles, add column references
            if tile.get("table_tile") and tile["table_tile"].get("column_order"):
                column_order = tile["table_tile"]["column_order"]
                columns = [
                    col.strip() for col in column_order.split(",") if col.strip()
                ]

                for column in columns:
                    if column != "RowNumbering":
                        # Remove "Entries/" or "Parameters/" prefix for tile references
                        clean_column = column
                        if column.startswith("Entries/"):
                            clean_column = column[8:]  # Remove "Entries/"
                        elif column.startswith("Parameters/"):
                            clean_column = column[11:]  # Remove "Parameters/"

                        available_tile_refs.add(f"{tile_name}.{clean_column}")

        # Validate all plot field references using the tile reference convention
        plot_fields = ["x_axis", "y_axis", "plot_group_by", "plot_aggregate"]
        for field_name in plot_fields:
            field_value = plot_data.get(field_name)
            if field_value:
                # All plot fields should use the {table_tile_name}.{field_name} convention
                if "." not in field_value:
                    issues.append(
                        ValidationIssue(
                            level="error",
                            component="plot_tile",
                            component_name=component_path,
                            issue_type="invalid_plot_reference_format",
                            message=f"Plot {field_name} '{field_value}' should use format 'table_tile_name.field_name'",
                            suggested_fix=f"Use format like: {', '.join(sorted(available_tile_refs))}",
                        ),
                    )
                elif field_value not in available_tile_refs:
                    issues.append(
                        ValidationIssue(
                            level="error",
                            component="plot_tile",
                            component_name=component_path,
                            issue_type="invalid_tile_reference",
                            message=f"Plot {field_name} references invalid tile column '{field_value}'",
                            suggested_fix=f"Use one of: {', '.join(sorted(available_tile_refs))}",
                        ),
                    )

        return issues


class TemplateValidator:
    """Handles validation of templates against projects."""

    def __init__(self, session: Session, request: Optional[Request] = None):
        self.session = session
        self.request = request
        self.organization_member_dao = OrganizationMemberDAO(session)
        self.context_dao = ContextDAO(session)
        self.project_dao = ProjectDAO(
            session,
            self.organization_member_dao,
            self.context_dao,
        )
        self.field_type_dao = FieldTypeDAO(session)

    def get_project_validation_schema(
        self,
        user_id: str,
        project_name: str,
    ) -> ProjectValidationSchema:
        """Generate validation schema for a project by calling actual endpoints."""
        project = self.project_dao.get_by_user_and_name(
            user_id=user_id,
            name=project_name,
        )
        if not project:
            raise ValueError(f"Project {project_name} not found")

        # Get contexts using the same logic as the endpoint
        contexts = self.context_dao.filter(project_id=project.id)
        context_names = []
        for context_tuple in contexts:
            context_obj = context_tuple[0]
            if context_obj.name != "":  # Filter out default context
                context_names.append(context_obj.name)

        # Get field types for each context
        fields_by_context = {}
        for context_name in context_names:
            context_obj = self.context_dao.filter(
                project_id=project.id,
                name=context_name,
            )
            if context_obj:
                context_id = context_obj[0][0].id
                field_types = self.field_type_dao.get_field_types(
                    project.id,
                    context_id=context_id,
                    return_mutable=True,
                )
                fields_by_context[context_name] = field_types
        return ProjectValidationSchema(
            contexts=context_names,
            field_types=fields_by_context,
        )

    def validate_interface_template(
        self,
        interface_template: Union[InterfaceTemplateSchema, dict],
        validation_schema: ProjectValidationSchema,
    ) -> ValidationResultSchema:
        """Validate an interface template against a project."""
        issues = []

        # Convert to dict if it's a schema object
        if isinstance(interface_template, InterfaceTemplateSchema):
            interface_data = interface_template.model_dump()
        else:
            interface_data = interface_template

        interface_name = interface_data.get("name", "unknown")

        # Validate tabs
        for tab_template in interface_data.get("tabs", []):
            tab_result = self.validate_tab_template(
                tab_template,
                validation_schema,
                interface_name,
            )
            issues.extend(tab_result.issues)

        # Check if template can be sanitized
        can_sanitize = all(issue.level != "error" for issue in issues)

        return ValidationResultSchema(
            is_valid=len([i for i in issues if i.level == "error"]) == 0,
            issues=issues,
            can_sanitize=can_sanitize,
        )

    def validate_project_template(
        self,
        project_template: Union[ProjectTemplateSchema, dict],
        validation_schema: ProjectValidationSchema,
    ) -> ValidationResultSchema:
        """Validate a project template against a project."""
        issues = []

        # Convert to dict if it's a schema object
        if isinstance(project_template, ProjectTemplateSchema):
            project_data = project_template.model_dump()
        else:
            project_data = project_template

        # Validate each interface in the project template
        for interface_template in project_data.get("interfaces", []):
            interface_result = self.validate_interface_template(
                interface_template,
                validation_schema,
            )
            issues.extend(interface_result.issues)

        # Check if template can be sanitized
        can_sanitize = all(issue.level != "error" for issue in issues)

        return ValidationResultSchema(
            is_valid=len([i for i in issues if i.level == "error"]) == 0,
            issues=issues,
            can_sanitize=can_sanitize,
        )

    def validate_tab_template(
        self,
        tab_template: Union[TabTemplateSchema, dict],
        validation_schema: ProjectValidationSchema,
        component_path: str = "",
    ) -> ValidationResultSchema:
        """Validate a tab template against a project."""
        issues = []

        # Convert to dict if it's a schema object
        if isinstance(tab_template, TabTemplateSchema):
            tab_data = tab_template.model_dump()
        else:
            tab_data = tab_template

        tab_name = tab_data.get("name", "unknown")
        path = f"{component_path}.{tab_name}" if component_path else tab_name

        # Validate global context
        global_context = tab_data.get("global_context")
        issues.extend(
            ContextValidator.validate_tab_global_context(
                global_context,
                validation_schema.contexts,
                tab_name,
            ),
        )

        # Get all tiles for tile reference validation
        tab_tiles = tab_data.get("tiles", [])

        # Validate tiles
        for tile_template in tab_tiles:
            tile_result = self.validate_tile_template(
                tile_template,
                validation_schema,
                path,
                tab_tiles,
            )
            issues.extend(tile_result.issues)

        # Check if template can be sanitized
        can_sanitize = all(issue.level != "error" for issue in issues)

        return ValidationResultSchema(
            is_valid=len([i for i in issues if i.level == "error"]) == 0,
            issues=issues,
            can_sanitize=can_sanitize,
        )

    def validate_tile_template(
        self,
        tile_template: Union[TileTemplateSchema, dict],
        validation_schema: ProjectValidationSchema,
        component_path: str = "",
        tab_tiles: List[dict] = None,
    ) -> ValidationResultSchema:
        """Validate a tile template against a project."""
        issues = []

        # Convert to dict if it's a schema object
        if isinstance(tile_template, TileTemplateSchema):
            tile_data = tile_template.model_dump()
        else:
            tile_data = tile_template

        tile_name = tile_data.get("name", "unknown")
        path = f"{component_path}.{tile_name}" if component_path else tile_name

        # Validate tile context
        tile_context = tile_data.get("context")
        issues.extend(
            ContextValidator.validate_tile_context(
                tile_context,
                validation_schema.contexts,
                tile_name,
            ),
        )

        # Get validation context for this tile
        validation_context = self._get_tile_validation_context(
            tile_data,
            validation_schema,
        )

        # Validate field data types
        issues.extend(
            FieldProcessor.validate_field_data_types(validation_context.fields, path),
        )

        # Validate column context
        column_context = tile_data.get("column_context")
        issues.extend(
            ContextValidator.validate_column_context(
                column_context,
                validation_context.column_contexts,
                tile_name,
            ),
        )

        # Validate grouping
        grouping = tile_data.get("grouping")
        if grouping:
            issues.extend(
                ColumnValidator.validate_column_list(
                    grouping,
                    validation_context.valid_columns,
                    "grouping",
                    path,
                ),
            )

        # Validate filters (not common_filter - that's always allowed)
        filters = tile_data.get("filters")
        if filters:
            valid_field_names = FieldProcessor.get_valid_field_names(
                validation_context.fields,
            )
            issues.extend(
                FilterValidator.validate_filters(filters, valid_field_names, path),
            )

        # Validate specialized tile data
        issues.extend(
            self._validate_specialized_tiles(
                tile_data,
                validation_context,
                path,
                tab_tiles or [],
            ),
        )

        # Check if template can be sanitized
        can_sanitize = all(issue.level != "error" for issue in issues)

        return ValidationResultSchema(
            is_valid=len([i for i in issues if i.level == "error"]) == 0,
            issues=issues,
            can_sanitize=can_sanitize,
        )

    def _get_tile_validation_context(
        self,
        tile_data: dict,
        validation_schema: ProjectValidationSchema,
    ) -> TileValidationContext:
        """Get validation context for a tile based on its context and column_context."""
        tile_context = tile_data.get("context")
        column_context = tile_data.get("column_context", "")
        # Get fields for this context
        fields = {}
        if tile_context and tile_context in validation_schema.field_types:
            fields = validation_schema.field_types[tile_context]
        elif validation_schema.field_types:
            # Use first available context if no specific context
            fields = list(validation_schema.field_types.values())[0]
        return FieldProcessor.create_tile_validation_context(fields, column_context)

    def _validate_specialized_tiles(
        self,
        tile_data: dict,
        validation_context: TileValidationContext,
        component_path: str,
        tab_tiles: List[dict],
    ) -> List[ValidationIssue]:
        """Validate specialized tile data."""
        issues = []

        # Validate table tile
        if tile_data.get("table_tile"):
            issues.extend(
                self._validate_table_tile(
                    tile_data["table_tile"],
                    validation_context,
                    component_path,
                ),
            )

        # Validate plot tile
        if tile_data.get("plot_tile"):
            issues.extend(
                self._validate_plot_tile(
                    tile_data["plot_tile"],
                    validation_context,
                    component_path,
                    tab_tiles,
                ),
            )

        return issues

    def _validate_table_tile(
        self,
        table_tile_data: dict,
        validation_context: TileValidationContext,
        component_path: str,
    ) -> List[ValidationIssue]:
        """Validate table tile specific data."""
        issues = []

        # Validate column_order first as it's the ground truth
        column_order = table_tile_data.get("column_order", "")
        if column_order:
            issues.extend(
                ColumnValidator.validate_column_list(
                    column_order,
                    validation_context.valid_columns,
                    "column_order",
                    component_path,
                ),
            )

            # Extract valid columns from column_order for subsequent validations
            valid_columns_in_table = set(
                col.strip() for col in column_order.split(",") if col.strip()
            )

            # Validate fields that should be subsets of column_order
            for field_name in [
                "hidden_columns",
                "columns_pin_left",
                "columns_pin_right",
            ]:
                field_value = table_tile_data.get(field_name, "")
                if field_value:
                    issues.extend(
                        ColumnValidator.validate_column_subset(
                            field_value,
                            valid_columns_in_table,
                            field_name,
                            component_path,
                        ),
                    )

        # Validate JSON fields
        for field in ["sorting", "group_sorting", "filters", "common_filter"]:
            field_value = table_tile_data.get(field, "")
            if field_value:
                issues.extend(
                    JsonValidator.validate_json_field(
                        field_value,
                        field,
                        component_path,
                    ),
                )

        return issues

    def _validate_plot_tile(
        self,
        plot_tile_data: dict,
        validation_context: TileValidationContext,
        component_path: str,
        tab_tiles: List[dict],
    ) -> List[ValidationIssue]:
        """Validate plot tile specific data."""
        issues = []

        # Validate plot references
        issues.extend(
            PlotValidator.validate_plot_references(
                plot_tile_data,
                tab_tiles,
                component_path,
            ),
        )

        return issues


class ColumnSanitizer:
    """Handles column-related sanitizations."""

    @staticmethod
    def sanitize_column_list(column_list_str: str, valid_columns: List[str]) -> str:
        """Sanitize a comma-separated list of column names."""
        if not column_list_str or not column_list_str.strip():
            return column_list_str

        columns = [col.strip() for col in column_list_str.split(",") if col.strip()]

        # Keep only valid columns
        valid_columns_set = set(valid_columns)
        sanitized_columns = []
        for column in columns:
            if column == "RowNumbering" or column in valid_columns_set:
                sanitized_columns.append(column)

        return ",".join(sanitized_columns)

    @staticmethod
    def sanitize_column_subset(
        column_list_str: str,
        reference_columns: Set[str],
    ) -> Optional[str]:
        """Sanitize columns to be a subset of reference columns."""
        if not column_list_str or not column_list_str.strip():
            return column_list_str

        columns = [col.strip() for col in column_list_str.split(",") if col.strip()]
        valid_columns = [col for col in columns if col in reference_columns]

        return ",".join(valid_columns) if valid_columns else None


class JsonSanitizer:
    """Handles JSON field sanitizations."""

    @staticmethod
    def sanitize_json_field(json_str: str) -> Optional[str]:
        """Sanitize JSON field by validating and returning None if invalid."""
        if not json_str or not json_str.strip():
            return json_str

        try:
            json.loads(json_str)  # Validate JSON
            return json_str
        except json.JSONDecodeError:
            return None


class FilterSanitizer:
    """Handles filter-related sanitizations."""

    @staticmethod
    def sanitize_filters(
        filters_str: str,
        valid_field_names: Set[str],
    ) -> Optional[str]:
        """Sanitize filters string by removing invalid column references."""
        if not filters_str or not filters_str.strip():
            return filters_str

        try:
            # Parse and rebuild filter expression
            filter_parts = filters_str.split("§")
            valid_filters = []

            for filter_part in filter_parts:
                if not filter_part.strip():
                    continue

                # Split by ~ to get [column, fn, value]
                parts = filter_part.split("~")
                if len(parts) >= 1:
                    column_name = parts[0].strip()

                    # Keep filter if column exists
                    if column_name in valid_field_names:
                        valid_filters.append(filter_part)

            return "§".join(valid_filters) if valid_filters else None

        except Exception:
            # If parsing fails, return None to remove invalid filter
            return None


class PlotSanitizer:
    """Handles plot-related sanitizations."""

    @staticmethod
    def sanitize_plot_references(plot_data: dict, tab_tiles: List[dict]) -> dict:
        """Sanitize plot field references to ensure they follow the {table_tile_name}.{field_name} convention."""
        sanitized = plot_data.copy()

        # Build map of available tile references
        available_tile_refs = set()
        for tile in tab_tiles:
            tile_name = tile.get("name", "")
            if not tile_name:
                continue

            # For table tiles, add column references
            if tile.get("table_tile") and tile["table_tile"].get("column_order"):
                column_order = tile["table_tile"]["column_order"]
                columns = [
                    col.strip() for col in column_order.split(",") if col.strip()
                ]

                for column in columns:
                    if column != "RowNumbering":
                        # Remove "Entries/" or "Parameters/" prefix for tile references
                        clean_column = column
                        if column.startswith("Entries/"):
                            clean_column = column[8:]  # Remove "Entries/"
                        elif column.startswith("Parameters/"):
                            clean_column = column[11:]  # Remove "Parameters/"

                        available_tile_refs.add(f"{tile_name}.{clean_column}")

        # Sanitize all plot field references
        plot_fields = ["x_axis", "y_axis", "plot_group_by", "plot_aggregate"]
        for field_name in plot_fields:
            field_value = sanitized.get(field_name)
            if field_value:
                # Remove invalid references (must contain . and be in available refs)
                if "." not in field_value or field_value not in available_tile_refs:
                    sanitized[field_name] = None

        return sanitized


class TemplateSanitizer:
    """Handles sanitization of templates for specific projects."""

    def __init__(self, validation_schema: ProjectValidationSchema):
        self.validation_schema = validation_schema

    def sanitize_project_template(
        self,
        project_template: Union[ProjectTemplateSchema, dict],
        remove_invalid: bool = True,
        preserve_structure: bool = True,
    ) -> dict:
        """Sanitize a project template for a project."""
        # Convert to dict if it's a schema object
        if isinstance(project_template, ProjectTemplateSchema):
            sanitized = project_template.model_dump()
        else:
            sanitized = project_template.copy()

        # Sanitize interfaces
        sanitized_interfaces = []
        for interface_template in sanitized.get("interfaces", []):
            sanitized_interface = self.sanitize_interface_template(
                interface_template,
                remove_invalid,
                preserve_structure,
            )
            if sanitized_interface:  # Only include if not completely removed
                sanitized_interfaces.append(sanitized_interface)

        sanitized["interfaces"] = sanitized_interfaces
        return sanitized

    def sanitize_interface_template(
        self,
        interface_template: Union[InterfaceTemplateSchema, dict],
        remove_invalid: bool = True,
        preserve_structure: bool = True,
    ) -> dict:
        """Sanitize an interface template for a project."""
        # Convert to dict if it's a schema object
        if isinstance(interface_template, InterfaceTemplateSchema):
            sanitized = interface_template.model_dump()
        else:
            sanitized = interface_template.copy()

        # Sanitize tabs
        sanitized_tabs = []
        for tab_template in sanitized.get("tabs", []):
            sanitized_tab = self.sanitize_tab_template(
                tab_template,
                remove_invalid,
                preserve_structure,
            )
            if sanitized_tab:  # Only include if not completely removed
                sanitized_tabs.append(sanitized_tab)

        sanitized["tabs"] = sanitized_tabs
        return sanitized

    def sanitize_tab_template(
        self,
        tab_template: Union[TabTemplateSchema, dict],
        remove_invalid: bool = True,
        preserve_structure: bool = True,
    ) -> Optional[dict]:
        """Sanitize a tab template for a project."""
        # Convert to dict if it's a schema object
        if isinstance(tab_template, TabTemplateSchema):
            sanitized = tab_template.model_dump()
        else:
            sanitized = tab_template.copy()

        # Sanitize global context
        global_context = sanitized.get("global_context")
        if global_context and global_context not in self.validation_schema.contexts:
            if remove_invalid:
                sanitized["global_context"] = None
            elif not preserve_structure:
                return None

        # Get all tiles for plot reference validation
        tab_tiles = sanitized.get("tiles", [])

        # Sanitize tiles
        sanitized_tiles = []
        for tile_template in tab_tiles:
            sanitized_tile = self.sanitize_tile_template(
                tile_template,
                remove_invalid,
                preserve_structure,
                tab_tiles,
            )
            if sanitized_tile:  # Only include if not completely removed
                sanitized_tiles.append(sanitized_tile)

        sanitized["tiles"] = sanitized_tiles
        return sanitized

    def sanitize_tile_template(
        self,
        tile_template: Union[TileTemplateSchema, dict],
        remove_invalid: bool = True,
        preserve_structure: bool = True,
        tab_tiles: List[dict] = None,
    ) -> Optional[dict]:
        """Sanitize a tile template for a project."""
        # Convert to dict if it's a schema object

        if isinstance(tile_template, TileTemplateSchema):
            sanitized = tile_template.model_dump()
        else:
            sanitized = tile_template.copy()
        # Sanitize tile context
        tile_context = sanitized.get("context")
        if tile_context and tile_context not in self.validation_schema.contexts:
            if remove_invalid:
                sanitized["context"] = None
            elif not preserve_structure:
                return None

        # STEP 1: Get initial validation context to check column_context validity
        initial_validation_context = self._get_tile_validation_context(sanitized)

        # STEP 2: Track original column_context and sanitize it
        original_column_context = sanitized.get("column_context")
        self._sanitize_tile_column_context(
            sanitized,
            initial_validation_context,
            remove_invalid,
        )
        sanitized_column_context = sanitized.get("column_context")

        # STEP 3: Determine which validation context to use for other field sanitization
        # If column_context was changed during sanitization, use fallback context
        # Otherwise, use the original validation context
        if original_column_context != sanitized_column_context:
            # Column context was sanitized - use fallback context with all valid columns
            validation_context = self._get_fallback_validation_context(sanitized)
        else:
            # Column context is valid - use original validation context
            validation_context = initial_validation_context

        # STEP 4: Now sanitize other fields using the appropriate validation context
        # Sanitize grouping
        self._sanitize_tile_grouping(sanitized, validation_context, remove_invalid)

        # Sanitize filters (not common_filter)
        self._sanitize_tile_filters(sanitized, validation_context, remove_invalid)

        # Sanitize specialized tile data
        self._sanitize_specialized_tiles(
            sanitized,
            validation_context,
            remove_invalid,
            preserve_structure,
            tab_tiles or [],
        )

        return sanitized

    def _get_tile_validation_context(self, tile_data: dict) -> TileValidationContext:
        """Get validation context for a tile during sanitization."""
        tile_context = tile_data.get("context")
        column_context = tile_data.get("column_context", "")

        # Get fields for this context
        fields = {}
        if tile_context and tile_context in self.validation_schema.field_types:
            fields = self.validation_schema.field_types[tile_context]
        elif self.validation_schema.field_types:
            fields = list(self.validation_schema.field_types.values())[0]

        return FieldProcessor.create_tile_validation_context(fields, column_context)

    def _sanitize_tile_column_context(
        self,
        sanitized: dict,
        validation_context: TileValidationContext,
        remove_invalid: bool,
    ):
        """Sanitize tile column context."""
        column_context = sanitized.get("column_context")
        if not column_context:
            return

        normalized_context = column_context.rstrip("/") + "/"
        valid_contexts = validation_context.column_contexts + [
            ctx.rstrip("/") for ctx in validation_context.column_contexts
        ]

        if (
            column_context not in valid_contexts
            and normalized_context not in validation_context.column_contexts
        ):
            if remove_invalid:
                sanitized["column_context"] = None

    def _sanitize_tile_grouping(
        self,
        sanitized: dict,
        validation_context: TileValidationContext,
        remove_invalid: bool,
    ):
        """Sanitize tile grouping field."""
        grouping = sanitized.get("grouping")
        if grouping and remove_invalid:
            sanitized["grouping"] = ColumnSanitizer.sanitize_column_list(
                grouping,
                validation_context.valid_columns,
            )

    def _sanitize_tile_filters(
        self,
        sanitized: dict,
        validation_context: TileValidationContext,
        remove_invalid: bool,
    ):
        """Sanitize tile filters field."""
        filters = sanitized.get("filters")
        if filters and remove_invalid:
            valid_field_names = FieldProcessor.get_valid_field_names(
                validation_context.fields,
            )
            sanitized["filters"] = FilterSanitizer.sanitize_filters(
                filters,
                valid_field_names,
            )

    def _sanitize_specialized_tiles(
        self,
        sanitized: dict,
        validation_context: TileValidationContext,
        remove_invalid: bool,
        preserve_structure: bool,
        tab_tiles: List[dict],
    ):
        """Sanitize specialized tile data."""
        # Sanitize table tile
        if sanitized.get("table_tile"):
            sanitized_table_tile = self._sanitize_table_tile(
                sanitized["table_tile"],
                validation_context,
                remove_invalid,
            )
            if sanitized_table_tile:
                sanitized["table_tile"] = sanitized_table_tile
            elif remove_invalid:
                sanitized["table_tile"] = None

        # Sanitize plot tile
        if sanitized.get("plot_tile"):
            sanitized_plot_tile = self._sanitize_plot_tile(
                sanitized["plot_tile"],
                validation_context,
                remove_invalid,
                tab_tiles,
            )
            if sanitized_plot_tile:
                sanitized["plot_tile"] = sanitized_plot_tile
            elif remove_invalid:
                sanitized["plot_tile"] = None

    def _sanitize_table_tile(
        self,
        table_tile_data: dict,
        validation_context: TileValidationContext,
        remove_invalid: bool,
    ) -> Optional[dict]:
        """Sanitize table tile specific data."""
        sanitized = table_tile_data.copy()

        # Always sanitize selected field
        if "selected" in sanitized:
            sanitized["selected"] = None

        # Sanitize column_order first
        if sanitized.get("column_order") and remove_invalid:
            sanitized["column_order"] = ColumnSanitizer.sanitize_column_list(
                sanitized["column_order"],
                validation_context.valid_columns,
            )

        # Get valid columns from sanitized column_order
        valid_columns_in_table = set()
        if sanitized.get("column_order"):
            valid_columns_in_table = set(
                col.strip()
                for col in sanitized["column_order"].split(",")
                if col.strip()
            )

        # Sanitize fields that should be subsets of column_order
        for field_name in ["hidden_columns", "columns_pin_left", "columns_pin_right"]:
            if sanitized.get(field_name) and remove_invalid:
                sanitized[field_name] = ColumnSanitizer.sanitize_column_subset(
                    sanitized[field_name],
                    valid_columns_in_table,
                )

        # Sanitize JSON fields
        for field in ["sorting", "group_sorting", "filters", "common_filter"]:
            if sanitized.get(field) and remove_invalid:
                sanitized[field] = JsonSanitizer.sanitize_json_field(sanitized[field])

        return sanitized

    def _sanitize_plot_tile(
        self,
        plot_tile_data: dict,
        validation_context: TileValidationContext,
        remove_invalid: bool,
        tab_tiles: List[dict],
    ) -> Optional[dict]:
        """Sanitize plot tile specific data."""
        sanitized = plot_tile_data.copy()

        if remove_invalid:
            # Use PlotSanitizer to sanitize plot references using tile reference convention
            sanitized = PlotSanitizer.sanitize_plot_references(sanitized, tab_tiles)

        return sanitized

    def _get_fallback_validation_context(
        self,
        tile_data: dict,
    ) -> TileValidationContext:
        """Get validation context for sanitization purposes - uses empty column_context to get all valid columns."""
        tile_context = tile_data.get("context")

        # Get fields for this context
        fields = {}
        if tile_context and tile_context in self.validation_schema.field_types:
            fields = self.validation_schema.field_types[tile_context]
        elif self.validation_schema.field_types:
            fields = list(self.validation_schema.field_types.values())[0]

        # Create validation context with empty column_context to get ALL valid columns
        return FieldProcessor.create_tile_validation_context(fields, "")


class TemplateConverter:
    """Converts between database objects and template schemas."""

    @staticmethod
    def tile_to_template(
        tile: Tile,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> TileTemplateSchema:
        """Convert a Tile database object to a template schema."""
        # Create base tile template
        tile_data = {
            "name": tile.name,
            "position": {
                "x": tile.x_position,
                "y": tile.y_position,
                "width": tile.width,
                "height": tile.height,
            },
            "type": tile.type,
            "minW": tile.minW,
            "minH": tile.minH,
            "visible": tile.visible,
            "locked": tile.locked,
            "moved": tile.moved,
            "static": tile.static,
            "color": tile.color,
            "context": tile.context,
            "table": tile.table,
            "auto_update": tile.auto_update,
            "freeze": tile.freeze,
            "filters": tile.filters,
            "common_filter": tile.common_filter,
            "metric": tile.metric,
            "column_context": tile.column_context,
            "grouping": tile.grouping,
        }

        # Add specialized tile data
        if tile.table_tile:
            tile_data["table_tile"] = {
                "table_type": tile.table_tile.table_type,
                "page_number": tile.table_tile.page_number,
                "column_order": tile.table_tile.column_order,
                "hidden_columns": tile.table_tile.hidden_columns,
                "sorting": tile.table_tile.sorting,
                "group_sorting": tile.table_tile.group_sorting,
                "columns_pin_left": tile.table_tile.columns_pin_left,
                "columns_pin_right": tile.table_tile.columns_pin_right,
                "selected": tile.table_tile.selected,
            }

        if tile.plot_tile:
            tile_data["plot_tile"] = {
                "plot_type": tile.plot_tile.plot_type,
                "plot_scale_x": tile.plot_tile.plot_scale_x,
                "plot_scale_y": tile.plot_tile.plot_scale_y,
                "plot_aggregate": tile.plot_tile.plot_aggregate,
                "x_axis": tile.plot_tile.x_axis,
                "y_axis": tile.plot_tile.y_axis,
                "plot_group_by": tile.plot_tile.plot_group_by,
                "plot_group_by_colors": tile.plot_tile.plot_group_by_colors,
                "bin_count": tile.plot_tile.bin_count,
                "regression_line": tile.plot_tile.regression_line,
            }

        if tile.view_tile:
            tile_data["view_tile"] = {
                "base_index": tile.view_tile.base_index,
            }

        if tile.editor_tile:
            tile_data["editor_tile"] = {
                "file_name": tile.editor_tile.file_name,
                "file_type": tile.editor_tile.file_type,
                "content": tile.editor_tile.content,
            }

        if tile.terminal_tile:
            tile_data["terminal_tile"] = {
                "shell_type": tile.terminal_tile.shell_type,
            }

        return TileTemplateSchema(
            **tile_data,
            description=description,
            created_by=created_by,
            tags=tags or [],
        )

    @staticmethod
    def tab_to_template(
        tab: Tab,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> TabTemplateSchema:
        """Convert a Tab database object to a template schema."""
        tiles = [
            TemplateConverter.tile_to_template(tile, description, created_by, tags)
            for tile in tab.tiles
        ]

        return TabTemplateSchema(
            name=tab.name,
            visible=tab.visible,
            active=tab.active,
            order=tab.order,
            global_context=tab.global_context,
            color=tab.color,
            tiles=tiles,
            description=description,
            created_by=created_by,
            tags=tags or [],
        )

    @staticmethod
    def interface_to_template(
        interface: Interface,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> InterfaceTemplateSchema:
        """Convert an Interface database object to a template schema."""
        tabs = [
            TemplateConverter.tab_to_template(tab, description, created_by, tags)
            for tab in interface.tabs
        ]

        # Find active tab name from active_tab_id
        active_tab_name = None
        if interface.active_tab_id:
            for tab in interface.tabs:
                if str(tab.id) == str(interface.active_tab_id):
                    active_tab_name = tab.name
                    break

        return InterfaceTemplateSchema(
            name=interface.name,
            tabs=tabs,
            active_tab_name=active_tab_name,
            color=interface.color,
            description=description,
            created_by=created_by,
            tags=tags or [],
        )

    @staticmethod
    def template_to_tile(
        tile_template: TileTemplateSchema,
        tab_id: str,
        tile_dao: TileDAO,
        new_tile_name: Optional[str] = None,
        overwrite_existing: bool = False,
    ) -> tuple[Tile, List[str]]:
        """Convert a tile template to a database Tile object.

        Args:
            tile_template: The tile template to convert
            tab_id: ID of the tab to create the tile in
            tile_dao: DAO for tile operations
            new_tile_name: Optional override for the tile name
            overwrite_existing: Whether to overwrite existing tiles with same name

        Returns:
            tuple: (created_tile, warnings)
        """
        warnings = []

        # Separate original and final names
        original_name = tile_template.name
        final_name = new_tile_name or original_name

        # Handle position - it might be a dict or an object
        position = tile_template.position or {"x": 0, "y": 0, "width": 4, "height": 4}

        # Check for existing tile using the FINAL name (where we want to create/overwrite)
        existing = tile_dao.get_by_tab_and_name(
            tab_id=tab_id,
            name=final_name,
            is_checkpoint=False,
        )

        if existing:
            if not overwrite_existing:
                raise ValueError(
                    f"Tile with name {final_name} already exists in this tab",
                )
            else:
                # Delete existing tile if overwriting
                tile_dao.delete_tile(id=str(existing.id))
                warnings.append(f"Existing tile '{final_name}' was overwritten")

        # Create the tile with the final name
        tile = tile_dao.create_tile(
            tab_id=tab_id,
            name=final_name or "Imported Tile",
            type=tile_template.type,
            x_position=(
                position.get("x", 0)
                if isinstance(position, dict)
                else getattr(position, "x", 0)
            ),
            y_position=(
                position.get("y", 0)
                if isinstance(position, dict)
                else getattr(position, "y", 0)
            ),
            width=(
                position.get("width", 4)
                if isinstance(position, dict)
                else getattr(position, "width", 4)
            ),
            height=(
                position.get("height", 4)
                if isinstance(position, dict)
                else getattr(position, "height", 4)
            ),
            minW=tile_template.minW,
            minH=tile_template.minH,
            visible=tile_template.visible
            if tile_template.visible is not None
            else True,
            locked=tile_template.locked if tile_template.locked is not None else False,
            moved=tile_template.moved if tile_template.moved is not None else False,
            static=tile_template.static if tile_template.static is not None else False,
            color=tile_template.color,
            context=tile_template.context,
            table=tile_template.table,
            auto_update=tile_template.auto_update,
            freeze=tile_template.freeze,
            filters=tile_template.filters,
            common_filter=tile_template.common_filter,
            metric=tile_template.metric,
            column_context=tile_template.column_context,
            grouping=tile_template.grouping,
            is_checkpoint=False,
            # Pass specialized tile data as dictionaries
            table_tile=(
                tile_template.table_tile.model_dump()
                if tile_template.table_tile
                else None
            ),
            plot_tile=(
                tile_template.plot_tile.model_dump()
                if tile_template.plot_tile
                else None
            ),
            view_tile=(
                tile_template.view_tile.model_dump()
                if tile_template.view_tile
                else None
            ),
            editor_tile=(
                tile_template.editor_tile.model_dump()
                if tile_template.editor_tile
                else None
            ),
            terminal_tile=(
                tile_template.terminal_tile.model_dump()
                if tile_template.terminal_tile
                else None
            ),
        )
        return tile, warnings

    @staticmethod
    def template_to_tab(
        tab_template: TabTemplateSchema,
        interface_id: str,
        tab_dao: TabDAO,
        tile_dao: TileDAO,
        new_tab_name: Optional[str] = None,
        overwrite_existing: bool = False,
    ) -> tuple[Tab, List[str]]:
        """Convert a tab template to a database Tab object.

        Args:
            tab_template: The tab template to convert
            interface_id: ID of the interface to create the tab in
            tab_dao: DAO for tab operations
            tile_dao: DAO for tile operations
            new_tab_name: Optional override for the tab name
            overwrite_existing: Whether to overwrite existing tabs with same name

        Returns:
            tuple: (created_tab, warnings)
        """
        warnings = []

        # Separate original and final names
        original_name = tab_template.name
        final_name = new_tab_name or original_name

        # Check for existing tab using the FINAL name (where we want to create/overwrite)
        existing = tab_dao.get_by_interface_and_name(
            interface_id=interface_id,
            name=final_name,
            is_checkpoint=False,
        )

        if existing:
            if not overwrite_existing:
                raise ValueError(
                    f"Tab with name {final_name} already exists in this interface",
                )
            else:
                # Delete existing tab if overwriting
                tab_dao.delete_tab(id=str(existing.id))
                warnings.append(f"Existing tab '{final_name}' was overwritten")

        # Create the tab with the final name
        tab = tab_dao.create_tab(
            interface_id=interface_id,
            name=final_name or "Imported Tab",
            visible=tab_template.visible if tab_template.visible is not None else True,
            active=tab_template.active if tab_template.active is not None else False,
            order=tab_template.order if tab_template.order is not None else 0,
            global_context=tab_template.global_context,
            color=tab_template.color,
            is_checkpoint=False,
        )

        # Create tiles for this tab
        for tile_template in tab_template.tiles or []:
            tile, tile_warnings = TemplateConverter.template_to_tile(
                tile_template=tile_template,
                tab_id=str(tab.id),
                tile_dao=tile_dao,
                overwrite_existing=overwrite_existing,
            )
            warnings.extend(tile_warnings)

        return tab, warnings

    @staticmethod
    def template_to_interface(
        interface_template: InterfaceTemplateSchema,
        project_id: str,
        interface_dao: InterfaceDAO,
        tab_dao: TabDAO,
        tile_dao: TileDAO,
        new_interface_name: Optional[str] = None,
        overwrite_existing: bool = False,
    ) -> tuple[Interface, List[str]]:
        """Convert an interface template to a database Interface object.

        Returns:
            tuple: (created_interface, warnings)
        """
        # Separate original and final names
        original_name = interface_template.name
        final_name = new_interface_name or original_name
        warnings = []

        # Check for existing interface using the FINAL name (where we want to create/overwrite)
        existing = interface_dao.get_by_project_and_name(
            project_id=project_id,
            name=final_name,
            is_checkpoint=False,
        )

        if existing:
            if not overwrite_existing:
                raise ValueError(
                    f"Interface with name {final_name} already exists in this project",
                )
            else:
                # Delete existing interface if overwriting
                interface_dao.delete_interface(id=str(existing.id))
                warnings.append(f"Existing interface '{final_name}' was overwritten")

        # Create the interface with the final name
        interface = interface_dao.create_interface(
            name=final_name,
            project_id=project_id,
            color=interface_template.color,
            is_checkpoint=False,
        )

        # Create tabs and tiles
        active_tab_id = None
        for tab_template in interface_template.tabs or []:
            tab, tab_warnings = TemplateConverter.template_to_tab(
                tab_template=tab_template,
                interface_id=str(interface.id),
                tab_dao=tab_dao,
                tile_dao=tile_dao,
                overwrite_existing=overwrite_existing,
            )
            warnings.extend(tab_warnings)

            # Set active tab if this matches the active tab name
            if (
                interface_template.active_tab_name
                and tab.name == interface_template.active_tab_name
            ):
                active_tab_id = str(tab.id)

        # Update active tab if specified
        if active_tab_id:
            interface_dao.update_interface(
                id=str(interface.id),
                active_tab_id=active_tab_id,
            )

        return interface, warnings

    @staticmethod
    def template_to_project_interfaces(
        project_template: ProjectTemplateSchema,
        project_id: str,
        interface_dao: InterfaceDAO,
        tab_dao: TabDAO,
        tile_dao: TileDAO,
        interface_name_prefix: Optional[str] = None,
        overwrite_existing: bool = False,
    ) -> tuple[List[Interface], List[str]]:
        """Convert a project template to database Interface objects.

        Returns:
            tuple: (created_interfaces, warnings)
        """
        created_interfaces = []
        warnings = []

        for interface_template in project_template.interfaces or []:
            # Determine interface name with optional prefix
            interface_name = interface_template.name or "Imported Interface"
            if interface_name_prefix:
                interface_name = f"{interface_name_prefix}{interface_name}"

            try:
                interface, interface_warnings = TemplateConverter.template_to_interface(
                    interface_template=interface_template,
                    project_id=project_id,
                    interface_dao=interface_dao,
                    tab_dao=tab_dao,
                    tile_dao=tile_dao,
                    new_interface_name=interface_name,
                    overwrite_existing=overwrite_existing,
                )
                created_interfaces.append(interface)
                warnings.extend(interface_warnings)
            except ValueError as e:
                if not overwrite_existing and "already exists" in str(e):
                    # Generate warning for skipped interface
                    warnings.append(
                        f"Interface '{interface_name}' already exists and was skipped (overwrite_existing=False)",
                    )
                    continue
                else:
                    raise

        return created_interfaces, warnings

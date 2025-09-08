"""
Utility functions for interface template operations.
Handles validation, sanitization, and conversion between regular objects and templates.
"""

import json
from typing import List, Optional, Union

from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.models.orchestra_models import Interface, Tab, Tile
from orchestra.web.api.interface.schema import (
    InterfaceTemplateSchema,
    ProjectValidationSchema,
    TabTemplateSchema,
    TileTemplateSchema,
    ValidationIssue,
    ValidationResultSchema,
)


class TemplateValidator:
    """Handles validation of templates against projects."""

    def __init__(self, session: Session):
        self.session = session
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
        """Generate validation schema for a project."""
        project = self.project_dao.get_by_user_and_name(
            user_id=user_id,
            name=project_name,
        )
        if not project:
            raise ValueError(f"Project {project_name} not found")

        # Get contexts
        contexts = self.context_dao.filter(project_id=project.id)
        context_names = [ctx[0].name for ctx in contexts]

        # Get field types grouped by context
        field_types = {}
        for ctx_tuple in contexts:
            ctx = ctx_tuple[0]
            ctx_field_types = self.field_type_dao.filter(context_id=ctx.id)
            field_types[ctx.name] = {
                ft[0].field_name: ft[0].field_type for ft in ctx_field_types
            }

        # For now, we'll use basic validation schema
        # In a full implementation, you'd also get actual table/column info
        return ProjectValidationSchema(
            contexts=context_names,
            tables=[],  # Would need to be populated from actual data sources
            columns={},  # Would need to be populated from actual data sources
            field_types=field_types,
        )

    def validate_tile_template(
        self,
        tile_template: Union[TileTemplateSchema, dict],
        validation_schema: ProjectValidationSchema,
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate a tile template against a project."""
        issues = []

        # Convert to dict if it's a schema object
        if isinstance(tile_template, TileTemplateSchema):
            tile_data = tile_template.model_dump()
        else:
            tile_data = tile_template

        tile_name = tile_data.get("name", "unknown")
        path = f"{component_path}.{tile_name}" if component_path else tile_name

        # Check context references
        if tile_data.get("context"):
            if tile_data["context"] not in validation_schema.contexts:
                issues.append(
                    ValidationIssue(
                        level="error",
                        component="tile",
                        component_name=tile_name,
                        issue_type="missing_context",
                        message=f"Context '{tile_data['context']}' not found in target project",
                        suggested_fix=f"Remove context reference or create context '{tile_data['context']}'",
                    ),
                )

        # Check table references
        if tile_data.get("table"):
            if (
                validation_schema.tables
                and tile_data["table"] not in validation_schema.tables
            ):
                issues.append(
                    ValidationIssue(
                        level="warning",
                        component="tile",
                        component_name=tile_name,
                        issue_type="missing_table",
                        message=f"Table '{tile_data['table']}' not found in target project",
                        suggested_fix=f"Remove table reference or ensure table '{tile_data['table']}' exists",
                    ),
                )

        # Validate specialized tile data
        for tile_type in [
            "table_tile",
            "plot_tile",
            "view_tile",
            "editor_tile",
            "terminal_tile",
        ]:
            if tile_data.get(tile_type):
                issues.extend(
                    self._validate_specialized_tile(
                        tile_data[tile_type],
                        tile_type,
                        validation_schema,
                        path,
                    ),
                )

        return issues

    def validate_tab_template(
        self,
        tab_template: Union[TabTemplateSchema, dict],
        validation_schema: ProjectValidationSchema,
        component_path: str = "",
    ) -> List[ValidationIssue]:
        """Validate a tab template against a project."""
        issues = []

        # Convert to dict if it's a schema object
        if isinstance(tab_template, TabTemplateSchema):
            tab_data = tab_template.model_dump()
        else:
            tab_data = tab_template

        tab_name = tab_data.get("name", "unknown")
        path = f"{component_path}.{tab_name}" if component_path else tab_name

        # Check global context
        if tab_data.get("context"):
            if tab_data["context"] not in validation_schema.contexts:
                issues.append(
                    ValidationIssue(
                        level="error",
                        component="tab",
                        component_name=tab_name,
                        issue_type="missing_context",
                        message=f"Global context '{tab_data['context']}' not found in target project",
                        suggested_fix=f"Remove context or create context '{tab_data['context']}'",
                    ),
                )

        # Validate tiles
        for tile_template in tab_data.get("tiles", []):
            issues.extend(
                self.validate_tile_template(tile_template, validation_schema, path),
            )

        return issues

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
            issues.extend(
                self.validate_tab_template(
                    tab_template,
                    validation_schema,
                    interface_name,
                ),
            )

        # Check if template can be sanitized
        can_sanitize = all(issue.level != "error" for issue in issues)

        return ValidationResultSchema(
            is_valid=len([i for i in issues if i.level == "error"]) == 0,
            issues=issues,
            can_sanitize=can_sanitize,
        )

    def _validate_specialized_tile(
        self,
        specialized_data: dict,
        tile_type: str,
        validation_schema: ProjectValidationSchema,
        component_path: str,
    ) -> List[ValidationIssue]:
        """Validate specialized tile data."""
        issues = []

        if tile_type == "table_tile":
            # Validate column references in column_order, hidden_columns, etc.
            for field in [
                "column_order",
                "hidden_columns",
                "default_hidden_columns",
                "columns_pin_left",
                "columns_pin_right",
            ]:
                if specialized_data.get(field):
                    try:
                        columns = json.loads(specialized_data[field])
                        if isinstance(columns, list):
                            # Would validate against actual table columns here
                            pass
                    except json.JSONDecodeError:
                        issues.append(
                            ValidationIssue(
                                level="warning",
                                component="table_tile",
                                component_name=component_path,
                                issue_type="invalid_json",
                                message=f"Invalid JSON in {field}",
                                suggested_fix=f"Fix JSON format in {field}",
                            ),
                        )

        elif tile_type == "plot_tile":
            # Validate axis references
            for axis in ["x_axis", "y_axis"]:
                if specialized_data.get(axis):
                    # Would validate against actual column names here
                    pass

        return issues


class TemplateSanitizer:
    """Handles sanitization of templates for specific projects."""

    def __init__(self, validation_schema: ProjectValidationSchema):
        self.validation_schema = validation_schema

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

        # Remove invalid global context
        if sanitized.get("context"):
            if sanitized["context"] not in self.validation_schema.contexts:
                if remove_invalid:
                    sanitized["context"] = None
                elif not preserve_structure:
                    return None

        # Sanitize tiles
        sanitized_tiles = []
        for tile_template in sanitized.get("tiles", []):
            sanitized_tile = self.sanitize_tile_template(
                tile_template,
                remove_invalid,
                preserve_structure,
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
    ) -> Optional[dict]:
        """Sanitize a tile template for a project."""
        # Convert to dict if it's a schema object
        if isinstance(tile_template, TileTemplateSchema):
            sanitized = tile_template.model_dump()
        else:
            sanitized = tile_template.copy()

        # Remove invalid context
        if sanitized.get("context"):
            if sanitized["context"] not in self.validation_schema.contexts:
                if remove_invalid:
                    sanitized["context"] = None
                elif not preserve_structure:
                    return None

        # Remove invalid table references
        if sanitized.get("table"):
            if (
                self.validation_schema.tables
                and sanitized["table"] not in self.validation_schema.tables
            ):
                if remove_invalid:
                    sanitized["table"] = None
                elif not preserve_structure:
                    return None

        # Sanitize specialized tile data
        for tile_type in [
            "table_tile",
            "plot_tile",
            "view_tile",
            "editor_tile",
            "terminal_tile",
        ]:
            if sanitized.get(tile_type):
                sanitized_specialized = self._sanitize_specialized_tile(
                    sanitized[tile_type],
                    tile_type,
                    remove_invalid,
                    preserve_structure,
                )
                if sanitized_specialized:
                    sanitized[tile_type] = sanitized_specialized
                elif remove_invalid:
                    sanitized[tile_type] = None

        return sanitized

    def _sanitize_specialized_tile(
        self,
        specialized_data: dict,
        tile_type: str,
        remove_invalid: bool = True,
        preserve_structure: bool = True,
    ) -> Optional[dict]:
        """Sanitize specialized tile data."""
        sanitized = specialized_data.copy()

        if tile_type == "table_tile":
            # Clean up invalid JSON in column-related fields
            for field in [
                "column_order",
                "hidden_columns",
                "default_hidden_columns",
                "columns_pin_left",
                "columns_pin_right",
            ]:
                if sanitized.get(field):
                    try:
                        json.loads(sanitized[field])  # Validate JSON
                    except json.JSONDecodeError:
                        if remove_invalid:
                            sanitized[field] = None

        return sanitized


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
                "default_hidden_columns": tile.table_tile.default_hidden_columns,
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
            context=tab.context,
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

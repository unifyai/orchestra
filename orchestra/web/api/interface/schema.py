from typing import List, Optional, Union

from pydantic import BaseModel


class BaseSchema(BaseModel):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_checkpoint: bool = False


# Tile-related schemas
class TilePosition(BaseModel):
    x: float
    y: float
    width: float
    height: float


# Specialized tile schemas
class TableTileSchema(BaseModel):
    id: Optional[str] = None
    tile_id: Optional[str] = None
    table_type: Optional[str] = None
    page_number: Optional[str] = None
    column_order: Optional[str] = None
    hidden_columns: Optional[str] = None
    default_hidden_columns: Optional[bool] = None
    sorting: Optional[str] = None
    group_sorting: Optional[str] = None
    columns_pin_left: Optional[str] = None
    columns_pin_right: Optional[str] = None
    selected: Optional[str] = None


class PlotTileSchema(BaseModel):
    id: Optional[str] = None
    tile_id: Optional[str] = None
    plot_type: Optional[str] = None
    plot_scale_x: Optional[str] = None
    plot_scale_y: Optional[str] = None
    plot_aggregate: Optional[str] = None
    x_axis: Optional[str] = None
    y_axis: Optional[str] = None
    plot_group_by: Optional[str] = None
    plot_group_by_colors: Optional[str] = None
    bin_count: Optional[str] = None
    regression_line: Optional[str] = None


class ViewTileSchema(BaseModel):
    id: Optional[str] = None
    tile_id: Optional[str] = None
    base_index: Optional[str] = None


class EditorTileSchema(BaseModel):
    id: Optional[str] = None
    tile_id: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    content: Optional[str] = None


class TerminalTileSchema(BaseModel):
    id: Optional[str] = None
    tile_id: Optional[str] = None
    shell_type: Optional[str] = None


# Base schemas for common fields
class BaseTileTemplateSchema(BaseModel):
    """Base template schema for tiles with common fields"""

    name: str
    position: TilePosition
    type: Optional[str] = None
    minW: Optional[float] = None
    minH: Optional[float] = None
    visible: bool = True
    locked: bool = False
    moved: bool = False
    static: bool = False
    color: Optional[str] = None
    context: Optional[str] = None
    table: Optional[str] = None
    auto_update: Optional[str] = None
    freeze: Optional[str] = None
    filters: Optional[str] = None
    common_filter: Optional[str] = None
    metric: Optional[str] = None
    column_context: Optional[str] = None
    grouping: Optional[str] = None
    # Type-specific template data
    table_tile: Optional[TableTileSchema] = None
    plot_tile: Optional[PlotTileSchema] = None
    view_tile: Optional[ViewTileSchema] = None
    editor_tile: Optional[EditorTileSchema] = None
    terminal_tile: Optional[TerminalTileSchema] = None


class TileTemplateSchema(BaseTileTemplateSchema):
    """Template schema for a detached tile - inherits all fields from base"""

    # Template-specific metadata
    template_version: str = "1.0"
    description: Optional[str] = None
    created_by: Optional[str] = None
    tags: List[str] = []


class BaseTileSchema(BaseTileTemplateSchema):
    """Base tile schema with common fields - extends template base with IDs and timestamps"""

    id: Optional[str] = None
    tab_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_checkpoint: bool = False


class TileSchema(BaseTileSchema):
    """Complete Tile schema with type-specific properties - now inherits from base"""


class BaseTabTemplateSchema(BaseModel):
    """Base template schema for tabs with common fields"""

    name: str
    visible: bool = True
    active: bool = False
    order: Optional[int] = None
    context: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None


class TabTemplateSchema(BaseTabTemplateSchema):
    """Template schema for a detached tab"""

    tiles: List[TileTemplateSchema] = []
    # Template-specific metadata
    template_version: str = "1.0"
    description: Optional[str] = None
    created_by: Optional[str] = None
    tags: List[str] = []


class BaseTabSchema(BaseTabTemplateSchema):
    """Base tab schema with common fields - extends template base with IDs and timestamps"""

    id: str
    interface_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_checkpoint: bool = False
    icon: str


class TabSchema(BaseTabSchema):
    """Complete Tab schema - now inherits from base"""

    tiles: List[TileSchema] = []


class BaseInterfaceTemplateSchema(BaseModel):
    """Base template schema for interfaces with common fields"""

    name: str
    icon: Optional[str] = None
    color: Optional[str] = None
    order: Optional[int] = None


class InterfaceTemplateSchema(BaseInterfaceTemplateSchema):
    """Template schema for a detached interface"""

    tabs: List[TabTemplateSchema] = []
    active_tab_name: Optional[str] = None  # Use name instead of ID for templates
    # Template-specific metadata
    template_version: str = "1.0"
    description: Optional[str] = None
    created_by: Optional[str] = None
    tags: List[str] = []


class ProjectTemplateSchema(BaseModel):
    """Template schema for multiple interfaces from a project"""

    interfaces: List[InterfaceTemplateSchema] = []
    # Template-specific metadata
    template_version: str = "1.0"
    description: Optional[str] = None
    created_by: Optional[str] = None
    tags: List[str] = []


class BaseInterfaceSchema(BaseInterfaceTemplateSchema):
    """Base interface schema with common fields - extends template base with IDs and timestamps"""

    id: str
    project_id: int
    context: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_checkpoint: bool = False
    icon: str


class InterfaceSchema(BaseInterfaceSchema):
    """Complete Interface schema - now inherits from base"""

    tabs: List[TabSchema] = []
    active_tab_id: Optional[str] = None


# Request/response schemas
class CreateTileRequest(BaseTileTemplateSchema):
    """Request to create a tile - inherits common fields from base template schema"""

    tile_id: Optional[str] = None
    tab_id: str

    class Config:
        extra = "forbid"  # Reject unknown fields


class UpdateTileRequest(BaseModel):
    """Request to update a tile - only includes fields that can be updated"""

    name: Optional[str] = None
    position: Optional[TilePosition] = None
    type: Optional[str] = None
    minW: Optional[float] = None
    minH: Optional[float] = None
    visible: Optional[bool] = None
    locked: Optional[bool] = None
    moved: Optional[bool] = None
    static: Optional[bool] = None
    color: Optional[str] = None
    context: Optional[str] = None
    table: Optional[str] = None
    auto_update: Optional[str] = None
    freeze: Optional[str] = None
    filters: Optional[str] = None
    common_filter: Optional[str] = None
    metric: Optional[str] = None
    column_context: Optional[str] = None
    grouping: Optional[str] = None
    # Type-specific fields
    table_tile: Optional[TableTileSchema] = None
    plot_tile: Optional[PlotTileSchema] = None
    view_tile: Optional[ViewTileSchema] = None
    editor_tile: Optional[EditorTileSchema] = None
    terminal_tile: Optional[TerminalTileSchema] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


class CreateTabRequest(BaseTabTemplateSchema):
    """Request to create a tab - inherits common fields from base template schema"""

    tab_id: Optional[str] = None
    interface_id: str

    class Config:
        extra = "forbid"  # Reject unknown fields


class UpdateTabRequest(BaseModel):
    """Request to update a tab - only includes fields that can be updated"""

    name: Optional[str] = None
    visible: Optional[bool] = None
    active: Optional[bool] = None
    order: Optional[int] = None
    context: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


class CreateInterfaceRequest(BaseInterfaceTemplateSchema):
    """Request to create an interface - inherits common fields from base template schema"""

    interface_id: Optional[str] = None
    project_name: str
    context: Optional[str] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


class UpdateInterfaceRequest(BaseModel):
    """Request to update an interface - only includes fields that can be updated"""

    name: Optional[str] = None
    active_tab_id: Optional[str] = None
    order: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    context: Optional[str] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


# For legacy support (until frontend migration is complete)
class Item(BaseModel):
    """Legacy Item schema for backward compatibility"""

    i: str
    x: float
    y: float
    w: float
    h: float
    minW: Optional[float] = None
    minH: Optional[float] = None
    moved: bool = False
    static: bool = False
    visible: bool = True
    color: Optional[str] = None
    tab: Optional[str] = None
    table: Optional[str] = None
    table_type: Optional[str] = None
    auto_update: Optional[str] = None
    freeze: Optional[str] = None
    context: Optional[str] = None
    column_context: Optional[str] = None
    prev_context: Optional[str] = None
    filters: Optional[str] = None
    common_filter: Optional[str] = None
    page_number: Optional[str] = None
    metric: Optional[str] = None
    column_order: Optional[str] = None
    hidden_columns: Optional[str] = None
    default_hidden_columns: Optional[bool] = None
    sorting: Optional[str] = None
    grouping: Optional[str] = None
    group_sorting: Optional[str] = None
    columns_pin_left: Optional[str] = None
    columns_pin_right: Optional[str] = None
    selected: Optional[str] = None
    base_index: Optional[str] = None
    plot_type: Optional[str] = None
    plot_scale_x: Optional[str] = None
    plot_scale_y: Optional[str] = None
    plot_aggregate: Optional[str] = None
    x_axis: Optional[str] = None
    y_axis: Optional[str] = None
    plot_group_by: Optional[str] = None
    plot_group_by_colors: Optional[str] = None
    bin_count: Optional[str] = None
    regression_line: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    content: Optional[str] = None


# Reference schemas for API responses
class TileReference(BaseModel):
    """Simple reference to a tile in a tab"""

    id: str
    name: str
    type: str


class TabReference(BaseModel):
    """Simple reference to a tab in an interface"""

    id: str
    name: str
    active: bool


# Validation Schema Objects
class ProjectValidationSchema(BaseModel):
    """Schema describing what exists in a project for template validation"""

    contexts: List[str] = []
    tables: List[str] = []
    columns: dict = {}  # table_name -> [column_names]
    field_types: dict = {}  # context_name -> {field_name: field_type}


class ValidationIssue(BaseModel):
    """Individual validation issue"""

    level: str  # "error", "warning", "info"
    component: str  # "interface", "tab", "tile", "table_tile", etc.
    component_name: str
    issue_type: str  # "missing_context", "missing_table", "missing_column", etc.
    message: str
    suggested_fix: Optional[str] = None


class ValidationResultSchema(BaseModel):
    """Result of validating a template against a project"""

    is_valid: bool
    issues: List[ValidationIssue] = []
    can_sanitize: bool  # Whether issues can be automatically fixed
    sanitized_template: Optional[
        Union[
            ProjectTemplateSchema,
            InterfaceTemplateSchema,
            TabTemplateSchema,
            TileTemplateSchema,
        ]
    ] = None  # Properly typed sanitized version


# Request/Response Schemas for Template Operations
class ExportTemplateRequest(BaseModel):
    """Request to export a template"""

    # Common fields
    include_metadata: bool = True
    description: Optional[str] = None
    tags: List[str] = []
    template_name: Optional[str] = None


class ExportInterfaceTemplateRequest(ExportTemplateRequest):
    """Request to export an interface template"""

    interface_id: Optional[str] = None
    project_name: Optional[str] = None
    interface_name: Optional[str] = None
    checkpoint: bool = False


class ExportTabTemplateRequest(ExportTemplateRequest):
    """Request to export a tab template"""

    tab_id: Optional[str] = None
    interface_id: Optional[str] = None
    tab_name: Optional[str] = None
    checkpoint: bool = False


class ExportTileTemplateRequest(ExportTemplateRequest):
    """Request to export a tile template"""

    tile_id: Optional[str] = None
    tab_id: Optional[str] = None
    tile_name: Optional[str] = None
    checkpoint: bool = False


class ExportProjectTemplateRequest(ExportTemplateRequest):
    """Request to export a project template"""

    project_name: str
    interface_names: Optional[List[str]] = None  # If None, export all interfaces
    checkpoint: bool = False


class ValidateTemplateRequest(BaseModel):
    """Request to validate a template against a project"""

    project_name: str
    template: Union[
        ProjectTemplateSchema,
        InterfaceTemplateSchema,
        TabTemplateSchema,
        TileTemplateSchema,
    ]  # Properly typed template
    strict_validation: bool = False  # Whether to be strict about warnings


class SanitizeTemplateRequest(BaseModel):
    """Request to sanitize a template for a project"""

    project_name: str
    template: Union[
        ProjectTemplateSchema,
        InterfaceTemplateSchema,
        TabTemplateSchema,
        TileTemplateSchema,
    ]  # Properly typed template
    remove_invalid_references: bool = True
    preserve_structure: bool = True  # Try to keep original structure where possible


class ImportTemplateRequest(BaseModel):
    """Base request to import a template"""

    project_name: str
    validate_first: bool = True
    auto_sanitize: bool = True
    overwrite_existing: bool = False  # Whether to overwrite if name conflicts


class ImportInterfaceTemplateRequest(ImportTemplateRequest):
    """Request to import an interface template"""

    template: InterfaceTemplateSchema  # Properly typed template
    new_interface_name: Optional[str] = None  # Override template name if provided


class ImportTabTemplateRequest(ImportTemplateRequest):
    """Request to import a tab template"""

    template: TabTemplateSchema  # Properly typed template
    interface_id: Optional[str] = None
    interface_name: Optional[str] = None
    new_tab_name: Optional[str] = None  # Override template name if provided


class ImportTileTemplateRequest(ImportTemplateRequest):
    """Request to import a tile template"""

    template: TileTemplateSchema  # Properly typed template
    tab_id: Optional[str] = None
    interface_id: Optional[str] = None
    tab_name: Optional[str] = None
    new_tile_name: Optional[str] = None  # Override template name if provided


class ImportProjectTemplateRequest(ImportTemplateRequest):
    """Request to import a project template"""

    template: ProjectTemplateSchema  # Properly typed template
    interface_name_prefix: Optional[str] = None  # Prefix for imported interface names


# Response Schemas
class TemplateExportResponse(BaseModel):
    """Response from exporting a template"""

    template: Union[
        ProjectTemplateSchema,
        InterfaceTemplateSchema,
        TabTemplateSchema,
        TileTemplateSchema,
    ]  # Properly typed template
    metadata: dict
    export_stats: dict  # counts of interfaces, tabs, tiles exported


class TemplateImportResponse(BaseModel):
    """Response from importing a template"""

    success: bool
    validation_result: Optional[ValidationResultSchema] = None
    import_stats: dict  # counts of interfaces, tabs, tiles imported
    created_ids: dict  # mapping of component types to new IDs created
    warnings: List[str] = []

from typing import List, Optional

from pydantic import BaseModel, Field


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


class BaseTileSchema(BaseSchema):
    id: Optional[str] = None
    name: str
    position: TilePosition
    type: Optional[str] = None
    minW: Optional[float] = Field(None)
    minH: Optional[float] = Field(None)
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


class TableTileSchema(BaseModel):
    id: Optional[str] = None
    tile_id: Optional[str] = None
    table_type: Optional[str] = None
    page_number: Optional[str] = None
    column_order: Optional[str] = None
    hidden_columns: Optional[str] = None
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
    file_path: Optional[str] = None
    file_type: Optional[str] = None
    content: Optional[str] = None


class TileSchema(BaseTileSchema):
    """Complete Tile schema with type-specific properties"""

    tab_id: str
    table_tile: Optional[TableTileSchema] = None
    plot_tile: Optional[PlotTileSchema] = None
    view_tile: Optional[ViewTileSchema] = None
    editor_tile: Optional[EditorTileSchema] = None


# Tab-related schemas
class TabSchema(BaseSchema):
    interface_id: str
    name: str
    visible: bool = True
    active: bool = False
    order: int = 0
    global_context: Optional[str] = None
    color: Optional[str] = None
    tiles: List[TileSchema] = []


# Interface-related schemas
class InterfaceSchema(BaseSchema):
    name: str
    project_id: int
    tabs: List[TabSchema] = []
    active_tab_id: Optional[str] = None
    color: Optional[str] = None


# Request/response schemas
class CreateTileRequest(BaseModel):
    tile_id: Optional[str] = None
    tab_id: str
    name: str
    position: TilePosition
    type: Optional[str] = None
    minW: Optional[float] = None
    minH: Optional[float] = None
    visible: Optional[bool] = True
    locked: Optional[bool] = False
    moved: Optional[bool] = False
    static: Optional[bool] = False
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
    # Type-specific fields will be included based on the tile type
    table_tile: Optional[TableTileSchema] = None
    plot_tile: Optional[PlotTileSchema] = None
    view_tile: Optional[ViewTileSchema] = None
    editor_tile: Optional[EditorTileSchema] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


class UpdateTileRequest(BaseModel):
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

    class Config:
        extra = "forbid"  # Reject unknown fields


class CreateTabRequest(BaseModel):
    tab_id: Optional[str] = None
    interface_id: str
    name: str
    visible: bool = True
    active: bool = False
    order: int = 0
    global_context: Optional[str] = None
    color: Optional[str] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


class UpdateTabRequest(BaseModel):
    name: Optional[str] = None
    visible: Optional[bool] = None
    active: Optional[bool] = None
    order: Optional[int] = None
    global_context: Optional[str] = None
    color: Optional[str] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


class CreateInterfaceRequest(BaseModel):
    interface_id: Optional[str] = None
    project: str
    name: str
    color: Optional[str] = None

    class Config:
        extra = "forbid"  # Reject unknown fields


class UpdateInterfaceRequest(BaseModel):
    name: Optional[str] = None
    active_tab_id: Optional[str] = None
    color: Optional[str] = None

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
    file_path: Optional[str] = None
    file_type: Optional[str] = None
    content: Optional[str] = None


class LegacyInterfaceConfig(BaseModel):
    """Legacy Interface configuration schema for backward compatibility"""

    name: str
    project: str
    items: List[Item]
    new_counter: int
    temporary: bool = False
    new_name: Optional[str] = None
    context: Optional[str] = None
    color: Optional[str] = None


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

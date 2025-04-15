from pydantic import BaseModel
from typing import List, Optional

# Legacy support (until frontend migration is complete)
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
    checkpoint: Optional[bool] = None


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

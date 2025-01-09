from pydantic import BaseModel


class Item(BaseModel):
    i: str
    x: float
    y: float
    w: float
    h: float
    moved: bool
    static: bool
    tab: str | None = None
    table: str | None = None
    filters: str | None = None
    common_filter: str | None = None
    page_number: str | None = None
    metric: str | None = None
    column_order: str | None = None
    hidden_columns: str | None = None
    sorting: str | None = None
    grouping: str | None = None
    columns_pin_left: str | None = None
    columns_pin_right: str | None = None
    selected: str | None = None
    base_index: str | None = None
    plot_type: str | None = None
    plot_scale: str | None = None
    is_aggregated: str | None = None
    x_axis: str | None = None
    y_axis: str | None = None
    plot_group_by: str | None = None


class InterfaceConfig(BaseModel):
    items: list[Item]
    new_counter: int
    project: str | None = None
    temporary: bool = False

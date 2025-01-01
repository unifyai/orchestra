from pydantic import BaseModel


class Item(BaseModel):
    i: str
    x: int
    y: int
    w: int
    h: int
    tab: str | None
    moved: bool
    static: bool


class InterfaceConfig(BaseModel):
    items: list[Item]
    new_counter: int

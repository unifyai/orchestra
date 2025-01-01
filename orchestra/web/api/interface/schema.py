from pydantic import BaseModel


class Item(BaseModel):
    i: str
    x: int
    y: int
    w: int
    h: int
    moved: bool
    static: bool
    tab: str | None = None


class InterfaceConfig(BaseModel):
    items: list[Item]
    new_counter: int

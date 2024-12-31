from pydantic import BaseModel


class Item(BaseModel):
    i: str
    x: int
    y: int
    w: int
    h: int
    tab: str | None


class InterfaceConfig(BaseModel):
    items: list[Item]
    new_counter: int

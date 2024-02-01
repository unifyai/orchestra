from dataclasses import asdict, dataclass, fields
from typing import Dict, List, Optional, Union

from providers.pricing.tools.utils import empty_as_none


def bool_loader(x: Union[bool, str]) -> bool:
    if isinstance(x, bool):
        return x
    return x.lower() == "true"


@dataclass
class RawCatalogItem:
    model_name: Optional[str]
    in_price: Optional[float]
    out_price: Optional[float]

    @staticmethod
    def from_dict(v: dict) -> "RawCatalogItem":
        return RawCatalogItem(
            model_name=empty_as_none(v.get("model_name")),
            in_price=empty_as_none(v.get("in_price"), loader=float),
            out_price=empty_as_none(v.get("out_price"), loader=float),
        )

    def dict(self) -> Dict[str, Union[str, int, float, bool, None]]:
        return asdict(self)


@dataclass
class CatalogItem(RawCatalogItem):
    """
    Attributes:
        model_name: name of the model
        price: $ per 1M of tokens
        provider: name of the provider
    """

    model_name: str
    in_price: float
    out_price: float
    provider: str

    @staticmethod
    def from_dict(v: dict, *, provider: Optional[str] = None) -> "CatalogItem":
        return CatalogItem(provider=provider, **asdict(RawCatalogItem.from_dict(v)))


@dataclass
class QueryFilter:
    """
    Attributes:
        provider: name of the provider to filter by. If not specified, all providers will be used
        min_price_inp: minimum input price in USD
        max_price_inp: maximum input price in USD
        min_price_out: minimum output price in USD
        max_price_out: maximum output price in USD
    """

    provider: Optional[List[str]] = None
    min_price_inp: Optional[float] = None
    max_price_inp: Optional[float] = None
    min_price_out: Optional[float] = None
    max_price_out: Optional[float] = None

    def __post_init__(self):
        if self.provider is not None:
            self.provider = [i.lower() for i in self.provider]

    def __repr__(self) -> str:
        """
        >>> QueryFilter()
        QueryFilter()
        >>> QueryFilter(max_price=1.2)
        QueryFilter(max_price=1.2)
        """
        kv = ", ".join(
            f"{f.name}={value}"
            for f in fields(self)
            if (value := getattr(self, f.name)) is not None
        )
        return f"QueryFilter({kv})"


@dataclass
class ModelInfo:
    name: str
    description: str

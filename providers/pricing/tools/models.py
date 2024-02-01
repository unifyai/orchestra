from dataclasses import asdict, dataclass, fields
from typing import Dict, List, Optional, Tuple, Union

from providers.pricing.tools.utils import empty_as_none

def bool_loader(x: Union[bool, str]) -> bool:
    if isinstance(x, bool):
        return x
    return x.lower() == "true"


@dataclass
class RawCatalogItem:
    model_name: Optional[str]
    price: Optional[float]

    @staticmethod
    def from_dict(v: dict) -> "RawCatalogItem":
        return RawCatalogItem(
            model_name=empty_as_none(v.get("model_name")),
            price=empty_as_none(v.get("price"), loader=float),
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
    price: float
    provider: str

    @staticmethod
    def from_dict(v: dict, *, provider: Optional[str] = None) -> "CatalogItem":
        return CatalogItem(provider=provider, **asdict(RawCatalogItem.from_dict(v)))


@dataclass
class QueryFilter:
    """
    Attributes:
        provider: name of the provider to filter by. If not specified, all providers will be used
        min_price: minimum price per hour in USD
        max_price: maximum price per hour in USD
    """

    provider: Optional[List[str]] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None

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

from abc import ABC, abstractmethod
from typing import List, Optional

from providers.pricing.tools.models import QueryFilter, RawCatalogItem


class AbstractProvider(ABC):
    NAME: str = "abstract"

    @abstractmethod
    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        pass

    @classmethod
    def filter(cls, offers: List[RawCatalogItem]) -> List[RawCatalogItem]:
        return offers

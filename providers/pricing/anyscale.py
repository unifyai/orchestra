import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

logger = logging.getLogger(__name__)


class AnyscaleProvider(AbstractProvider):
    NAME = "anyscale"

    def __init__(self):
        req = Request(
            "https://docs.endpoints.anyscale.com/pricing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        for row in self.pricing_tables[0].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_name = cols[0].text.strip()
            price = float(cols[1].text.strip())
            offer = RawCatalogItem(
                model_name=model_name,
                in_price=price,
                out_price=price,
                request_price=None,
            )
            offers.append(offer)
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = AnyscaleProvider()
    print(provider.get())

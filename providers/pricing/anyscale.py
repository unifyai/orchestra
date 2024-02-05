import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

from providers.completion.anyscale import Anyscale
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
        self.supported_models = set([x["endpoint"].split("/")[-1].lower() for x in Anyscale().supported_models.values()])

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        found_models = []
        for row in self.pricing_tables[0].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_name = cols[0].text.strip().lower()
            price = float(cols[1].text.strip())
            offer = RawCatalogItem(
                model_name=model_name,
                in_price=price,
                out_price=price,
                request_price=None,
            )
            offers.append(offer)
            found_models.append(model_name)
        # checking if any model left
        models_missing_in_unify = []
        for model_name in found_models:
            try:
                self.supported_models.remove(model_name)
            except KeyError:
                models_missing_in_unify.append(model_name)
        if models_missing_in_unify:
            print(f"Found in pricing page but not in our list ({self.NAME}): {models_missing_in_unify}")
        if self.supported_models != set():
            print(f"Models not in pricing table ({self.NAME}): {list(self.supported_models)}")
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = AnyscaleProvider()
    print(provider.get())

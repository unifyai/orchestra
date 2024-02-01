import logging
import re
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

logger = logging.getLogger(__name__)


class TogetherAIProvider(AbstractProvider):
    NAME = "together-ai"

    def __init__(self):
        req = Request(
            "https://www.together.ai/pricing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all('ul', class_='pricing-list w-list-unstyled')

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        for table in self.pricing_tables:
            if table.h3.text == 'CHat, language, and\xa0code models':
                for div in table.find_all('div', class_='pricing_content-cell'):
                    # TODO: filter out model sizes and prices
                    for entries in div.find_all("p"):
                        print(entries.text)
                    model_size = div.find('h4').text
                    input_pr = float(input_pr)
                    output_pr = float(output_pr)

                    offer = RawCatalogItem(
                        model_name=model_name,
                        in_price=input_pr,
                        out_price=output_pr,
                        request_price=None,
                    )
                    offers.append(offer)
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = TogetherAIProvider()
    print(provider.get())
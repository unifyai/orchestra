import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

logger = logging.getLogger(__name__)


class OpenAIProvider(AbstractProvider):
    NAME = "openai"

    def __init__(self):
        req = Request(
            "https://openai.com/pricing",
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
        for table in self.pricing_tables:
            table_headers = table.find_all("span", class_="f-heading-5")
            if table_headers[0].text != "Model" or table_headers[1].text != "Input":
                continue
            for row in table.find_all("tr")[1:]:
                cols = row.find_all("span")
                if len(cols) == 5:
                    model_name = cols[0].text
                    input_pr = cols[1].text[1:]
                    output_pr = cols[3].text[1:]
                elif len(cols) == 3:
                    model_name = cols[0].text
                    input_pr = cols[1].text[1:]
                    output_pr = cols[1].text[1:]

                # standardize pricing to per million
                input_pr = float(input_pr) * 1000
                output_pr = float(output_pr) * 1000

                offer = RawCatalogItem(
                    model_name=model_name,
                    in_price=input_pr,
                    out_price=output_pr,
                    request_price=None,
                )
                offers.append(offer)
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = OpenAIProvider()
    print(provider.get())

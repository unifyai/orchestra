import logging
import re
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

logger = logging.getLogger(__name__)


class MistralProvider(AbstractProvider):
    NAME = "mistral-ai"

    def __init__(self):
        req = Request(
            "https://docs.mistral.ai/platform/pricing/",
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
            rows = table.find_all("tr")
            for row in rows[1:]:
                cols = row.find_all("td")
                model_name = cols[0].find("code").text
                input_pr = cols[1].text.strip()
                inp_conv_pr = find_and_convert(input_pr)
                if not model_name == "mistral-embed":
                    output_pr = cols[2].text.strip()
                    out_conv_pr = find_and_convert(output_pr)
                    offer = RawCatalogItem(
                        model_name=model_name,
                        in_price=inp_conv_pr,
                        out_price=out_conv_pr,
                        request_price=None,
                    )
                else:
                    offer = RawCatalogItem(
                        model_name=model_name,
                        in_price=inp_conv_pr,
                        out_price=0.0,
                        request_price=None,
                    )
                offers.append(offer)
        return sorted(offers, key=lambda i: i.in_price)


def find_and_convert(search_str):
    pattern = r"(\d+\.\d+)"
    match = re.search(pattern, search_str)
    eur_pr = float(match.group(1))
    # need to use API to fetch currency exchange rate
    usd_pr = eur_pr * 1.08
    return usd_pr


if __name__ == "__main__":
    provider = MistralProvider()
    print(provider.get())

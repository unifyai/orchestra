import logging
import re
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.completion.mistral import Mistral
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

from forex_python.converter import CurrencyRates

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


class MistralProvider(AbstractProvider):
    NAME = "mistral-ai"

    def __init__(self):
        self.currency_rates = CurrencyRates()
        req = Request(
            "https://docs.mistral.ai/platform/pricing/",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")
        self.supported_models = set(
            [
                x["endpoint"].split("/")[-1].lower()
                for x in Mistral().supported_models.values()
            ],
        )


    def find_and_convert(self, search_str):
        match = re.findall(r"(\d+\.\d+)€", search_str)
        eur_pr = float(match[0])
        usd_pr = self.currency_rates.convert("EUR", "USD", eur_pr)
        return usd_pr
    

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        found_models = []
        for table in self.pricing_tables:
            rows = table.find_all("tr")
            for row in rows[1:]:
                cols = row.find_all("td")
                model_name = cols[0].text.strip().lower()
                input_pr = cols[1].text.strip()
                inp_conv_pr = self.find_and_convert(input_pr)
                if not model_name == "mistral-embed":
                    output_pr = cols[2].text.strip()
                    out_conv_pr = self.find_and_convert(output_pr)
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
                found_models.append(model_name)
        # checking if any model left
        models_missing_in_unify = []
        for model_name in found_models:
            try:
                self.supported_models.remove(model_name)
            except KeyError:
                models_missing_in_unify.append(model_name)
        if models_missing_in_unify:
            logger.info(
                f"Found in pricing page but not in our list ({self.NAME}): {models_missing_in_unify}",
            )
        if self.supported_models != set():
            logger.info(
                f"Models not in pricing page ({self.NAME}): {list(self.supported_models)}",
            )
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = MistralProvider()
    print(provider.get())

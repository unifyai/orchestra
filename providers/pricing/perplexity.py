import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.completion.perplexity import Perplexity
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

logger = logging.getLogger(__name__)


class PerplexityProvider(AbstractProvider):
    NAME = "perplexity-ai"

    def __init__(self):
        req = Request(
            "https://docs.perplexity.ai/docs/pricing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")
        # perplexity only lists pricing according to model size
        # so pulling all supported models
        self.supported_models = set(
            [
                x["endpoint"].split("/")[-1].lower()
                for x in Perplexity().supported_models.values()
            ],
        )

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        online_models_pr = {}
        for row in self.pricing_tables[1].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_size = cols[0].text.strip()
            requests_pr = float(cols[1].text[1:].strip())
            output_pr = float(cols[2].text[1:].strip())
            online_models_pr[model_size] = (requests_pr, output_pr)
        for row in self.pricing_tables[0].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_size = cols[0].text.strip()
            input_pr = float(cols[1].text[1:].strip())
            output_pr = float(cols[2].text[1:].strip())

            relevant_models = []
            for model_name in self.supported_models:
                if model_size.lower() in model_name:
                    relevant_models.append(model_name)

            for model_name in relevant_models:
                self.supported_models.remove(model_name)
                if "online" in model_name:
                    input_pr = 0
                    offer = RawCatalogItem(
                        model_name=model_name,
                        in_price=input_pr,
                        out_price=online_models_pr[model_size][1],
                        request_price=online_models_pr[model_size][0],
                    )
                else:
                    offer = RawCatalogItem(
                        model_name=model_name,
                        in_price=input_pr,
                        out_price=output_pr,
                        request_price=None,
                    )
                offers.append(offer)
        # checking if any model left
        if self.supported_models != set():
            print(f"Models not in pricing table ({self.NAME}): {self.supported_models}")
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = PerplexityProvider()
    print(provider.get())

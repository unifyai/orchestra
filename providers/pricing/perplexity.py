import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem
from providers.completion.perplexity import Perplexity

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
        self.perplexity_models = set(Perplexity().supported_models)

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
            for model_name in self.perplexity_models:
                if model_size.lower() in model_name:
                    relevant_models.append(model_name)

            for model_name in relevant_models:
                self.perplexity_models.remove(model_name)
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
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = PerplexityProvider()
    print(provider.get())
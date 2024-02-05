import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

from providers.completion.octoai_provider import OctoAI

logger = logging.getLogger(__name__)


class OctoAIProvider(AbstractProvider):
    NAME = "octoai"

    def __init__(self):
        req = Request(
            "https://octo.ai/docs/getting-started/pricing-and-billing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")
        self.supported_models = set([x["endpoint"].split("/")[-1].lower() for x in OctoAI().supported_models.values()])

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        found_models = []
        for row in self.pricing_tables[-2].find_all("tr")[1:]:
            cols = row.find_all("td")
            parameter_precision = "-" + cols[1].text.strip().lower()
            model_name = cols[0].text.strip().lower().replace(" ", "-") + parameter_precision
            input_pr = cols[2].text[1:].split("/")[0].strip()
            output_pr = cols[3].text[1:].split("/")[0].strip()

            if "llama2" in model_name:
                model_name = model_name.replace("llama2", "llama-2")

            # standardize pricing to per million
            output_pr = float(output_pr) * 1000
            input_pr = float(input_pr) * 1000 if input_pr else output_pr
            offer = RawCatalogItem(
                model_name=model_name,
                in_price=input_pr,
                out_price=output_pr,
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
    provider = OctoAIProvider()
    print(provider.get())

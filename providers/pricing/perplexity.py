import logging
import re
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.completion.perplexity import Perplexity
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


class PerplexityProvider(AbstractProvider):
    NAME = "perplexity-ai"

    def __init__(self):
        # model size pricing
        req = Request(
            "https://docs.perplexity.ai/docs/pricing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")
        self.supported_models = dict()
        for k, v in Perplexity().supported_models.items():
            self.supported_models[v['endpoint'].split("/")[-1].lower()] = {'mdl_code': k, "cost": v['cost']}

    def get_models_missing_in_unify(self, notification_msgs):
        """Checks against all models in Perplexity website"""
        req = Request(
            "https://docs.perplexity.ai/docs/model-cards",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        pricing_tables = soup.find_all("table")
        # checking if any model left
        models_missing_in_unify = []
        for row in pricing_tables[0].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_name = cols[0].text.strip().lower().split(" ")[0]
            context_len = int(cols[1].text.strip().split(" ")[0])
            model_type = cols[2].text.strip().lower()
            if model_type == "chat completion":
                if model_name not in self.supported_models:
                    models_missing_in_unify.append(model_name)
        if models_missing_in_unify:
            notification_msgs.append(
                f"Found in pricing page but not in our list ({self.NAME}): {models_missing_in_unify}",
            )

    def get(
        self,
        mdl_codes: Optional[List[str]] = None,
    )  -> tuple[List[RawCatalogItem], List[str]]:
        '''
        Runs with or without mdl_codes
        If mdl_codes is None, returns all pricing of all models found
        '''
        offers = []
        online_models_pr = {}
        notification_msgs = []
        # get pricing details of online models, this is the second table
        # in the pricing page
        for row in self.pricing_tables[1].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_size = cols[0].text.strip()
            requests_pr = float(cols[1].text[1:].strip())
            output_pr = float(cols[2].text[1:].strip())
            online_models_pr[model_size] = {"requests_pr": requests_pr, "output_pr": output_pr}
        # going through vanilla model pricing in 1st table
        for row in self.pricing_tables[0].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_size = cols[0].text.strip()
            input_pr = float(cols[1].text[1:].strip())
            output_pr = float(cols[2].text[1:].strip())
            request_price = None

            relevant_model_endpoints = []
            for model_endpoint_name in self.supported_models:
                supported_model_size = re.findall(r"((?:\d+x)?\d+b)", model_endpoint_name)[
                    0
                ].lower()
                # https://docs.perplexity.ai/changelog/new-model-mixtral-8x7b-instruct
                if supported_model_size == "8x7b":
                    supported_model_size = "13b"
                if model_size.lower() == supported_model_size:
                    relevant_model_endpoints.append(model_endpoint_name)

            for model_endpoint_name in relevant_model_endpoints:
                model_metadata = self.supported_models.pop(model_endpoint_name)
                mdl_code = model_metadata['mdl_code']
                cost_info = model_metadata['cost']
                if "online" in mdl_code:
                    input_pr = 0
                    output_pr = online_models_pr[model_size]["output_pr"]
                    request_price=online_models_pr[model_size]["requests_pr"],
                
                if input_pr != cost_info['prompt'] or output_pr != cost_info['completion']:
                    notification_msgs.append(
                        f"Model {model_endpoint_name} has different prompt and completion costs than in supported_models dict",
                    )
                if mdl_codes and mdl_code not in mdl_codes:
                    continue
                offer = RawCatalogItem(
                    model_name=mdl_code,
                    in_price=input_pr,
                    out_price=output_pr,
                    request_price=request_price,
                )
                offers.append(offer)
        self.get_models_missing_in_unify(notification_msgs)
        if len(self.supported_models):
            notification_msgs.append(
                f"Models not in pricing page ({self.NAME}): {self.supported_models.keys()}",
            )
        return sorted(offers, key=lambda i: i.in_price), notification_msgs


if __name__ == "__main__":
    provider = PerplexityProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)

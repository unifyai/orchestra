import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.completion.octoai_provider import OctoAI
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem


class OctoAIProvider(AbstractProvider):
    NAME = "octoai"

    def __init__(self):
        req = Request(
            "https://octo.ai/docs/getting-started/pricing-and-billing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read().decode('utf-8', 'replace')
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")
        self.supported_models = dict()
        for k, v in OctoAI().supported_models.items():
            self.supported_models[v['endpoint'].split("/")[-1].lower()] = {'mdl_code': k, "cost": v['cost']}


    def get(
        self,
        mdl_codes: Optional[List[str]] = None,
    )  -> tuple[List[RawCatalogItem], List[str]]:
        '''
        Runs with or without mdl_codes
        If mdl_codes is None, returns all pricing of all models found
        '''
        offers = []
        models_missing_in_unify = []
        notification_msgs = []
        for row in self.pricing_tables[-2].find_all("tr")[1:]:
            cols = row.find_all("td")
            parameter_precision = "-" + cols[1].text.strip().lower()
            model_endpoint_name = (
                cols[0].text.strip().lower().replace(" ", "-") + parameter_precision
            )
            input_pr = cols[2].text[1:].split("/")[0].strip()
            output_pr = cols[3].text[1:].split("/")[0].strip()

            if "llama2" in model_endpoint_name:
                model_endpoint_name = model_endpoint_name.replace("llama2", "llama-2")

            # standardize pricing to per million
            output_pr = float(output_pr) * 1000
            input_pr = float(input_pr) * 1000 if input_pr else output_pr
            if model_endpoint_name in self.supported_models:
                model_metadata = self.supported_models.pop(model_endpoint_name)
                mdl_code = model_metadata['mdl_code']
                cost_info = model_metadata['cost']
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
                    request_price=None,
                )
                offers.append(offer)
            else:
                models_missing_in_unify.append(model_endpoint_name)
        if models_missing_in_unify:
            notification_msgs.append(
                f"Found in pricing page but not in our list ({self.NAME}): {models_missing_in_unify}",
            )
        if len(self.supported_models):
            notification_msgs.append(
                f"Models not in pricing page ({self.NAME}): {self.supported_models.keys()}",
            )
        return sorted(offers, key=lambda i: i.in_price), notification_msgs


if __name__ == "__main__":
    provider = OctoAIProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)

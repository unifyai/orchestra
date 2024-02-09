import logging
import re
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.completion.togetherai import TogetherAI
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem


class TogetherAIProvider(AbstractProvider):
    NAME = "together-ai"

    def __init__(self):
        # model size pricing
        req = Request(
            "https://www.together.ai/pricing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("ul", class_="pricing-list w-list-unstyled")
        self.supported_models = dict()
        for k, v in TogetherAI().supported_models.items():
            self.supported_models[v['endpoint'].split("/")[-1].lower()] = {'mdl_code': k, "cost": v['cost']}

    def get_models_missing_in_unify(self, notification_msgs):
        """Checks against all models in Perplexity website"""
        req = Request(
            "https://docs.together.ai/docs/inference-models",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        pricing_tables = soup.find_all("table")
        # checking if any model left
        models_missing_in_unify = []
        for table in pricing_tables[:3]:
            for row in table.find_all("tr")[1:]:
                cols = row.find_all("td")
                model_name = cols[2].text.strip().lower()
                context_len = int(cols[3].text.strip())
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
        model_size_to_pr = {}
        curr_model_size = None
        notification_msgs = []
        for table in self.pricing_tables:
            if table.h3.text == "CHat, language, and\xa0code models":
                for div in table.find_all("div", class_="pricing_content-cell"):
                    if curr_model_size is None:
                        model_size = div.find("p")
                        if model_size and "B" in model_size.text:
                            curr_model_size = model_size.text
                    else:
                        price = div.find("p", class_="text-color-tgblue")
                        if price:
                            model_size_to_pr[curr_model_size] = price.text
                            curr_model_size = None
        if model_size_to_pr == {}:
            notification_msgs.append(f"No pricing data found for {self.NAME}")
            notification_msgs.append(f"Check if table was renamed")
        for i, (size, cost) in enumerate(model_size_to_pr.items()):
            cost = float(cost[1:])
            relevant_model_endpoints = []
            # CHAT, LANGUAGE, AND CODE MODELS
            if i <= 4:
                size_range = [float(s[:-1]) for s in size.split(" ") if "B" in s]
                if len(size_range) == 1:
                    lower_range = 0
                    upper_range = size_range[0]
                elif len(size_range) == 2:
                    lower_range = size_range[0]
                    upper_range = size_range[1]
                else:
                    notification_msgs.append(f"Error in size range {self.NAME}")
                    notification_msgs.append(
                        f"Page possibly changed from model size only structure",
                    )
                for model_endpoint_name in self.supported_models:
                    if "llama" in model_endpoint_name:
                        continue
                    supported_model_size = re.findall(r"(?<!x)(\d+)b", model_endpoint_name)
                    if supported_model_size:
                        supported_model_size = float(supported_model_size[0].lower())
                        if lower_range <= supported_model_size <= upper_range:
                            relevant_model_endpoints.append(model_endpoint_name)
            # LLAMA-2 AND CODELLAMA MODELS
            elif 8 >= i >= 5:
                llama_size = float(size[:-1])
                for model_endpoint_name in self.supported_models:
                    if "llama" in model_endpoint_name:
                        supported_model_size = re.findall(r"(?<!x)(\d+)b", model_endpoint_name)
                        if supported_model_size:
                            supported_model_size = float(
                                supported_model_size[0].lower(),
                            )
                            if supported_model_size == llama_size:
                                relevant_model_endpoints.append(model_endpoint_name)
            # MIXTURE-OF-EXPERTS
            elif i >= 9:
                moe_size_from_chart = re.findall(r"(\d+X) (\d+B)", size)
                if len(moe_size_from_chart) == 0:
                    notification_msgs.append("MOE size not found in expected scrapped data")
                else:
                    moe_size_from_chart = "".join(list(moe_size_from_chart[0])).lower()
                    for model_endpoint_name in self.supported_models:
                        model_size = re.findall(r"(\d+x\d+b)", model_endpoint_name)
                        if model_size:
                            if moe_size_from_chart == model_size[0]:
                                relevant_model_endpoints.append(model_endpoint_name)

            for model_endpoint_name in relevant_model_endpoints:
                model_metadata = self.supported_models.pop(model_endpoint_name)
                mdl_code = model_metadata['mdl_code']
                cost_info = model_metadata['cost']
                if not (cost == cost_info['prompt'] == cost_info['completion']):
                    notification_msgs.append(
                        f"Model {model_endpoint_name} has different prompt and completion costs than in supported_models dict",
                    )
                if mdl_codes and mdl_code not in mdl_codes:
                    continue
                offer = RawCatalogItem(
                    model_name=model_endpoint_name,
                    in_price=cost,
                    out_price=cost,
                    request_price=None,
                )
                offers.append(offer)
        self.get_models_missing_in_unify(notification_msgs)
        if len(self.supported_models):
            notification_msgs.append(
                f"Models not in pricing page ({self.NAME}): {self.supported_models.keys()}",
            )
        return sorted(offers, key=lambda i: i.in_price), notification_msgs


if __name__ == "__main__":
    provider = TogetherAIProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)

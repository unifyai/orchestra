import logging
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.completion.anyscale import Anyscale
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem


class AnyscaleProvider(AbstractProvider):
    NAME = "anyscale"

    def __init__(self):
        req = Request(
            "https://docs.endpoints.anyscale.com/pricing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")
        self.supported_models = dict()
        for k, v in Anyscale().supported_models.items():
            self.supported_models[v['endpoint'].split("/")[-1].lower()] = {'mdl_code': k, "cost": v['cost']}


    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> (List[RawCatalogItem], List[str]):
        offers = []
        models_missing_in_unify = []
        notification_msgs = []
        for row in self.pricing_tables[0].find_all("tr")[1:]:
            cols = row.find_all("td")
            model_endpoint_name = cols[0].text.strip().lower()
            price = float(cols[1].text.strip())
            if model_endpoint_name in self.supported_models:
                model_metadata = self.supported_models.pop(model_endpoint_name)
                mdl_code = model_metadata['mdl_code']
                cost_info = model_metadata['cost']
                if not (price == cost_info['prompt'] == cost_info['completion']):
                    notification_msgs.append(
                        f"Model {model_endpoint_name} has different prompt and completion costs than in supported_models dict",
                    )
                offer = RawCatalogItem(
                    model_name=mdl_code,
                    in_price=price,
                    out_price=price,
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
                f"Models not in pricing page ({self.NAME}): {self.supported_models}",
            )
        return sorted(offers, key=lambda i: i.in_price), notification_msgs


if __name__ == "__main__":
    provider = AnyscaleProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)

from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.completion.openai_provider import OpenAI
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import RawCatalogItem


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
        self.supported_models = dict()
        for k, v in OpenAI().supported_models.items():
            self.supported_models[v["endpoint"].split("/")[-1].lower()] = {
                "mdl_code": k,
                "cost": v["cost"],
            }

    def get(
        self,
        mdl_codes: Optional[List[str]] = None,
    ) -> tuple[List[RawCatalogItem], List[str]]:
        """
        Runs with or without mdl_codes
        If mdl_codes is None, returns all pricing of all models found
        """
        offers = []
        models_missing_in_unify = []
        notification_msgs = []
        for table in self.pricing_tables:
            table_headers = table.find_all("span", class_="f-heading-5")
            if table_headers[0].text != "Model" or table_headers[1].text != "Input":
                continue
            for row in table.find_all("tr")[1:]:
                cols = row.find_all("span")
                if len(cols) == 5:
                    model_endpoint_name = cols[0].text
                    input_pr = cols[1].text[1:]
                    output_pr = cols[3].text[1:]
                elif len(cols) == 3:
                    model_endpoint_name = cols[0].text
                    input_pr = cols[1].text[1:]
                    output_pr = cols[1].text[1:]

                # standardize pricing to per million
                input_pr = float(input_pr) * 1000
                output_pr = float(output_pr) * 1000
                if model_endpoint_name in self.supported_models:
                    model_metadata = self.supported_models.pop(model_endpoint_name)
                    mdl_code = model_metadata["mdl_code"]
                    cost_info = model_metadata["cost"]
                    if (
                        input_pr != cost_info["prompt"]
                        or output_pr != cost_info["completion"]
                    ):
                        notification_msgs.append(
                            f"Model {model_endpoint_name} has different prompt and completion costs than in supported_models dict",
                        )
                    if mdl_codes and mdl_code not in mdl_codes:
                        continue
                    offer = RawCatalogItem(
                        model_name=model_endpoint_name,
                        in_price=input_pr,
                        out_price=output_pr,
                        request_price=None,
                    )
                    offers.append(offer)
                else:
                    models_missing_in_unify.append(model_endpoint_name)
        # checking if any model left
        models_missing_in_unify = []
        if models_missing_in_unify:
            notification_msgs.append(
                f"Found in pricing page but not in our list ({self.NAME}): {models_missing_in_unify}",
            )
        if len(self.supported_models):
            notification_msgs.append(
                f"Models not in pricing page ({self.NAME}): {self.supported_models.keys()}",
            )
        return offers, notification_msgs


if __name__ == "__main__":
    provider = OpenAIProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)

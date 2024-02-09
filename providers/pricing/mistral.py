import os.path as op
import re
from datetime import date
from typing import List, Optional
from urllib.request import Request, urlopen, urlretrieve

from bs4 import BeautifulSoup
from currency_converter import ECB_URL, CurrencyConverter
from providers.completion.mistral import Mistral
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import RawCatalogItem


class MistralProvider(AbstractProvider):
    NAME = "mistral-ai"

    def __init__(self):
        filename = f"ecb_{date.today():%Y%m%d}.zip"
        if not op.isfile(filename):
            urlretrieve(ECB_URL, filename)
        self.currency_rates = CurrencyConverter(filename)
        req = Request(
            "https://docs.mistral.ai/platform/pricing/",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all("table")
        self.supported_models = dict()
        for k, v in Mistral().supported_models.items():
            self.supported_models[v["endpoint"].split("/")[-1].lower()] = {
                "mdl_code": k,
                "cost": v["cost"],
            }

    def find_and_convert(self, search_str):
        match = re.findall(r"(\d+\.\d+)€", search_str)
        eur_pr = float(match[0])
        usd_pr = self.currency_rates.convert(eur_pr, "EUR", "USD")
        return eur_pr, usd_pr

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
        for table in self.pricing_tables[:1]:
            rows = table.find_all("tr")
            for row in rows[1:]:
                cols = row.find_all("td")
                model_endpoint_name = cols[0].text.strip().lower()
                input_pr_eur, inp_pr_usd = self.find_and_convert(cols[1].text.strip())
                out_pr_eur, out_pr_usd = self.find_and_convert(cols[2].text.strip())
                if model_endpoint_name in self.supported_models:
                    model_metadata = self.supported_models.pop(model_endpoint_name)
                    mdl_code = model_metadata["mdl_code"]
                    cost_info = model_metadata["cost"]
                    if cost_info.get("currency", None) == "EUR":
                        if (
                            input_pr_eur != cost_info["prompt"]
                            or out_pr_eur != cost_info["completion"]
                        ):
                            notification_msgs.append(
                                f"Model {model_endpoint_name} has different prompt and completion costs than in supported_models dict",
                            )
                            notification_msgs.append(
                                f"Prompt: {input_pr_eur} (page) vs {cost_info['prompt']} (dict), Completion: {out_pr_eur} (page) vs {cost_info['completion']} (dict)",
                            )
                    else:
                        notification_msgs.append(
                            f"Mistral model {model_endpoint_name} isn't in EUR pricing in supported_models dict",
                        )
                    if mdl_codes and mdl_code not in mdl_codes:
                        continue
                    offer = RawCatalogItem(
                        model_name=mdl_code,
                        in_price=inp_pr_usd,
                        out_price=out_pr_usd,
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
        return offers, notification_msgs


if __name__ == "__main__":
    provider = MistralProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)

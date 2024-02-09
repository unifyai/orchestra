import time
from typing import List, Optional

from providers.completion.replicate import Replicate
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import RawCatalogItem
from selenium import webdriver
from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By


class ReplicateProvider(AbstractProvider):
    NAME = "replicate"

    def __init__(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.page_load_strategy = "none"

        driver = Chrome(options=options)
        driver.implicitly_wait(5)

        url = "https://replicate.com/pricing"

        driver.get(url)
        time.sleep(5)

        content = driver.find_element(By.CSS_SELECTOR, "div[class*='space-y-lh'")
        self.rows = content.find_elements(By.TAG_NAME, "table")
        self.supported_models = dict()
        for k, v in Replicate().supported_models.items():
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
        for row in self.rows[0].text.split("\n")[1:]:
            cols = row.split(" ")
            model_endpoint_name = cols[0].split("/")[1]
            input_pr = float(cols[1][1:])
            output_pr = float(cols[5][1:])
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
    provider = ReplicateProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)

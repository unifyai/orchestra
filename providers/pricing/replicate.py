import logging
import time
from typing import List, Optional

from providers.completion.replicate import Replicate
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem
from selenium import webdriver
from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)


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
        self.supported_models = set(
            [
                x["endpoint"].split("/")[-1].lower()
                for x in Replicate().supported_models.values()
            ],
        )

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        found_models = []
        for row in self.rows[0].text.split("\n")[1:]:
            cols = row.split(" ")
            model_name = cols[0].split("/")[1]
            input_pr = float(cols[1][1:])
            output_pr = float(cols[5][1:])
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
            print(
                f"Found in pricing page but not in our list ({self.NAME}): {models_missing_in_unify}",
            )
        if self.supported_models != set():
            print(
                f"Models not in pricing table ({self.NAME}): {list(self.supported_models)}",
            )
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = ReplicateProvider()
    print(provider.get())

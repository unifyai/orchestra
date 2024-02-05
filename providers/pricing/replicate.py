import logging
import re
from typing import List, Optional
import time 
from selenium import webdriver 
from selenium.webdriver import Chrome 
from selenium.webdriver.common.by import By 
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

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

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        for row in self.rows[0].text.split("\n")[1:]:
            cols = row.split(" ")
            model_name = cols[0]
            input_pr = float(cols[1][1:])
            output_pr = float(cols[5][1:])
            offer = RawCatalogItem(
                model_name=model_name,
                in_price=input_pr,
                out_price=output_pr,
                request_price=None,
            )
            offers.append(offer)
        return sorted(offers, key=lambda i: i.in_price)



if __name__ == "__main__":
    provider = ReplicateProvider()
    print(provider.get())

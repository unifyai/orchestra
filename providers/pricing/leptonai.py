import os.path as op
from datetime import date
from typing import List, Optional
from urllib.request import urlretrieve

from currency_converter import ECB_URL, CurrencyConverter
from providers.completion.leptonai import supported_models
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import RawCatalogItem
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver import Chrome


class LeptonAIProvider(AbstractProvider):
    NAME = "lepton-ai"

    def __init__(self):
        filename = f"ecb_{date.today():%Y%m%d}.zip"
        if not op.isfile(filename):
            urlretrieve(ECB_URL, filename)
        self.currency_rates = CurrencyConverter(filename)
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.page_load_strategy = "none"

        self.driver = Chrome(options=options)
        self.driver.implicitly_wait(5)

        self.driver.get("https://www.lepton.ai/pricing")

        self.pricing_tables = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "/html/body/div/div/div/div/div[3]/div[2]/div/div/div/table"))
        )


        self.supported_models = dict()
        for k, v in supported_models.items():
            self.supported_models[v["endpoint"]] = {
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
        
        rows = self.driver.find_elements(By.XPATH, '/html/body/div/div/div/div/div[3]/div[2]/div/div/div/table/tbody/tr')



        for row in rows:
            
            model_endpoint_name = row.find_element(By.XPATH, './td[1]').text.replace(' ', '-').lower()
            price_data = row.find_element(By.XPATH, './td[2]').text
            if "token" not in price_data:
                continue
            input_pr = output_pr = float(price_data.split(' ')[0].lstrip('$'))
        
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
                    notification_msgs.append(
                        f"Prompt: {input_pr} (page) vs {cost_info['prompt']} (dict), Completion: {output_pr} (page) vs {cost_info['completion']} (dict)",
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
        self.driver.quit()
        return offers, notification_msgs


if __name__ == "__main__":
    provider = LeptonAIProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)
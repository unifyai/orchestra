import re
import os.path as op
from datetime import date
from typing import List, Optional
from urllib.request import urlretrieve

from currency_converter import ECB_URL, CurrencyConverter
from providers.completion.fireworksai import supported_models
from providers.pricing import AbstractProvider
from providers.pricing.tools.models import RawCatalogItem
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver import Chrome

class FireworksAIProvider(AbstractProvider):
    NAME = "fireworks-ai"

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

        self.driver.get("https://fireworks.ai/pricing")

        self.pricing_tables = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "/html/body/div[2]/div/table[2]"))
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
        
        rows = self.driver.find_elements(By.XPATH, '/html/body/div[2]/div/table[2]/tbody/tr')

        price_list = []
        for row in rows:
            text = row.find_element(By.XPATH, './td[1]').text
            numbers = re.findall(r"(\d+\.\d+|\d+)", text)
            limits = [float(num) for num in numbers]
            input_pr = float(row.find_element(By.XPATH, './td[2]').text.lstrip('$'))
            output_pr = float(row.find_element(By.XPATH, './td[3]').text.lstrip('$'))
            price_list.append((text, limits, input_pr, output_pr))        

        
        for model_endpoint_name in self.supported_models:
            model_metadata = self.supported_models[model_endpoint_name]
            mdl_code = model_metadata["mdl_code"]
            cost_info = model_metadata["cost"]
            model_size = float(re.search(r"(\d+)b|B", mdl_code).group(1))
            
            if "mixtral" in text.lower() and "mixtral" in mdl_code:
                for price in price_list:
                    if "mixtral" in price[0].lower():
                        input_pr = price[2]
                        output_pr = price[3]
                        break   
            else:
                for price in price_list:
                    if len(price[1]) == 1 and "up to" in price[0] and model_size < price[1][0]:
                        input_pr = price[2]
                        output_pr = price[3]
                        break
                    elif len(price[1]) == 2 and model_size > price[1][0] and model_size < price[1][1]:
                        input_pr = price[2]
                        output_pr = price[3]
                        break

            if input_pr != cost_info["prompt"] or output_pr != cost_info["completion"]:
                notification_msgs.append(
                    f"Model {model_endpoint_name} has different prompt and completion costs than in supported_models dict",
                )
                notification_msgs.append(
                    f"Prompt: {input_pr} (page) vs {cost_info['prompt']} (dict), Completion: {output_pr} (page) vs {cost_info['completion']} (dict)",
                )

            offer = RawCatalogItem(
                model_name=model_endpoint_name,
                in_price=input_pr,
                out_price=output_pr,
                request_price=None,
            )
            offers.append(offer)
        self.driver.quit()
        return offers, notification_msgs

if __name__ == "__main__":
    provider = FireworksAIProvider()
    price_data, notification_msgs = provider.get()
    print(price_data)
    print(notification_msgs)
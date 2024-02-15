from typing import List

from providers.completion.base_completion_provider import BaseCompletionProvider
from providers.pricing.tools.models import RawCatalogItem


class FireworksAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://fireworks.ai/models
    Pricing is per million tokens: https://fireworks.ai/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models
        self.name = "fireworks-ai"

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_FIREWORKS_AI_API_KEY"

    @property
    def base_url(self):
        return "https://api.fireworks.ai/inference/v1"

    def get_prices(
        self,
    ) -> tuple[List[RawCatalogItem], List[str]]:

        import re

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        self._set_currency_rates()
        driver = self._get_driver()
        driver.get("https://fireworks.ai/pricing")

        self.pricing_tables = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "/html/body/div[2]/div/table[2]"))
        )

        models = self._reformat_models()
        offers = []
        notification_msgs = []

        rows = driver.find_elements(By.XPATH, "/html/body/div[2]/div/table[2]/tbody/tr")

        price_list = []
        for row in rows:
            text = row.find_element(By.XPATH, "./td[1]").text
            numbers = re.findall(r"(\d+\.\d+|\d+)", text)
            limits = [float(num) for num in numbers]
            input_pr = float(row.find_element(By.XPATH, "./td[2]").text.lstrip("$"))
            output_pr = float(row.find_element(By.XPATH, "./td[3]").text.lstrip("$"))
            price_list.append((text, limits, input_pr, output_pr))

        for model_endpoint_name in models:
            model_metadata = models[model_endpoint_name]
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
                    if (
                        len(price[1]) == 1
                        and "up to" in price[0]
                        and model_size < price[1][0]
                    ):
                        input_pr = price[2]
                        output_pr = price[3]
                        break
                    elif (
                        len(price[1]) == 2
                        and model_size > price[1][0]
                        and model_size < price[1][1]
                    ):
                        input_pr = price[2]
                        output_pr = price[3]
                        break

            if input_pr != cost_info["prompt"] or output_pr != cost_info["completion"]:
                notification_msgs = self._notify_cost_discrepancy(
                    notification_msgs,
                    model_endpoint_name,
                    input_pr,
                    output_pr,
                    cost_info,
                )

            offer = RawCatalogItem(
                model_name=model_endpoint_name,
                in_price=input_pr,
                out_price=output_pr,
                request_price=None,
            )
            offers.append(offer)
        driver.quit()
        return offers, notification_msgs


supported_models = {
    "llama-2-7b": {
        "endpoint": "accounts/fireworks/models/llama-v2-7b",
        "context_window": 4096,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "llama-2-13b": {
        "endpoint": "accounts/fireworks/models/llama-v2-13b",
        "context_window": 4096,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "llama-2-70b": {
        "endpoint": "accounts/fireworks/models/llama-v2-70b",
        "context_window": 4096,
        "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
    },
    "llama-2-7b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v2-7b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "llama-2-13b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v2-13b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "llama-2-70b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v2-70b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
    },
    "mistral-7b-v0.1": {
        "endpoint": "accounts/fireworks/models/mistral-7b",
        "context_window": 16384,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "mistral-7b-instruct-v0.1": {
        "endpoint": "accounts/fireworks/models/mistral-7b-instruct-4k",
        "context_window": 16384,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "accounts/fireworks/models/mixtral-8x7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.40, "completion": 1.60},  # noqa: WPS339
    },
    "falcon-7b": {
        "endpoint": "accounts/fireworks/models/falcon-7b",
        "context_window": 2048,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "falcon-40b": {
        "endpoint": "accounts/fireworks/models/falcon-40b",
        "context_window": 2048,
        "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
    },
    "codellama-70b-instruct": {
        "endpoint": "accounts/fireworks/models/llama-v2-70b-code-instruct",
        "context_window": 4096,
        "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
    },
    "codellama-34b-instruct": {
        "endpoint": "accounts/fireworks/models/llama-v2-34b-code-instruct",
        "context_window": 16384,
        "cost": {"prompt": 0.70, "completion": 2.80},  # noqa: WPS339
    },
    "codellama-13b-instruct": {
        "endpoint": "accounts/fireworks/models/llama-v2-13b-code-instruct",
        "context_window": 4096,
        "cost": {"prompt": 0.20, "completion": 0.80},  # noqa: WPS339
    },
    "zephyr-7b-beta": {
        "endpoint": "accounts/fireworks/models/zephyr-7b-beta",
        "context_window": 16384,
        "cost": {"prompt": 0.20, "completion": 0.80},
    },
}

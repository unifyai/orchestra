from typing import List, Optional

from providers.completion.base_completion_provider import BaseCompletionProvider
from providers.pricing.tools.models import RawCatalogItem


class Deepinfra(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://deepinfra.com/pricing
    Pricing is per million tokens: https://deepinfra.com/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models
        self.name = "deepinfra"

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_DEEPINFRA_API_KEY"

    @property
    def base_url(self):
        return "https://api.deepinfra.com/v1/openai"

    def get_prices(
        self,
        mdl_codes: Optional[List[str]] = None,
    ) -> tuple[List[RawCatalogItem], List[str]]:
        """
        Runs with or without mdl_codes
        If mdl_codes is None, returns all pricing of all models found
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        self._set_currency_rates()
        driver = self._get_driver()

        driver.get("https://deepinfra.com/pricing")

        self.pricing_tables = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "pricing"))
        )

        models = self._reformat_models()
        offers = []
        models_missing_in_unify = []
        notification_msgs = []

        rows = driver.find_elements(
            By.XPATH, '//*[@id="pricing"]/div[1]/div/div[5]/table/tbody/tr'
        )

        for row in rows:
            model_endpoint_name = (
                row.find_element(By.XPATH, "./th/a")
                .get_attribute("href")
                .replace("https://deepinfra.com/", "")
            )
            input_pr = float(row.find_element(By.XPATH, "./td[2]").text.lstrip("$"))
            output_pr = float(row.find_element(By.XPATH, "./td[3]").text.lstrip("$"))

            if model_endpoint_name in models:
                model_metadata = models.pop(model_endpoint_name)
                mdl_code = model_metadata["mdl_code"]
                cost_info = model_metadata["cost"]
                if (
                    input_pr != cost_info["prompt"]
                    or output_pr != cost_info["completion"]
                ):
                    notification_msgs = self._notify_cost_discrepancy(
                        model_endpoint_name,
                        input_pr,
                        output_pr,
                        cost_info,
                        notification_msgs,
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
                self._notify_missing_models(models_missing_in_unify)
            )

        if len(models):
            notification_msgs.append(self._notify_missing_prices(models))
        driver.quit()
        return offers, notification_msgs


supported_models = {
    "llama-2-7b-chat": {
        "endpoint": "meta-llama/Llama-2-7b-chat-hf",
        "context_window": 4096,
        "cost": {"prompt": 0.13, "completion": 0.13},
    },
    "llama-2-13b-chat": {
        "endpoint": "meta-llama/Llama-2-13b-chat-hf",
        "context_window": 4096,
        "cost": {"prompt": 0.22, "completion": 0.22},
    },
    "llama-2-70b-chat": {
        "endpoint": "meta-llama/Llama-2-70b-chat-hf",
        "context_window": 4096,
        "cost": {"prompt": 0.70, "completion": 0.90},  # noqa: WPS339
    },
    "mistral-7b-instruct-v0.1": {
        "endpoint": "mistralai/Mistral-7B-Instruct-v0.1",
        "context_window": 32768,
        "cost": {"prompt": 0.13, "completion": 0.13},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "context_window": 32768,
        "cost": {"prompt": 0.27, "completion": 0.27},  # noqa: WPS339
    },
    "codellama-34b-instruct": {
        "endpoint": "codellama/CodeLlama-34b-Instruct-hf",
        "context_window": 16384,
        "cost": {"prompt": 0.60, "completion": 0.60},  # noqa: WPS339
    },
    "phind-codellama-34b-v2": {
        "endpoint": "Phind/Phind-CodeLlama-34B-v2",
        "context_window": 16384,
        "cost": {"prompt": 0.60, "completion": 0.60},  # noqa: WPS339
    },
    "mythomax-l2-13b": {
        "endpoint": "Gryphe/MythoMax-L2-13b",
        "context_window": 4096,
        "cost": {"prompt": 0.22, "completion": 0.22},
    },
    "yi-34b-chat": {
        "endpoint": "01-ai/Yi-34B-Chat",
        "context_window": 4096,
        "cost": {"prompt": 0.60, "completion": 0.60},  # noqa: WPS339
    },
}

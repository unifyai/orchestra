import time
from typing import Dict, List, Optional

from providers.completion.base_completion_provider import BaseCompletionProvider
from providers.pricing.tools.models import RawCatalogItem


class LeptonAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://www.lepton.ai/playground
    Pricing is per million tokens: https://www.lepton.ai/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models
        self.name = "lepton-ai"

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_LEPTON_AI_API_KEY"

    @property
    def base_url(self):
        return "https://{0}.lepton.run/api/v1/".format(self.provider_endpoint)

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

        driver.get("https://www.lepton.ai/pricing")

        self.pricing_tables = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "/html/body/div/div/div/div/div[3]/div[2]/div/div/div/table")
            )
        )

        models = self._reformat_models()
        offers = []
        models_missing_in_unify = []
        notification_msgs = []

        rows = driver.find_elements(
            By.XPATH,
            "/html/body/div/div/div/div/div[3]/div[2]/div/div/div/table/tbody/tr",
        )

        for row in rows:

            model_endpoint_name = (
                row.find_element(By.XPATH, "./td[1]").text.replace(" ", "-").lower()
            )
            price_data = row.find_element(By.XPATH, "./td[2]").text
            if "token" not in price_data:
                continue
            input_pr = output_pr = float(price_data.split(" ")[0].lstrip("$"))

            if model_endpoint_name in models:
                model_metadata = models.pop(model_endpoint_name)
                mdl_code = model_metadata["mdl_code"]
                cost_info = model_metadata["cost"]
                if (
                    input_pr != cost_info["prompt"]
                    or output_pr != cost_info["completion"]
                ):
                    self._notify_cost_discrepancy(
                        model_endpoint_name,
                        input_pr,
                        output_pr,
                        cost_info["prompt"],
                        cost_info["completion"],
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
            self._notify_missing_models(models_missing_in_unify)

        if len(models):
            self._notify_missing_prices(models)
        driver.quit()
        return offers, notification_msgs

    def _modify_output(self, out: Dict, stream: bool) -> Dict:
        out["created"] = int(time.time())
        out["object"] = "chat.completion"
        if stream:
            out["object"] = "chat.completion.chunk"
        return out


supported_models = {
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mixtral-8x7b",
        "context_window": 32768,
        "cost": {"prompt": 0.5, "completion": 0.5},
    },
    "llama-2-7b-chat": {
        "endpoint": "llama2-7b",
        "context_window": 4096,
        "cost": {"prompt": 0.1, "completion": 0.1},
    },
    "llama-2-13b-chat": {
        "endpoint": "llama2-13b",
        "context_window": 4096,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
    "llama-2-70b-chat": {
        "endpoint": "llama2-70b",
        "context_window": 4096,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
}

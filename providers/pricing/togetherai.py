import logging
import re
from typing import List, Optional
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from providers.pricing import AbstractProvider
from providers.completion.togetherai import TogetherAI
from providers.pricing.tools.models import QueryFilter, RawCatalogItem

logger = logging.getLogger(__name__)


class TogetherAIProvider(AbstractProvider):
    NAME = "together-ai"

    def __init__(self):
        req = Request(
            "https://www.together.ai/pricing",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html_page = urlopen(req).read()
        soup = BeautifulSoup(html_page, "html.parser")
        self.pricing_tables = soup.find_all('ul', class_='pricing-list w-list-unstyled')
        self.togetherai_models = set(TogetherAI().supported_models)

    def get(
        self,
        query_filter: Optional[QueryFilter] = None,
        balance_resources: bool = True,
    ) -> List[RawCatalogItem]:
        offers = []
        model_size_to_pr = {}
        curr_model_size = None
        for table in self.pricing_tables:
            if table.h3.text == 'CHat, language, and\xa0code models':
                for div in table.find_all('div', class_='pricing_content-cell'):
                    if curr_model_size is None:
                        model_size = div.find("p")
                        if model_size and "B" in model_size.text:
                            curr_model_size = model_size.text
                    else:
                        price = div.find("p", class_='text-color-tgblue')
                        if price:
                            model_size_to_pr[curr_model_size] = price.text
                            curr_model_size = None
        for i, (size, cost) in enumerate(model_size_to_pr.items()):
            cost = float(cost[1:])
            relevant_models = []
            # CHAT, LANGUAGE, AND CODE MODELS
            if i <= 4:
                # print(i, size, cost)
                size_range = [float(s[:-1]) for s in size.split(" ") if "B" in s]
                # print(size_range)
                if len(size_range) == 1:
                    lower_range = 0
                    upper_range = size_range[0]
                elif len(size_range) == 2:
                    lower_range = size_range[0]
                    upper_range = size_range[1]
                else:
                    print("Error in size range")
                for model_name in self.togetherai_models:
                    if "llama" in model_name:
                        continue
                    model_size = re.findall(r"(?<!x)(\d+)b", model_name)
                    if model_size:
                        model_size = float(model_size[0])
                        if lower_range <= model_size <= upper_range:
                            relevant_models.append(model_name)
            # LLAMA-2 AND CODELLAMA MODELS
            elif 8 >= i >=5:
                llama_size = float(size[:-1])
                for model_name in self.togetherai_models:
                    if "llama" in model_name:
                        model_size = re.findall(r"(?<!x)(\d+)b", model_name)
                        if model_size:
                            model_size = float(model_size[0])
                            if model_size == llama_size:
                                relevant_models.append(model_name)
            # MIXTURE-OF-EXPERTS
            elif i >= 9:
                moe_size_from_chart = re.findall(r"(\d+X) (\d+B)", size)
                if len(moe_size_from_chart) == 0:
                    print('MOE size not found in expected scrapped data')
                else:
                    moe_size_from_chart = ''.join(list(moe_size_from_chart[0])).lower()
                    for model_name in self.togetherai_models:
                        model_size = re.findall(r"(\d+x\d+b)", model_name)
                        if model_size:
                            if moe_size_from_chart == model_size[0]:
                                relevant_models.append(model_name)

            for model_name in relevant_models:
                self.togetherai_models.remove(model_name)
                offer = RawCatalogItem(
                    model_name=model_name,
                    in_price=cost,
                    out_price=cost,
                    request_price=None,
                )
                offers.append(offer)
        return sorted(offers, key=lambda i: i.in_price)


if __name__ == "__main__":
    provider = TogetherAIProvider()
    print(provider.get())
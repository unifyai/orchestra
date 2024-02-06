from providers.pricing.anyscale import AnyscaleProvider
from providers.pricing.mistral import MistralProvider
from providers.pricing.octoai_price import OctoAIProvider
from providers.pricing.openai_price import OpenAIProvider
from providers.pricing.perplexity import PerplexityProvider
from providers.pricing.replicate import ReplicateProvider
from providers.pricing.togetherai import TogetherAIProvider

for provider in [
    AnyscaleProvider,
    MistralProvider,
    OctoAIProvider,
    OpenAIProvider,
    PerplexityProvider,
    ReplicateProvider,
    TogetherAIProvider,
]:
    try:
        print(f"Scrapping {provider.NAME} pricing page...")
        scrape_obj = provider()
    except Exception as e:
        print(f"Failed to scrape {provider.NAME}: {e}")
        continue
    # try-except on the entire get block instead of making it per model
    # inside get block is intentional
    # if anything goes wrong for a singular model, chances are site structure was
    # changed, in which case, code needs to be updated
    try:
        print(f"Extracting data...")
        print(scrape_obj.get())
        print("Done")
    except Exception as e:
        print(f"Failed to get {provider.NAME}: {e}")
    print("=====================================")

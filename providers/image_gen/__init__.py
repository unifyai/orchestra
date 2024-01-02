"""providers.image_gen package."""
from providers.image_gen.stability import Stability
from providers.image_gen.octoai import OctoAI

PROVIDER_CLASSES = {
    "stability": Stability,
    "octoai": OctoAI,    
}

# Pricing info of providers with pay-per-token model is
# standardized to per million tokens.
PRICING_PER_TOKENS = 1000000

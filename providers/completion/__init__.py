"""providers.completion package."""
from providers.completion.anthropic import Anthropic
from providers.completion.anyscale import Anyscale
from providers.completion.openai import OpenAI
from providers.completion.perplexity import Perplexity
from providers.completion.replicate import Replicate
from providers.completion.togetherai import TogetherAI
from providers.completion.vertexai import VertexAI
from providers.completion.mistral import Mistral

PROVIDER_CLASSES = {
    "anyscale": Anyscale,
    "perplexity": Perplexity,
    "together_ai": TogetherAI,
    "anthropic": Anthropic,
    "replicate": Replicate,
    "vertexai": VertexAI,
    "openai": OpenAI,
    "mistral": Mistral,
}

# Pricing info of providers with pay-per-token model is
# standardized to per million tokens.
PRICING_PER_TOKENS = 1000000

"""providers.completion package."""
from providers.completion.anthropic import Anthropic
from providers.completion.anyscale import Anyscale
from providers.completion.mistral import Mistral
from providers.completion.octoai import OctoAI
from providers.completion.openai import OpenAI
from providers.completion.perplexity import Perplexity
from providers.completion.replicate import Replicate
from providers.completion.togetherai import TogetherAI
from providers.completion.vertexai import VertexAI

PROVIDER_CLASSES = {
    "Anyscale": Anyscale,
    "Perplexity AI": Perplexity,
    "Together AI": TogetherAI,
    "Anthropic": Anthropic,
    "Replicate": Replicate,
    "Vertex AI": VertexAI,
    "OpenAI": OpenAI,
    "Mistral AI": Mistral,
    "OctoAI": OctoAI,
}

# Pricing info of providers with pay-per-token model is
# standardized to per million tokens.
PRICING_PER_TOKENS = 1000000

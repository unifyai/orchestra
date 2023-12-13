"""providers.completion package."""
from providers.completion.anthropic import Anthropic
from providers.completion.anyscale import Anyscale
from providers.completion.openai import OpenAI
from providers.completion.perplexity import Perplexity
from providers.completion.replicate import Replicate
from providers.completion.togetherai import TogetherAI
from providers.completion.vertexai import VertexAI

PROVIDER_CLASSES = {
    "anyscale": Anyscale,
    "perplexity": Perplexity,
    "together_ai": TogetherAI,
    "anthropic": Anthropic,
    "replicate": Replicate,
    "vertexai": VertexAI,
    "openai": OpenAI,
}

PRICING_PER_TOKENS = 1000000

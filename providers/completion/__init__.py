"""providers.completion package."""
from providers.completion.anthropic import Anthropic
from providers.completion.anyscale import Anyscale
from providers.completion.deepinfra import Deepinfra
from providers.completion.fireworksai import FireworksAI
from providers.completion.leptonai import LeptonAI
from providers.completion.mistral import Mistral
from providers.completion.octoai import OctoAI
from providers.completion.openai import OpenAI
from providers.completion.perplexity import Perplexity
# from providers.completion.replicate import Replicate
from providers.completion.togetherai import TogetherAI
from providers.completion.vertexai import VertexAI

PROVIDER_CLASSES = {
    "anyscale": Anyscale,
    "perplexity-ai": Perplexity,
    "together-ai": TogetherAI,
    "anthropic": Anthropic,
    # "replicate": Replicate,
    "vertex-ai": VertexAI,
    "openai": OpenAI,
    "mistral-ai": Mistral,
    "octoai": OctoAI,
    "lepton-ai": LeptonAI,
    "fireworks-ai": FireworksAI,
    "deepinfra": Deepinfra,
}

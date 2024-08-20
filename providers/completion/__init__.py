"""providers.completion package."""

from providers.completion.anthropic import Anthropic
from providers.completion.azureai import AzureAI
from providers.completion.bedrock import AWSBedrock
from providers.completion.custom_provider import CustomProvider
from providers.completion.deepinfra import Deepinfra
from providers.completion.fireworksai import FireworksAI
from providers.completion.groq import Groq
from providers.completion.leptonai import LeptonAI
from providers.completion.mistral import Mistral
from providers.completion.octoai import OctoAI
from providers.completion.openai import OpenAI
from providers.completion.perplexity import Perplexity
from providers.completion.replicate import Replicate
from providers.completion.togetherai import TogetherAI
from providers.completion.vertexai import VertexAI

PROVIDER_CLASSES = {
    "perplexity-ai": Perplexity,
    "together-ai": TogetherAI,
    "anthropic": Anthropic,
    "replicate": Replicate,
    "openai": OpenAI,
    "mistral-ai": Mistral,
    "octoai": OctoAI,
    "groq": Groq,
    "lepton-ai": LeptonAI,
    "fireworks-ai": FireworksAI,
    "deepinfra": Deepinfra,
    "aws-bedrock": AWSBedrock,
    "vertex-ai": VertexAI,
    "azure-ai": AzureAI,
    "custom": CustomProvider,
}

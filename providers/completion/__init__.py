"""providers.completion package."""

from providers.completion.anthropic import Anthropic
from providers.completion.bedrock import AWSBedrock
from providers.completion.custom_provider import CustomProvider
from providers.completion.deepinfra import Deepinfra
from providers.completion.deepseek import DeepSeek
from providers.completion.fireworksai import FireworksAI
from providers.completion.groq import Groq
from providers.completion.leptonai import LeptonAI
from providers.completion.mistral import Mistral
from providers.completion.openai import OpenAI
from providers.completion.replicate import Replicate
from providers.completion.togetherai import TogetherAI
from providers.completion.vertexai import VertexAI
from providers.completion.xai import XAI

PROVIDER_CLASSES = {
    "together-ai": TogetherAI,
    "anthropic": Anthropic,
    "replicate": Replicate,
    "openai": OpenAI,
    "mistral-ai": Mistral,
    "groq": Groq,
    "lepton-ai": LeptonAI,
    "fireworks-ai": FireworksAI,
    "deepinfra": Deepinfra,
    "deepseek": DeepSeek,
    "aws-bedrock": AWSBedrock,
    "vertex-ai": VertexAI,
    "xai": XAI,
    "custom": CustomProvider,
}

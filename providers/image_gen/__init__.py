"""providers.image_gen package."""
from providers.image_gen.octoai import OctoAI
from providers.image_gen.stability import Stability

PROVIDER_CLASSES = {
    "stability": Stability,
    "octoai": OctoAI,
}

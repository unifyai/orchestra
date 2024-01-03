from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseImageGenProvider(ABC):
    """Base class for image generation providers."""

    supported_models: List[str] = []

    def __init__(self) -> None:
        self.model: str = ""

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        self.api_key = api_key

    @abstractmethod
    def imagegen(  # noqa: D102
        self,
        prompt: str,
        model: str,
        kwargs: Optional[Dict],
    ) -> Optional[Any]:
        pass  # noqa: WPS420

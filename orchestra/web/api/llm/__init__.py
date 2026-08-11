"""Server-side LLM gateway.

Brokers provider LLM calls so assistant containers never hold a raw provider
API key. Callers authenticate with their existing Unify API key; Orchestra
holds the provider credentials, meters actual usage, and deducts credits.
"""

from orchestra.web.api.llm.views import router

__all__ = ["router"]

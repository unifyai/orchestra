"""Local echo provider used when live execution is not configured."""

from __future__ import annotations

from orchestra.integrations.providers.base import (
    BaseIntegrationProviderAdapter,
    ProviderExecutionRequest,
    ProviderExecutionResult,
)


class LocalEchoProviderAdapter(BaseIntegrationProviderAdapter):
    """Development fallback for deterministic local execution envelopes."""

    backend_id = "local_echo"

    def execute(self, request: ProviderExecutionRequest) -> ProviderExecutionResult:
        return ProviderExecutionResult(
            status="ok",
            result={
                "provider": request.backend_id,
                "provider_tool_id": request.provider_tool_id,
                "connection_id": request.connection_id,
                "arguments": request.arguments,
                "message": "Provider execution adapter boundary reached.",
            },
        )

    def health_check(
        self,
        request: ProviderExecutionRequest,
    ) -> ProviderExecutionResult:
        return ProviderExecutionResult(
            status="ok",
            result={
                "provider": request.backend_id,
                "connection_id": request.connection_id,
                "message": "Provider health check adapter boundary reached.",
            },
        )

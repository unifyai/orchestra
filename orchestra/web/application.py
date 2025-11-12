import logging
from importlib import metadata

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import UJSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from orchestra.settings import settings
from orchestra.web.api.router import api_router

# from orchestra.web.api.utils.production_traffic_middleware import (
#     ProductionTrafficMiddleware,
# )
from orchestra.web.api.utils.prometheus_middleware import PrometheusMiddleware, metrics
from orchestra.web.lifetime import register_shutdown_event, register_startup_event


def get_app() -> FastAPI:
    """
    Get FastAPI application.

    This is the main factory function of the application.

    :return: application.
    """
    if settings.sentry_dsn:
        # Enables sentry integration.
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_sample_rate,
            environment=settings.environment,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(
                    level=logging.INFO,
                    event_level=logging.ERROR,
                ),
                SqlalchemyIntegration(),
            ],
        )
    app = FastAPI(
        title="UnifyAI HTTP API Reference",
        version=metadata.version("orchestra"),
        redoc_url="/v0/redoc",
        openapi_url="/v0/openapi.json",
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
        default_response_class=UJSONResponse,
    )
    # Set up CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Add Prometheus metrics middleware
    app.add_middleware(
        PrometheusMiddleware,
        app_name="orchestra",
    )
    # Add Production Traffic middleware
    # app.add_middleware(
    #     ProductionTrafficMiddleware,
    # )
    # Register startup and shutdown events
    register_startup_event(app)
    register_shutdown_event(app)

    # Register API router
    app.include_router(router=api_router, prefix="/v0")

    # Add Prometheus metrics endpoint
    app.add_api_route("/metrics", metrics)

    return app

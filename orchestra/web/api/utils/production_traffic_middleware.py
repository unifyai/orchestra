import logging
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from orchestra.settings import settings
from orchestra.web.api.utils.gcp import send_pubsub_msg

logger = logging.getLogger(__name__)


async def log_production_traffic(
    user_id: Optional[int],
    email: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    request_path: str,
    request_method: str,
    request_timestamp: float,
    response_timestamp: float,
    status_code: int,
    time_taken: float,
    sql_trace: List[Dict[str, Any]],
    request_fastapi: Request,
) -> None:
    """
    Publish production traffic logs to PubSub for asynchronous processing.

    Args:
        user_id: The ID of the user making the request
        email: The email of the user
        first_name: The first name of the user
        last_name: The last name of the user
        request_path: The path of the request
        request_method: The HTTP method of the request
        request_timestamp: The timestamp when the request was received
        response_timestamp: The timestamp when the response was sent
        status_code: The HTTP status code of the response
        time_taken: The time taken to process the request in milliseconds
        sql_trace: SQL trace data captured during request processing
        request_fastapi: The original FastAPI request object
    """
    try:
        # Prepare log entries as a message for PubSub
        entries = {
            "user_id": user_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "endpoint_name": request_path,
            "method": request_method,
            "request_timestamp": datetime.fromtimestamp(request_timestamp).isoformat(),
            "response_timestamp": datetime.fromtimestamp(
                response_timestamp,
            ).isoformat(),
            "time_taken(s)": time_taken,
            "sql_trace": sql_trace or [],
            "status_code": status_code,
        }

        # Send to PubSub
        topic = f"projects/{settings.traffic_log_pubsub_project_id}/topics/{settings.traffic_log_pubsub_topic}"
        logger.info(f"Sending traffic log to PubSub: {topic} for path: {request_path}")
        send_pubsub_msg(topic, entries)
        logger.debug(
            f"Successfully sent traffic log to PubSub for path: {request_path}",
        )
    except Exception as e:
        logger.error(f"Error sending traffic log to PubSub: {str(e)}")
        traceback.print_exc()


class ProductionTrafficMiddleware(BaseHTTPMiddleware):
    """
    Middleware that captures production traffic data and logs it.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if any(
            s in request.url.path for s in ["/metrics", "/v0/admin", "/v0/webhooks"]
        ):
            return await call_next(request)
        # Initialize timestamps and data
        request_timestamp = time.time()
        status_code = 500  # Default status code in case of unhandled errors

        # Get request path and method
        request_path = request.url.path
        request_method = request.method

        try:
            # Execute the original endpoint
            response = await call_next(request)

            # Capture response timestamp
            response_timestamp = time.time()

            # Get response data and status code
            status_code = response.status_code

        except Exception as exc:
            # Capture response timestamp for errors too
            response = None
            response_timestamp = time.time()
            # Re-raise the exception to maintain original behavior
            raise

        finally:
            # Calculate time taken in milliseconds
            response_timestamp = time.time()
            time_taken = response_timestamp - request_timestamp

            # Get SQL trace data if available
            sql_trace = getattr(request.state, "sql_trace", [])

            # Use background tasks to avoid impacting response time
            # Get user information from request state if available
            user_id = getattr(request.state, "user_id", None)
            email = getattr(request.state, "email", None)
            first_name = getattr(request.state, "first_name", None)
            last_name = getattr(request.state, "last_name", None)
            # Create a new BackgroundTasks instance for this request
            tasks = BackgroundTasks()
            tasks.add_task(
                log_production_traffic,
                user_id=user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                request_path=request_path,
                request_method=request_method,
                request_timestamp=request_timestamp,
                response_timestamp=response_timestamp,
                status_code=status_code,
                time_taken=time_taken,
                sql_trace=sql_trace,
                request_fastapi=request,
            )

            # Add background task to response
            if isinstance(response, Response):
                response.background = tasks

        return response

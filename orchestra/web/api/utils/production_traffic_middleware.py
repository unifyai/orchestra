import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.settings import settings
from orchestra.web.api.log.views import CreateLogConfig, create_logs_internal


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
    Write production traffic logs to the 'Production Traffic' project.

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
        session_generator = get_db_session(request_fastapi)
        session = next(session_generator)

        # Initialize DAOs
        project_dao = ProjectDAO(session)
        log_event_dao = LogEventDAO(session)
        log_dao = LogDAO(session)
        field_type_dao = FieldTypeDAO(session)
        context_dao = ContextDAO(session)
        org_dao = OrganizationDAO(session)
        # Get the Production Traffic project
        orgs = org_dao.filter(
            name=settings.orchestra_organization_name,
            owner_id=settings.orchestra_owner_id,
        )
        if not orgs:
            return
        org_id = orgs[0][0].id
        projects = project_dao.filter(
            name=settings.orchestra_prod_traffic_name,
            organization_id=org_id,
        )
        if not projects:
            return

        project_id = projects[0][0].id
        project_name = settings.orchestra_prod_traffic_name

        # Prepare log entries
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

        # Call create_logs_internal directly to avoid HTTP call and middleware recursion
        context_id = context_dao.get_or_create(
            project_id,
            name="",
            description=None,
            is_versioned=False,
        )
        event_ids = create_logs_internal(
            project_id=project_id,
            context_id=context_id,
            request=CreateLogConfig(
                entries=entries,
                project=project_name,
                context=None,
            ),
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            log_event_dao=log_event_dao,
            log_dao=log_dao,
            context_dao=context_dao,
        )
    except Exception as e:
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
        if any(s in request.url.path for s in ["/metrics", "/v0/admin"]):
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

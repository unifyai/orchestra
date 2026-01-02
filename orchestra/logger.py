import logging
import sys
from typing import Any, Dict

from loguru import logger
from opentelemetry import trace
from pythonjsonlogger import jsonlogger

try:
    import logging_loki

    LOKI_AVAILABLE = True
except ImportError:
    LOKI_AVAILABLE = False

import requests

from orchestra.settings import settings
from orchestra.web.api.utils.observability import (
    get_request_id,
    get_user_email,
    get_user_id,
)

# Include request_id in log format
JSON_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s %(module)s %(funcName)s %(lineno)d %(traceID)s %(spanID)s %(user_id)s %(user_email)s %(request_id)s"

# Connection timeout for external services (in seconds)
CONNECTION_TIMEOUT = 2.0

# Loguru format for pretty console output in development
LOGURU_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "| <level>{level: <8}</level> "
    "| <magenta>traceID={extra[traceID]}</magenta> "
    "| <blue>spanID={extra[spanID]}</blue> "
    "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
    "- <level>{message}</level>\n"
)


class TraceUserContextFilter(logging.Filter):
    """
    Logging filter that adds trace context and user information to log records.
    This ensures that all logs can be correlated with traces and user actions.
    """

    def __init__(self, name: str = ""):
        super().__init__(name)

    def filter(self, record):
        # Extract trace context from OpenTelemetry
        span = trace.get_current_span()
        context = span.get_span_context()
        # Format trace and span IDs as hexadecimal strings
        if hasattr(context, "trace_id") and context.trace_id:
            record.trace_id = f"{context.trace_id:032x}"
        else:
            record.trace_id = "00" * 16  # Default trace ID (all zeros)

        if hasattr(context, "span_id") and context.span_id:
            record.span_id = f"{context.span_id:016x}"
        else:
            record.span_id = "00" * 8  # Default span ID (all zeros)

        # Add user context from context variables
        record.user_id = get_user_id() or "anonymous"
        record.user_email = get_user_email() or "unknown"

        # Add request ID from context
        record.request_id = get_request_id() or "unknown"

        # Add any additional attributes from the span
        if hasattr(span, "attributes"):
            for key, value in span.attributes.items():
                if not hasattr(record, key):
                    setattr(record, key, value)

        return True


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """
    Custom JSON formatter that adds additional fields to the log record.
    """

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)

        # Add timestamp in ISO format
        log_record["timestamp"] = record.created
        log_record["level"] = record.levelname

        # Add service name
        log_record["service"] = "orchestra"

        # Add environment
        log_record["environment"] = settings.environment

        # Ensure trace context is included
        if not log_record.get("traceID") and hasattr(record, "traceID"):
            log_record["traceID"] = record.traceID

        if not log_record.get("spanID") and hasattr(record, "spanID"):
            log_record["spanID"] = record.spanID

        # Ensure user context is included
        if not log_record.get("user_id") and hasattr(record, "user_id"):
            log_record["user_id"] = record.user_id

        if not log_record.get("user_email") and hasattr(record, "user_email"):
            log_record["user_email"] = record.user_email

        # Ensure request ID is included
        if not log_record.get("request_id") and hasattr(record, "request_id"):
            log_record["request_id"] = record.request_id


class InterceptHandler(logging.Handler):
    """
    Intercepts standard logging and redirects to loguru.
    This allows us to use loguru's pretty formatting for all logs.
    """

    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        # Extract context from record
        trace_id = getattr(record, "traceID", "00" * 16)
        span_id = getattr(record, "spanID", "00" * 8)
        user_id = getattr(record, "user_id", "anonymous")
        user_email = getattr(record, "user_email", "unknown")
        request_id = getattr(record, "request_id", "unknown")

        # Log to loguru with context
        logger.opt(depth=depth, exception=record.exc_info).log(
            level,
            record.getMessage(),
            trace_id=trace_id,
            span_id=span_id,
            user_id=user_id,
            user_email=user_email,
            request_id=request_id,
        )


class StructuredLogger:
    """Logger wrapper that adds structured context to all log messages."""

    def __init__(self):
        self.logger = logging.getLogger()

    def log(self, level: int, msg: str, extra: Dict[str, Any] = None):
        """
        Log a message with additional structured context.

        Args:
            level: Logging level (e.g., logging.INFO)
            msg: Log message
            extra: Additional fields to include in the structured log
        """
        if extra is None:
            extra = {}

        # Add user context if available
        user_id = get_user_id()
        if user_id and "user_id" not in extra:
            extra["user_id"] = user_id

        user_email = get_user_email()
        if user_email and "user_email" not in extra:
            extra["user_email"] = user_email

        # Add request ID if available
        request_id = get_request_id()
        if request_id and "request_id" not in extra:
            extra["request_id"] = request_id

        # Get trace context
        span = trace.get_current_span()
        context = span.get_span_context()
        if hasattr(context, "trace_id") and context.trace_id:
            trace_id = f"{context.trace_id:032x}"
            if "traceID" not in extra:
                extra["traceID"] = trace_id

        if hasattr(context, "span_id") and context.span_id:
            span_id = f"{context.span_id:016x}"
            if "spanID" not in extra:
                extra["spanID"] = span_id

        # Log with all context
        self.logger.log(level, msg, extra=extra)

    def info(self, msg: str, extra: Dict[str, Any] = None):
        self.log(logging.INFO, msg, extra)

    def warning(self, msg: str, extra: Dict[str, Any] = None):
        self.log(logging.WARNING, msg, extra)

    def error(self, msg: str, extra: Dict[str, Any] = None):
        self.log(logging.ERROR, msg, extra)

    def debug(self, msg: str, extra: Dict[str, Any] = None):
        self.log(logging.DEBUG, msg, extra)


def is_service_available(url, timeout=CONNECTION_TIMEOUT):
    """Check if a service is available by making a HEAD request"""
    if not url:
        return False

    try:
        # Extract base URL without path
        base_url = url.split("/v1")[0] if "/v1" in url else url

        # Special handling for Tempo
        if "tempo" in base_url.lower() or ":4317" in base_url or ":4318" in base_url:
            # For Tempo, we just check if the port is open
            import socket
            from urllib.parse import urlparse

            parsed_url = urlparse(base_url)
            host = parsed_url.hostname or "localhost"
            port = parsed_url.port or 4317

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()

            return result == 0

        # Special handling for Loki
        if "loki" in base_url.lower() or ":3100" in base_url:
            # Try a GET request to /ready endpoint
            loki_ready_url = f"{base_url}/ready"
            try:
                response = requests.get(loki_ready_url, timeout=timeout)
                if response.status_code < 400:
                    return True
            except:
                pass

            # Try a GET request to the root as fallback
            try:
                response = requests.get(base_url, timeout=timeout)
                # Loki often returns 404 for root but is still running
                return response.status_code < 400 or response.status_code == 404
            except:
                return False

        # Default handling for other services
        requests.head(base_url, timeout=timeout)
        return True
    except (requests.RequestException, ConnectionError):
        return False


def setup_logging(log_level: str = "INFO"):
    """
    Set up logging with JSON formatting and Loki integration if available.
    This is the main entry point for configuring logging in the application.

    Respects the ORCHESTRA_LOG environment variable:
    - ORCHESTRA_LOG=true (default): Enable logging
    - ORCHESTRA_LOG=false: Disable all logging (set WARNING level)
    """
    # Check master switch
    if not settings.log_enabled:
        # Disable most logging when master switch is off
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.WARNING)
        return

    # Convert string log level to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Use different logging setup based on environment
    if settings.environment == "development":
        # Use Loguru for pretty console output in development
        # Configure loguru
        logger.configure(
            handlers=[
                {
                    "sink": sys.stdout,
                    "format": LOGURU_FORMAT,
                    "level": log_level,
                    "colorize": True,
                },
            ],
        )

        # Intercept standard logging and redirect to loguru
        root_logger.addHandler(InterceptHandler())

        # Intercept other common libraries
        for name in logging.root.manager.loggerDict:
            log = logging.getLogger(name)
            log.handlers = [InterceptHandler()]
            log.propagate = False

        logging.info("Configured colorized logging for development")
    else:
        json_formatter = CustomJsonFormatter(JSON_LOG_FORMAT)
        trace_filter = TraceUserContextFilter()

        logging.info("Configured JSON logging for production")

    # Configure Loki handler if available
    loki_handler = None
    if LOKI_AVAILABLE and settings.loki_url:
        # Check if Loki is actually available before trying to connect
        if is_service_available(settings.loki_url):
            try:
                # Create Loki handler
                loki_handler = logging_loki.LokiHandler(
                    url=f"{settings.loki_url}/loki/api/v1/push",
                    tags={"service": "orchestra", "environment": settings.environment},
                    auth=(
                        (settings.loki_username, settings.loki_password)
                        if settings.loki_username
                        else None
                    ),
                    version="1",
                )
                loki_handler.setLevel(numeric_level)

                if settings.environment != "development":
                    # Use JSON formatter for production
                    loki_handler.setFormatter(json_formatter)
                    loki_handler.addFilter(trace_filter)

                root_logger.addHandler(loki_handler)
                logging.info("Loki logging configured successfully")
            except Exception as e:
                logging.error(f"Failed to configure Loki logging: {e}")
        else:
            logging.warning(
                f"Loki service at {settings.loki_url} is not available - continuing without Loki logging",
            )

    # Set SQLAlchemy logging level
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if numeric_level <= logging.DEBUG else logging.WARNING,
    )

    # Set uvicorn access logs level
    logging.getLogger("uvicorn.access").setLevel(numeric_level)


# Create a global instance of StructuredLogger for import and use throughout the application
structured_logger = StructuredLogger()

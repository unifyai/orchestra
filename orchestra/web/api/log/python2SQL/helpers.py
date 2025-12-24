import base64
import copy
import functools
import io
import json
import logging
import math
import os
import re
import threading
from typing import Optional, Union

import httpx
import unify
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from openai import OpenAI
from PIL import Image
from sqlalchemy import (
    TIMESTAMP,
    BindParameter,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Interval,
    String,
    Text,
    Time,
    and_,
    case,
    cast,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
)

load_dotenv()
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import BinaryExpression, Cast, ColumnClause
from sqlalchemy.sql.selectable import CTE, Subquery

from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.models.orchestra_models import (
    DerivedLog,
    Embedding,
    Log,
    LogEventDerivedLog,
    LogEventLog,
)

from . import alias_utils

__all__ = [
    "unify_inferred_types",
    "cast_expr",
    "_build_subquery_for_identifier",
    "_join_subqueries",
    "_substring_expr",
    "_parse_rhs_list_or_dict_if_needed",
    "_get_parent_idx",
    "_flatten_target",
    "_extract_placeholders",
    "_substitute_placeholders",
    "_maybe_vector_column",
    "_ensure_vectors_exist",
    "_queue_embeddings_for_generation",
    "_get_or_generate_embedding_sync",
    "_get_embedding",
    "_get_embeddings_batch",
    "_get_image_embedding_batch",
    "_get_image_embedding_from_url",
    "_is_jsonb_expression",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_IMAGE_EMBEDDING_MODEL",
]

# Initialize OpenAI client if API key is available
try:
    OPENAI_API_KEY = os.getenv("ORCHESTRA_OPENAI_API_KEY")
    _client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    raise ValueError(f"Failed to initialize OpenAI client: {str(e)}")

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_IMAGE_EMBEDDING_MODEL = "multimodalembedding@001"
MAX_EMBEDDING_DIMS = 1536
MAX_TOKENS_PER_REQUEST = 2970000
MAX_TOKENS_PER_INPUT = 8000

# Vertex AI configuration
VERTEXAI_PROJECT = os.getenv("ORCHESTRA_VERTEXAI_PROJECT")
VERTEXAI_LOCATION = os.getenv("ORCHESTRA_VERTEXAI_LOCATION", "us-central1")

# Cache for Google Cloud credentials
_vertexai_credentials = None
_vertexai_credentials_lock = threading.Lock()


def _reset_vertexai_credentials():
    """
    Reset the Vertex AI credentials cache. Used when connection or auth errors occur.
    This forces credentials to be re-fetched on the next API call.
    Thread-safe: Uses a lock to prevent race conditions.
    """
    global _vertexai_credentials
    with _vertexai_credentials_lock:
        _vertexai_credentials = None
        logging.info("Reset Vertex AI credentials cache")


def _get_vertexai_credentials():
    """
    Get Google Cloud credentials for Vertex AI API calls.
    Credentials are cached and refreshed when needed.

    Thread-safe: Uses a lock to prevent race conditions during initialization.

    Returns:
        Credentials object with an access token
    """
    global _vertexai_credentials

    # Double-checked locking pattern for thread safety
    if _vertexai_credentials is None:
        with _vertexai_credentials_lock:
            if _vertexai_credentials is None:
                try:
                    if not VERTEXAI_PROJECT:
                        raise RuntimeError(
                            "ORCHESTRA_VERTEXAI_PROJECT environment variable must be set to use image embeddings",
                        )

                    # Get credentials with proper scopes for Vertex AI
                    from google.auth import default

                    # Request the cloud-platform scope which is needed for Vertex AI
                    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
                    credentials, project = default(scopes=scopes)
                    _vertexai_credentials = credentials
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to get Google Cloud credentials for Vertex AI. "
                        f"Ensure GOOGLE_APPLICATION_CREDENTIALS is set or default credentials are available. Error: {e}",
                    )

    # Refresh token if expired
    if not _vertexai_credentials.valid:
        _vertexai_credentials.refresh(Request())

    return _vertexai_credentials


def count_tokens_per_utf_byte(document: str) -> int:
    """
    Estimates token count based on UTF-8 byte length.
    Open AI uses this rather than `tiktoken` contrary
    to what is mentioned in the docs:
    https://community.openai.com/t/max-total-embeddings-tokens-per-request/1254699/6
    """
    # Single C-level pass to UTF-8; then take length
    n_bytes = len(document.encode("utf-8"))
    return math.ceil(0.25 * n_bytes)  # 0.25 tokens per byte


@functools.lru_cache(maxsize=4096)
def _get_embedding(
    text: str,
    model: str | None = None,
    dimensions: int | None = None,
) -> list[float]:
    """
    Get embedding vector for a single text string.
    This is now a convenience wrapper around the batch-capable function.
    """
    return _get_embeddings_batch([text], model, dimensions)[0]


def _get_embeddings_batch(
    texts: list[str],
    model: str | None = None,
    dimensions: int | None = None,
) -> list[list[float]]:
    """
    Get embedding vectors for a batch of text strings using OpenAI's API.

    Token-aware batching
    --------------------
    - Estimates tokens per input with `count_tokens_per_utf_byte`.
    - Enforces `MAX_TOKENS_PER_INPUT` for each text. If any input exceeds the
      per-input limit, raises a ValueError.
    - Greedily splits the list of texts into sub-batches whose combined
      estimated tokens are <= `MAX_TOKENS_PER_REQUEST`.
    - Calls the API per sub-batch and concatenates results in original order.
    - If the API still returns a token-limit error for a sub-batch, recursively
      splits that sub-batch until it succeeds or the sub-batch size is 1.

    Notes
    -----
    - This function does not modify individual texts.
    - The order of outputs matches the order of `texts`.

    Raises
    ------
    ValueError: if the API key is missing, any input exceeds
                `MAX_TOKENS_PER_INPUT`, an API call fails for a non-token-limit
                reason, or embedding dimensions exceed `MAX_EMBEDDING_DIMS`.
    """
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY environment variable must be set to use embed()",
        )

    model = model or DEFAULT_EMBEDDING_MODEL

    if not texts:
        return []

    # 1) Estimate tokens per input and validate per-input limit
    token_estimates = [math.ceil(count_tokens_per_utf_byte(t)) for t in texts]
    too_large = [
        (i, est) for i, est in enumerate(token_estimates) if est > MAX_TOKENS_PER_INPUT
    ]
    if too_large:
        examples = ", ".join(
            [f"idx={i}, tokens={est}" for i, est in too_large[:5]],
        )
        raise ValueError(
            f"One or more inputs exceed MAX_TOKENS_PER_INPUT={MAX_TOKENS_PER_INPUT}. "
            f"Examples: {examples}",
        )

    # 2) Greedily group texts into batches under MAX_TOKENS_PER_REQUEST
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_tokens = 0
    for text, est in zip(texts, token_estimates):
        if current_batch and (current_tokens + est > MAX_TOKENS_PER_REQUEST):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(text)
        current_tokens += est
    if current_batch:
        batches.append(current_batch)

    def _embed_or_split(batch_texts: list[str]) -> list[list[float]]:
        """Try to embed the given batch; on token-limit error, split and retry."""
        kwargs = {"model": model, "input": batch_texts}
        if dimensions is not None:
            kwargs["dimensions"] = dimensions
        try:
            resp = _client.embeddings.create(**kwargs)
            resp.data.sort(key=lambda x: x.index)
            embs = [d.embedding for d in resp.data]
            if embs and len(embs[0]) > MAX_EMBEDDING_DIMS:
                raise ValueError(
                    f"Embedding dimension {len(embs[0])} exceeds {MAX_EMBEDDING_DIMS}",
                )
            return embs
        except Exception as e:
            msg = str(e).lower()
            if "max_tokens_per_request" in msg or "too many tokens" in msg:
                if len(batch_texts) == 1:
                    # Cannot split further; surface error
                    raise ValueError(f"Failed to get embeddings: {str(e)}")
                mid = len(batch_texts) // 2
                left = _embed_or_split(batch_texts[:mid])
                right = _embed_or_split(batch_texts[mid:])
                return left + right
            raise ValueError(f"Failed to get embeddings: {str(e)}")

    # 3) Embed each batch and concatenate results in-order
    out: list[list[float]] = []
    for batch in batches:
        out.extend(_embed_or_split(batch))
    return out


def _get_image_embedding_from_url(
    image_url: str,
    bucket_service=None,
    _retry_count: int = 0,
) -> list[float] | None:
    """
    Get embedding vector for a single image from a GCS URL or base64 string.

    Args:
        image_url: Either a GCS URL (https://storage.googleapis.com/...) or
                   a base64 encoded image string (with or without data URI prefix)
        bucket_service: Optional BucketService instance for fetching GCS images.
                       If not provided, a new instance will be created (not recommended for batch operations).
        _retry_count: Internal parameter for tracking retry attempts

    Returns:
        Embedding vector as a list of floats, or None if embedding fails
    """
    try:
        # Get credentials for authentication
        credentials = _get_vertexai_credentials()

        # Check if this is a GCS URL or base64 string
        if image_url and isinstance(image_url, str):
            if image_url.startswith("http://") or image_url.startswith("https://"):
                # This is a GCS URL - download the image first
                if bucket_service is None:
                    from orchestra.services.bucket_service import BucketService

                    bucket_service = BucketService()

                # Extract filename from URL (last part after /)
                filename = image_url.split("/")[-1]
                base64_image = bucket_service.get_media(filename)

                if not base64_image:
                    logging.warning(f"Failed to fetch image from GCS: {filename}")
                    return None

                # Decode the base64 image
                image_data = base64.b64decode(base64_image)
            else:
                # This is a base64 string - use directly
                b64_string = image_url

                # Remove data URI prefix if present
                if "," in b64_string and b64_string.startswith("data:"):
                    b64_string = b64_string.split(",", 1)[1]

                # Decode base64 string
                image_data = base64.b64decode(b64_string)

            # Create a PIL Image and ensure RGB format
            pil_image = Image.open(io.BytesIO(image_data)).convert("RGB")

            # Save to temporary bytes buffer and re-encode as base64
            img_byte_arr = io.BytesIO()
            pil_image.save(img_byte_arr, format="PNG")
            img_byte_arr.seek(0)
            image_base64 = base64.b64encode(img_byte_arr.read()).decode("utf-8")

            # Construct the REST API endpoint
            endpoint = (
                f"https://{VERTEXAI_LOCATION}-aiplatform.googleapis.com/v1/"
                f"projects/{VERTEXAI_PROJECT}/locations/{VERTEXAI_LOCATION}/"
                f"publishers/google/models/{DEFAULT_IMAGE_EMBEDDING_MODEL}:predict"
            )

            # Prepare the request payload
            payload = {"instances": [{"image": {"bytesBase64Encoded": image_base64}}]}

            # Make the REST API call
            headers = {
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            }

            response = httpx.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=30.0,  # 30 second timeout
            )

            # Check for errors
            response.raise_for_status()

            # Parse the response
            result = response.json()

            # Extract the image embedding from the response
            if "predictions" in result and len(result["predictions"]) > 0:
                prediction = result["predictions"][0]
                if "imageEmbedding" in prediction:
                    embedding = [float(val) for val in prediction["imageEmbedding"]]
                    return embedding

            logging.error(f"Unexpected response format from Vertex AI: {result}")
            return None

        return None

    except Exception as e:
        error_msg = str(e).lower()
        error_type = type(e).__name__

        # Check if this is a connection/auth error that can be retried
        is_retryable_error = any(
            keyword in error_msg
            for keyword in [
                "unavailable",
                "timeout",
                "connection",
                "503",
                "502",
                "401",  # Auth errors might need credential refresh
                "invalid_scope",
                "refresh",
            ]
        )

        # Retry once with fresh credentials if it's a retryable error
        if is_retryable_error and _retry_count == 0:
            logging.warning(
                f"Retryable error during image embedding ({error_type}), retrying with fresh credentials...",
            )
            _reset_vertexai_credentials()
            return _get_image_embedding_from_url(
                image_url,
                bucket_service,
                _retry_count=1,
            )

        # Log the error and return None
        logging.error(
            f"Failed to compute image embedding for {image_url[:100]}...: {error_type}: {e}",
            exc_info=True,
        )
        return None


def _get_image_embedding_batch(image_urls: list[str]) -> list[list[float]]:
    """
    Get embedding vectors for a batch of images (GCS URLs or base64 strings) using
    Vertex AI's multimodal embedding model with parallel processing.

    Args:
        image_urls: List of image URLs (GCS) or base64 encoded image strings

    Returns:
        List of embedding vectors (each a list of floats)

    Raises:
        RuntimeError: If the image embedding model cannot be loaded
        ValueError: If image decoding fails
    """
    if not image_urls:
        return []

    # Use parallel processing to compute embeddings for all images
    def compute_single_embedding(image_url):
        """Helper function to compute embedding for a single image."""
        return _get_image_embedding_from_url(image_url)

    # Format arguments for unify.map
    formatted_args = [((url,), {}) for url in image_urls]

    # Use parallel processing with threading
    embeddings = unify.map(
        compute_single_embedding,
        formatted_args,
        mode="threading",
        name="compute_image_embeddings",
    )

    return embeddings


def _extract_placeholders(equation: str) -> list:
    """
    Find placeholders like '{log0:score}' in the equation.
    """
    pattern = re.compile(r"\{([^:{}\s]+:[^:{}\s]+)\}")
    return pattern.findall(equation)


def _substitute_placeholders(equation: str, single_ref: dict) -> tuple:
    """
    E.g. equation="{log0:score} - {log1:score}", single_ref={"log0":10,"log1":20}
    => "BASE([10],score) - BASE([20],score)" if we are referencing 1 ID each time.

    If you have multiple IDs, we might do "BASE_IN([10,11],score)" etc.
    Because we want membership logic (log_event_id in [10,11]).
    """
    # Count opening and closing parentheses
    open_count = 0
    close_count = 0
    for c in equation:
        if c == "(":
            open_count += 1
        elif c == ")":
            close_count += 1

    # If we have more closing than opening parentheses, remove the extra ones from the end
    if close_count > open_count:
        equation = equation.rstrip(")")
        equation = equation + ")" * open_count

    new_expr = equation
    alias_to_key_map = {}
    placeholders = _extract_placeholders(equation)
    # Characters that can be misinterpreted by the parser as operators
    problematic_chars = {"-", "/", "+", "*", "&", "|", "^"}
    for ph in placeholders:
        var, key = ph.split(":", 1)
        alias_to_key_map[var] = key
        base_ids = single_ref[var]
        # Even if base_ids is a single int, let's store it as a list for membership
        if not isinstance(base_ids, list):
            base_ids = [base_ids]
        # Quote field names containing special characters to prevent parser misinterpretation
        # e.g., "dt/est_time" would otherwise be parsed as "dt" divided by "est_time"
        if any(char in key for char in problematic_chars):
            key_repr = json.dumps(key)  # Produces a properly escaped JSON string
        else:
            key_repr = key
        rep = f"BASE({json.dumps(base_ids)},{key_repr})"
        new_expr = new_expr.replace(f"{{{ph}}}", rep)
    return new_expr, alias_to_key_map


def _extract_field_name_from_jsonb_expr(expr) -> Optional[str]:
    """
    Extract field name from a JSONB extraction expression.

    Only returns string field names, not integer array indices.
    """
    # This is tricky with SQLAlchemy expressions.
    # e.g. col.op('->>')('field')
    # The structure is BinaryExpression(left=col,    # Check for JSONB operators (-> returns jsonb, ->> returns text)
    if isinstance(expr, BinaryExpression):
        # Check for JSONB operators (-> returns jsonb, ->> returns text)
        op_str = str(expr.operator)
        if op_str == "->" or op_str == "->>":
            if hasattr(expr.right, "value"):
                # Only return string field names, not integer indices
                val = expr.right.value
                if isinstance(val, str):
                    return val

    # Handle cast wrapper: cast(expr, Type)
    if isinstance(expr, Cast):
        return _extract_field_name_from_jsonb_expr(expr.clause)

    return None


def _is_jsonb_expression(expr) -> bool:
    """
    Detect if an expression is a JSONB operation (BinaryExpression, Cast, or column reference) vs. a Subquery.

    This function is used in comprehensions and methods to determine whether
    to wrap expressions in subqueries or operate on them directly.
    """
    from sqlalchemy.sql.elements import ColumnElement

    if isinstance(expr, Subquery):
        return False

    # If it's a Cast or BinaryExpression, it's an expression
    if isinstance(expr, (BinaryExpression, Cast)):
        return True

    # Check for ColumnElement (covers most SQLAlchemy expressions)
    if isinstance(expr, ColumnElement):
        return True

    # BindParameter is also an expression
    if isinstance(expr, BindParameter):
        return True

    return False


def _select_value(
    subq,
    session,
    is_collection=False,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
):
    """
    Helper function to select the appropriate value column from a subquery.
    This version is deterministic, unifying all possible types in a subquery.
    Now also handles JSONB expressions (data->>'field') by inferring type from cast or querying FieldType table.
    """
    from orchestra.web.api.log.utils.type_utils import get_base_storage_type

    if isinstance(subq, BindParameter):
        inferred = LogDAO.infer_type("", subq.value)
        return subq.value, (get_base_storage_type(inferred) or inferred)

    # Comment 4: Reorder reduction metric check before JSONB early-return
    if hasattr(subq, "element") and getattr(subq, "name", "") == "reduction_metric":
        return subq.element, "float"

    # Handle JSONB expressions by inferring type from structure
    if not isinstance(subq, (Subquery, ColumnClause, BindParameter)):
        # This is likely a JSONB expression (e.g., data->>'field' or cast(data->>'field', Float))
        # Import locally to avoid circular dependency
        try:
            from .jsonb_builder import _infer_expression_type

            # Comment 3: Pass project_id/context_id to improve inference
            inferred = _infer_expression_type(
                subq,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            return subq, inferred
        except ImportError:
            pass

    # Handle JSONB expressions and Casts
    if isinstance(subq, Cast):
        type_map = {
            Float: "float",
            Integer: "int",
            Boolean: "bool",
            String: "str",
            TIMESTAMP: "datetime",
            Date: "date",
            Time: "time",
            Interval: "timedelta",
            JSONB: "dict",  # Defaulting to dict, could be list
        }
        # Handle type classes vs instances
        expr_type = type(subq.type) if not isinstance(subq.type, type) else subq.type
        inferred = "str"
        # Check against values in type_map keys (which are classes)
        for sqla_type, str_type in type_map.items():
            if issubclass(expr_type, sqla_type):
                inferred = str_type
                break
        return subq, inferred

    if isinstance(subq, BinaryExpression):
        # If it's a JSONB extraction without cast (defaults to text)
        field_name = _extract_field_name_from_jsonb_expr(subq)
        if field_name:
            # Without project_id, we can't query FieldType easily.
            # But we know ->> returns text, -> returns jsonb.
            if subq.operator == "->>":
                return subq, "str"
            elif subq.operator == "->":
                # Could be list, dict, bool (if casted, but handled above)
                return subq, "dict"

    if isinstance(subq, ColumnClause):
        # TODO(yusha): this is a hack to get the type of the column (susceptible to SQL ordering non-determinism)
        # we should have a better way to do this.
        dt = session.execute(select(subq).limit(1)).first()
        dt = dt[-1]
        inferred = LogDAO.infer_type("", dt)
        return subq, (get_base_storage_type(inferred) or inferred)

    if isinstance(subq, Subquery):
        dt = None
        # Subqueries with a single 'value' column (results of functions, operations)
        if hasattr(subq.c, "value"):
            distinct_types_rows = session.execute(
                select(subq.c.inferred_type).distinct(),
            ).fetchall()
            distinct_types_raw = [
                row[0]
                for row in distinct_types_rows
                if row[0] not in (None, "NoneType")
            ]
            # Normalize nested/spec types (e.g., List[int], Dict[str, Any]) to storage family
            distinct_types = [
                (get_base_storage_type(t) or t) for t in distinct_types_raw
            ]

            if not distinct_types:
                dt = "NoneType"
            elif len(distinct_types) == 1:
                dt = distinct_types[0]
            else:
                # Multiple types - use "jsonb" for runtime type checking
                # to ensure correct truthiness evaluation
                dt = "jsonb"
            return subq.c.value, dt

        # Subqueries with multiple typed columns (from _build_subquery_for_identifier)
        elif hasattr(subq.c, "inferred_type"):
            # Prioritize the is_vector flag to ensure the correct column is selected.
            if is_vector:
                dt = "vector"
            else:
                distinct_types_rows = session.execute(
                    select(subq.c.inferred_type).distinct(),
                ).fetchall()
                distinct_types_raw = [
                    row[0]
                    for row in distinct_types_rows
                    if row[0] not in (None, "NoneType")
                ]

                if not distinct_types_raw:
                    dt = "NoneType"
                else:
                    normalized = [
                        (get_base_storage_type(t) or t) for t in distinct_types_raw
                    ]
                    if len(normalized) == 1:
                        dt = normalized[0]
                    else:
                        dt = functools.reduce(unify_inferred_types, normalized)

            # Choose column based on normalized storage family
            type_to_col_map = {
                "int": subq.c.int_value,
                "float": subq.c.float_value,
                "bool": subq.c.bool_value,
                "str": subq.c.str_value,
                "datetime": subq.c.timestamp_value,
                "time": subq.c.time_value,
                "date": subq.c.date_value,
                "timedelta": subq.c.timedelta_value,
                "list": subq.c.jsonb_value,
                "dict": subq.c.jsonb_value,
                "tuple": subq.c.jsonb_value,
                "set": subq.c.jsonb_value,
                "union": subq.c.jsonb_value,
                "Any": subq.c.jsonb_value,
                "vector": subq.c.vector_value,
                "image": subq.c.str_value,
                "audio": subq.c.str_value,
                "NoneType": subq.c.int_value,  # Fallback, value will be NULL
            }
            col = type_to_col_map.get(dt)
            # Return normalized storage family so callers can rely on base families
            return col, dt

    if not isinstance(subq, Subquery):
        inferred = LogDAO.infer_type("", subq)
        return subq, (get_base_storage_type(inferred) or inferred)

    return None, None


def unify_inferred_types(t1: str, t2: str) -> str:
    """
    Given two inferred types like "int", "float", "str", return which type has higher precedence.
    For example, unify_inferred_types('int', 'float') -> 'float'
    unify_inferred_types('bool', 'float') -> 'float'
    unify_inferred_types('int', 'str') -> 'str'

    Special handling for "Any" type: When one type is "Any" (meaning unknown/untyped),
    we return the OTHER type since we want to use the known type for casting.
    This handles cases like comparing JSONB fields with unknown types against typed literals.
    """
    # Normalize types to base storage types
    from orchestra.web.api.log.utils.type_utils import get_base_storage_type

    t1 = get_base_storage_type(t1) or t1
    t2 = get_base_storage_type(t2) or t2

    # If either side is "none", we skip it or treat it as the other side
    if t1 is None:
        return t2
    if t2 is None:
        return t1

    # Special handling for "Any": use the other type since "Any" means unknown
    # This enables proper casting when comparing untyped JSONB fields with typed literals
    if t1 == "Any" and t2 != "Any":
        return t2
    if t2 == "Any" and t1 != "Any":
        return t1
    # If both are "Any", default to "str" for safe string comparison
    if t1 == "Any" and t2 == "Any":
        return "str"

    # Always prioritize vector type if either operand is a vector
    if t1 == "vector" or t2 == "vector":
        return "vector"

    # You can customize this ordering as you please
    precedence = [
        "NoneType",
        "bool",
        "int",
        "float",
        "str",
        "enum",  # Enum is treated like str for filtering/comparison purposes
        "datetime",
        "time",
        "date",
        "timedelta",
        "list",
        "dict",
        "tuple",
        "set",
        "union",
        "image",
        "audio",
        "jsonb",
        "Any",
    ]

    # Find each type's position in the precedence list
    try:
        i1 = precedence.index(t1)
    except ValueError:
        i1 = len(precedence)

    try:
        i2 = precedence.index(t2)
    except ValueError:
        i2 = len(precedence)

    # Handle case where type is not in precedence list
    # Default to "str" for safe comparison
    max_index = max(i1, i2)
    if max_index >= len(precedence):
        return "str"

    return precedence[max_index]


def _safe_float(col):
    """Return FLOAT or SQL NULL if the JSONB literal is 'null'."""
    return cast(func.nullif(cast(col, String), "null"), Float)


def cast_expr(expr, from_type: str, to_type: str, force_to_type: bool = False):
    """
    Casts SQLAlchemy `expr` from `from_type` to the unified final type
    after comparing `from_type` and `to_type`.

    For example, if from_type='int' and to_type='float',
    the final type is 'float' => cast(expr, Float).
    If from_type='float' and to_type='int',
    we still end up casting to float so we don't lose decimal data.

    If force_to_type is True, we skip unification and force the target type.

    This function also detects if the expression is already casted to the target
    type and skips redundant re-casting to avoid patterns like:
    CAST(CAST(data ->> 'a' AS INTEGER) AS INTEGER)
    """
    if force_to_type:
        final_type = to_type
    else:
        final_type = unify_inferred_types(from_type, to_type)

    # Check if expression is already casted to the target type
    # This prevents double-casting like CAST(CAST(x AS INTEGER) AS INTEGER)
    if isinstance(expr, Cast):
        # Map SQLAlchemy types to string type names
        cast_type_map = {
            Float: "float",
            Integer: "int",
            Boolean: "bool",
            String: "str",
            Text: "str",
            TIMESTAMP: "datetime",
            DateTime: "datetime",
            Date: "date",
            Time: "time",
            Interval: "timedelta",
            JSONB: "jsonb",
        }
        # Get the current cast target type
        expr_type_class = (
            type(expr.type) if not isinstance(expr.type, type) else expr.type
        )
        current_cast_type = None
        for sqla_type, str_type in cast_type_map.items():
            try:
                if issubclass(expr_type_class, sqla_type):
                    current_cast_type = str_type
                    break
            except TypeError:
                pass

        # If already casted to the target type, skip redundant casting
        # Exception: for "str" type with quote stripping, we may still need processing
        # but only if from_type indicates it's not already a clean string
        if current_cast_type == final_type:
            # For string type, we might need quote stripping via replace()
            # but only if the source is JSONB scalar (not already str/list/dict)
            if final_type == "str" and from_type not in (
                "list",
                "dict",
                "tuple",
                "set",
                "vector",
                "jsonb",
                "str",
            ):
                # Need quote stripping, proceed with normal logic
                pass
            else:
                return expr

    if final_type == "str":
        # Strings might still have quotes, so remove them via `replace()`
        # BUT only if we think it's a scalar string that came from JSONB.
        # If it's a structured type (list/dict/jsonb), we should preserve the JSON structure.
        # If it's already a string (from_type == "str"), don't strip quotes from string literals.
        if from_type in ("list", "dict", "tuple", "set", "vector", "jsonb", "str"):
            return cast(expr, String)

        return func.replace(
            cast(expr, String),
            literal('"', type_=String),
            literal("", type_=String),
        )
    elif final_type == "jsonb":
        # Use to_jsonb for converting to JSONB, but handle None
        if expr is None or (isinstance(expr, BindParameter) and expr.value is None):
            return null()
        if from_type == "jsonb":
            return expr

        # Cast to specific SQL type before to_jsonb to avoid "could not determine polymorphic type"
        if from_type == "str":
            expr = cast(expr, String)
        elif from_type == "int":
            expr = cast(expr, Integer)
        elif from_type == "float":
            expr = cast(expr, Float)
        elif from_type == "bool":
            expr = cast(expr, Boolean)

        return func.to_jsonb(expr)
    elif final_type == "float":
        return cast(expr, Float)
    elif final_type == "int":
        return cast(expr, Integer)
    elif final_type == "bool":
        # For bool casting, use Python-like truthiness rules instead of PostgreSQL CAST
        # PostgreSQL can't cast strings like "123" to BOOLEAN directly
        # Instead, use CASE expressions that mimic Python truthiness:
        # - Numbers: non-zero is truthy
        # - Strings: non-empty is truthy
        # - None/null: falsy
        if from_type == "bool":
            # Already boolean, just return as-is
            return expr
        elif from_type in ("int", "float"):
            # Non-zero is truthy
            return case(
                (expr.is_(None), literal(False)),
                (cast(expr, Float) != 0, literal(True)),
                else_=literal(False),
            )
        elif from_type == "str":
            # Non-empty string is truthy (excluding "false", "0", "null" for JSON compatibility)
            str_expr = cast(expr, String)
            str_lower = func.lower(func.btrim(str_expr))
            return case(
                (str_expr.is_(None), literal(False)),
                (str_lower == "", literal(False)),
                (str_lower == "false", literal(False)),
                (str_lower == "null", literal(False)),
                else_=literal(True),
            )
        else:
            # For other types, check if not null and not empty
            return case(
                (expr.is_(None), literal(False)),
                else_=literal(True),
            )
    elif final_type == "datetime":
        # If the expression is already a native TIMESTAMP column, use it directly
        # This avoids unnecessary conversions for system columns like created_at/updated_at
        if hasattr(expr, "type") and isinstance(expr.type, (TIMESTAMP, DateTime)):
            return expr
        # Use custom PostgreSQL function that safely casts to TIMESTAMP WITH TIME ZONE
        # This preserves timezone information for comparisons (== and !=)
        # Note: For datetime subtraction, use the separate naive timestamp handling
        # in _arithmetic_expr which strips timezones to compare wall clock times
        cleaned_expr = func.replace(cast(expr, Text), '"', "")
        return func.safe_cast_to_timestamptz(cleaned_expr)
    elif final_type == "time":
        # Use custom PostgreSQL function that safely casts time values
        # Requires safe_cast_to_time() function to be created in the database
        cleaned_expr = func.replace(cast(expr, Text), '"', "")
        return func.safe_cast_to_time(cleaned_expr)
    elif final_type == "date":
        # Use custom PostgreSQL function that safely casts date values
        # Requires safe_cast_to_date() function to be created in the database
        cleaned_expr = func.replace(cast(expr, Text), '"', "")
        return func.safe_cast_to_date(cleaned_expr)
    elif final_type == "timedelta":
        # Use custom PostgreSQL function that safely casts interval values
        # Requires safe_cast_to_interval() function to be created in the database
        cleaned_expr = func.replace(cast(expr, Text), '"', "")
        return func.safe_cast_to_interval(cleaned_expr)
    elif final_type == "vector":
        return expr
    else:
        # If neither side is recognized or is "NoneType", just return expr uncasted
        return expr


def _build_subquery_for_identifier(
    key,
    log_event_alias,
    log_event_ids,
    alias=None,
    session=None,
    is_derived=False,
    is_vector=False,
):
    """
    Build a subselect that retrieves columns for a given log key.
    The returned subselect columns typically include:
      - id (to allow joining)
      - several casted columns (str_value, int_value, float_value, bool_value, jsonb_value)
    """

    # Sanitize the alias to ensure it's a valid SQL identifier
    if alias:
        safe_alias = re.sub(r"[^a-zA-Z0-9_]", "_", str(alias))
        if not safe_alias:
            safe_alias = "subq"
    else:
        safe_alias = None

    # Sanitize key for use in CTE names and generate unique CTE name prefix
    safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", str(key))
    if not safe_key:
        safe_key = "key"

    # Generate unique CTE names to avoid collisions when same key is used multiple times
    # (e.g., multiple date comparisons on the same key)
    # Generate unique names for both stage1 and stage2 CTEs to prevent collisions
    base_logs_stage1_cte_name = alias_utils.unique_alias(
        f"filtered_logs_by_key_{safe_key}_stage1",
    )
    base_logs_cte_name = alias_utils.unique_alias(f"filtered_logs_by_key_{safe_key}")
    derived_logs_stage1_cte_name = alias_utils.unique_alias(
        f"filtered_derived_logs_by_key_{safe_key}_stage1",
    )
    derived_logs_cte_name = alias_utils.unique_alias(
        f"filtered_derived_logs_by_key_{safe_key}",
    )

    def extract_json_text(col):
        # This uses the PostgreSQL operator ->> to extract the JSON scalar as text.
        return col.op("#>>")(literal_column("'{}'"))

    log_alias = aliased(Log, name="log_alias")
    log_event_log_alias = aliased(LogEventLog, name="log_event_log_alias")
    derived_log_alias = aliased(DerivedLog, name="derived_log_alias")
    log_event_derived_log_alias = aliased(
        LogEventDerivedLog,
        name="log_event_derived_log_alias",
    )
    # Determine filtering strategy based on log_event_ids type
    # When log_event_ids is a Subquery or CTE, use JOIN for better index usage.
    # This allows PostgreSQL to use idx_log_event_log_event_id efficiently by joining with
    # the filtered event_ids_subq first, rather than scanning all log_event_log rows and
    # filtering with IN. This is critical for performance on staging/prod with many projects.
    use_join = isinstance(log_event_ids, Subquery) or isinstance(log_event_ids, CTE)

    if log_event_ids is None:
        # TODO(yusha): figure out why empty ids were passed and remove this check once we have a better way to handle it
        log_id_condition = True
        derived_log_id_condition = True
        log_event_condition = True
    elif isinstance(log_event_ids, list):
        # For derived logs, we pass reference logs as list of ids
        log_id_condition = log_event_log_alias.log_event_id.in_(log_event_ids)
        derived_log_id_condition = log_event_derived_log_alias.log_event_id.in_(
            log_event_ids,
        )
        log_event_condition = log_event_alias.id.in_(log_event_ids)
    else:
        # log_event_ids is a Subquery or CTE - will use JOIN instead of WHERE conditions
        log_id_condition = None
        derived_log_id_condition = None
        log_event_condition = None

    # Special handling for log_id field
    if key == "log_id":
        log_id_select_cols = [
            log_event_alias.id.label("log_event_id"),
            literal(None).label("vector_value"),
            literal(None).label("jsonb_value"),
            literal(None).label("timestamp_value"),
            literal(None).label("time_value"),
            literal(None).label("date_value"),
            literal(None).label("timedelta_value"),
            literal(None).label("str_value"),
            log_event_alias.id.label("int_value"),
            literal(None).label("float_value"),
            literal(None).label("bool_value"),
            literal("int").label("inferred_type"),
        ]
        subq = select(*log_id_select_cols).select_from(log_event_alias)
        if use_join:
            subq = subq.join(log_event_ids, log_event_ids.c.id == log_event_alias.id)
        else:
            subq = subq.where(log_event_condition)
        return alias_utils.subquery_with_unique_alias(
            subq,
            prefix=safe_alias or "log_id",
        )

    # Special handling for created_at and updated_at fields from LogEvent table
    if key in ("created_at", "updated_at"):
        timestamp_select_cols = [
            log_event_alias.id.label("log_event_id"),
            literal(None).label("jsonb_value"),
            literal(None).label("vector_value"),
            case(
                (True, cast(getattr(log_event_alias, key), TIMESTAMP)),
                else_=None,
            ).label("timestamp_value"),
            literal(None).label("time_value"),
            literal(None).label("date_value"),
            literal(None).label("timedelta_value"),
            literal(None).label("str_value"),
            literal(None).label("int_value"),
            literal(None).label("float_value"),
            literal(None).label("bool_value"),
            literal("datetime").label("inferred_type"),
        ]
        subq = select(*timestamp_select_cols).select_from(log_event_alias)
        if use_join:
            subq = subq.join(log_event_ids, log_event_ids.c.id == log_event_alias.id)
        else:
            subq = subq.where(log_event_condition)
        return alias_utils.subquery_with_unique_alias(subq, prefix=safe_alias or key)

    def _build_log_select_cols(log_alias_ref, log_event_id_col, use_list_pattern=False):
        """Build select columns for log/derived_log subquery."""
        list_pattern = "List[%" if use_list_pattern else "List%"
        dict_pattern = "Dict[%" if use_list_pattern else "Dict%"
        tuple_pattern = "Tuple[%" if use_list_pattern else "Tuple%"
        set_pattern = "Set[%" if use_list_pattern else "Set%"
        union_pattern = "Union[%" if use_list_pattern else "Union%"

        return [
            log_event_id_col.label("log_event_id"),
            literal(None).label("vector_value"),
            case(
                (
                    or_(
                        log_alias_ref.inferred_type == "list",
                        log_alias_ref.inferred_type == "dict",
                        log_alias_ref.inferred_type == "tuple",
                        log_alias_ref.inferred_type == "set",
                        log_alias_ref.inferred_type == "union",
                        log_alias_ref.inferred_type == "Any",
                        log_alias_ref.inferred_type.ilike(list_pattern),
                        log_alias_ref.inferred_type.ilike(dict_pattern),
                        log_alias_ref.inferred_type.ilike(tuple_pattern),
                        log_alias_ref.inferred_type.ilike(set_pattern),
                        log_alias_ref.inferred_type.ilike(union_pattern),
                        log_alias_ref.inferred_type.like("{%"),
                    ),
                    cast(log_alias_ref.value, JSONB),
                ),
                else_=None,
            ).label("jsonb_value"),
            case(
                (
                    log_alias_ref.inferred_type == "datetime",
                    cast(log_alias_ref.value, JSONB),
                ),
                else_=None,
            ).label("timestamp_value"),
            case(
                (
                    log_alias_ref.inferred_type == "time",
                    cast(log_alias_ref.value, JSONB),
                ),
                else_=None,
            ).label("time_value"),
            case(
                (
                    log_alias_ref.inferred_type == "date",
                    cast(log_alias_ref.value, JSONB),
                ),
                else_=None,
            ).label("date_value"),
            case(
                (
                    log_alias_ref.inferred_type == "timedelta",
                    cast(log_alias_ref.value, JSONB),
                ),
                else_=None,
            ).label("timedelta_value"),
            case(
                (
                    log_alias_ref.inferred_type == "str",
                    extract_json_text(log_alias_ref.value),
                ),
                (
                    log_alias_ref.inferred_type == "image",
                    extract_json_text(log_alias_ref.value),
                ),
                (
                    log_alias_ref.inferred_type == "audio",
                    extract_json_text(log_alias_ref.value),
                ),
                else_=None,
            ).label("str_value"),
            case(
                (
                    log_alias_ref.inferred_type == "int",
                    _safe_float(log_alias_ref.value),
                ),
                else_=None,
            ).label("int_value"),
            case(
                (
                    log_alias_ref.inferred_type == "float",
                    _safe_float(log_alias_ref.value),
                ),
                else_=None,
            ).label("float_value"),
            case(
                (
                    log_alias_ref.inferred_type == "bool",
                    cast(log_alias_ref.value, Boolean),
                ),
                else_=None,
            ).label("bool_value"),
            log_alias_ref.inferred_type.label("inferred_type"),
        ]

    def _build_log_subquery_without_cte(
        log_alias_ref,
        log_event_log_alias_ref,
        log_id_col_ref,
        select_cols,
        key,
        id_condition,
    ):
        """Build log subquery without CTE (for list/None case)."""
        return (
            select(*select_cols)
            .select_from(log_alias_ref)
            .where(log_alias_ref.key == key)
            .join(
                log_event_log_alias_ref,
                log_id_col_ref == log_alias_ref.id,
            )
            .where(id_condition)
        )

    def _build_log_subquery_with_cte(
        log_alias_ref,
        log_event_log_alias_ref,
        log_id_col_ref,
        stage1_cte_name,
        stage2_cte_name,
        key,
        use_list_pattern=False,
    ):
        """
        Build log subquery using two-stage CTE approach.

        This optimization dramatically reduces the working set by filtering by key FIRST
        before joining, allowing PostgreSQL to use index efficiently (idx_log_key or
        ix_derived_log_key) and only scan logs with the matching key, rather than scanning
        all log_event_log/log_event_derived_log rows for all event_ids_subq rows and then
        filtering by key.

        Stage 1 (MATERIALIZED): Filter log by key FIRST (no joins) - forces key filter to
        happen first. This ensures PostgreSQL uses the key index efficiently before any joins.
        MATERIALIZED ensures this filter happens first, regardless of planner estimates.

        Stage 2 (not MATERIALIZED): Join filtered logs to log_event_log/log_event_derived_log
        and event_ids_subq. This ensures we only materialize logs for the relevant project/context,
        not all logs with that key across the entire database. Not MATERIALIZED to allow planner
        flexibility in join ordering.
        """
        list_pattern = "List[%" if use_list_pattern else "List%"
        dict_pattern = "Dict[%" if use_list_pattern else "Dict%"
        tuple_pattern = "Tuple[%" if use_list_pattern else "Tuple%"
        set_pattern = "Set[%" if use_list_pattern else "Set%"
        union_pattern = "Union[%" if use_list_pattern else "Union%"

        # Stage 1: Filter log/derived_log by key FIRST (no joins) - forces key filter to happen first
        # This ensures PostgreSQL uses idx_log_key/ix_derived_log_key index efficiently before any joins
        # MATERIALIZED ensures this filter happens first, regardless of planner estimates
        logs_for_key_cte = (
            select(
                log_alias_ref.id,
                log_alias_ref.value,
                log_alias_ref.inferred_type,
            )
            .select_from(log_alias_ref)
            .where(log_alias_ref.key == key)
            .cte(stage1_cte_name)
            .prefix_with("MATERIALIZED")
        )

        # Stage 2: Join filtered logs/derived_logs to log_event_log/log_event_derived_log and event_ids_subq
        # This ensures we only materialize logs/derived_logs for the relevant project/context,
        # not all logs/derived_logs with that key across the entire database
        # Not MATERIALIZED to allow planner flexibility in join ordering
        filtered_logs_by_key_cte = (
            select(
                logs_for_key_cte.c.id,
                logs_for_key_cte.c.value,
                logs_for_key_cte.c.inferred_type,
                log_event_log_alias_ref.log_event_id,
            )
            .select_from(logs_for_key_cte)
            .join(
                log_event_log_alias_ref,
                log_id_col_ref == logs_for_key_cte.c.id,
            )
            .join(
                log_event_ids,
                log_event_ids.c.id == log_event_log_alias_ref.log_event_id,
            )
            .cte(stage2_cte_name)
        )

        # Build subquery using CTE columns directly (no join back needed)
        return select(
            filtered_logs_by_key_cte.c.log_event_id.label("log_event_id"),
            literal(None).label("vector_value"),
            case(
                (
                    or_(
                        filtered_logs_by_key_cte.c.inferred_type == "list",
                        filtered_logs_by_key_cte.c.inferred_type == "dict",
                        filtered_logs_by_key_cte.c.inferred_type == "tuple",
                        filtered_logs_by_key_cte.c.inferred_type == "set",
                        filtered_logs_by_key_cte.c.inferred_type == "union",
                        filtered_logs_by_key_cte.c.inferred_type == "Any",
                        filtered_logs_by_key_cte.c.inferred_type.ilike(list_pattern),
                        filtered_logs_by_key_cte.c.inferred_type.ilike(dict_pattern),
                        filtered_logs_by_key_cte.c.inferred_type.ilike(tuple_pattern),
                        filtered_logs_by_key_cte.c.inferred_type.ilike(set_pattern),
                        filtered_logs_by_key_cte.c.inferred_type.ilike(union_pattern),
                        filtered_logs_by_key_cte.c.inferred_type.like("{%"),
                    ),
                    cast(filtered_logs_by_key_cte.c.value, JSONB),
                ),
                else_=None,
            ).label("jsonb_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "datetime",
                    cast(filtered_logs_by_key_cte.c.value, JSONB),
                ),
                else_=None,
            ).label("timestamp_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "time",
                    cast(filtered_logs_by_key_cte.c.value, JSONB),
                ),
                else_=None,
            ).label("time_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "date",
                    cast(filtered_logs_by_key_cte.c.value, JSONB),
                ),
                else_=None,
            ).label("date_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "timedelta",
                    cast(filtered_logs_by_key_cte.c.value, JSONB),
                ),
                else_=None,
            ).label("timedelta_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "str",
                    extract_json_text(filtered_logs_by_key_cte.c.value),
                ),
                (
                    filtered_logs_by_key_cte.c.inferred_type == "image",
                    extract_json_text(filtered_logs_by_key_cte.c.value),
                ),
                (
                    filtered_logs_by_key_cte.c.inferred_type == "audio",
                    extract_json_text(filtered_logs_by_key_cte.c.value),
                ),
                else_=None,
            ).label("str_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "int",
                    _safe_float(filtered_logs_by_key_cte.c.value),
                ),
                else_=None,
            ).label("int_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "float",
                    _safe_float(filtered_logs_by_key_cte.c.value),
                ),
                else_=None,
            ).label("float_value"),
            case(
                (
                    filtered_logs_by_key_cte.c.inferred_type == "bool",
                    cast(filtered_logs_by_key_cte.c.value, Boolean),
                ),
                else_=None,
            ).label("bool_value"),
            filtered_logs_by_key_cte.c.inferred_type.label("inferred_type"),
        ).select_from(filtered_logs_by_key_cte)

    # Build base logs subquery
    base_select_cols = _build_log_select_cols(
        log_alias,
        log_event_log_alias.log_event_id,
        use_list_pattern=False,
    )

    # Filter by key FIRST before joining to dramatically reduce the working set
    # This allows PostgreSQL to use idx_log_key index efficiently and only scan
    # logs with the matching key, rather than scanning all log_event_log rows
    # for all event_ids_subq rows and then filtering by key
    if use_join:
        # Stage 1: Filter log by key FIRST (no joins) - forces key filter to happen first
        # This ensures PostgreSQL uses idx_log_key index efficiently before any joins
        # MATERIALIZED ensures this filter happens first, regardless of planner estimates
        #
        # Stage 2: Join filtered logs to log_event_log and event_ids_subq
        # This ensures we only materialize logs for the relevant project/context,
        # not all logs with that key across the entire database
        # Not MATERIALIZED to allow planner flexibility in join ordering
        base_subq = _build_log_subquery_with_cte(
            log_alias,
            log_event_log_alias,
            log_event_log_alias.log_id,
            base_logs_stage1_cte_name,
            base_logs_cte_name,
            key,
            use_list_pattern=False,
        )
    else:
        # For list/None case, use original approach without CTE
        base_subq = _build_log_subquery_without_cte(
            log_alias,
            log_event_log_alias,
            log_event_log_alias.log_id,
            base_select_cols,
            key,
            log_id_condition,
        )

    # Build derived logs subquery
    derived_select_cols = _build_log_select_cols(
        derived_log_alias,
        log_event_derived_log_alias.log_event_id,
        use_list_pattern=True,
    )

    # Filter by key FIRST before joining to dramatically reduce the working set
    # This allows PostgreSQL to use ix_derived_log_key index efficiently and only scan
    # derived logs with the matching key, rather than scanning all log_event_derived_log rows
    # for all event_ids_subq rows and then filtering by key
    if use_join:
        # Stage 1: Filter derived_log by key FIRST (no joins) - forces key filter to happen first
        # This ensures PostgreSQL uses ix_derived_log_key index efficiently before any joins
        # MATERIALIZED ensures this filter happens first, regardless of planner estimates
        #
        # Stage 2: Join filtered derived logs to log_event_derived_log and event_ids_subq
        # This ensures we only materialize derived logs for the relevant project/context,
        # not all derived logs with that key across the entire database
        # Not MATERIALIZED to allow planner flexibility in join ordering
        derived_subq = _build_log_subquery_with_cte(
            derived_log_alias,
            log_event_derived_log_alias,
            log_event_derived_log_alias.derived_log_id,
            derived_logs_stage1_cte_name,
            derived_logs_cte_name,
            key,
            use_list_pattern=True,
        )
    else:
        # For list/None case, use original approach without CTE
        derived_subq = _build_log_subquery_without_cte(
            derived_log_alias,
            log_event_derived_log_alias,
            log_event_derived_log_alias.derived_log_id,
            derived_select_cols,
            key,
            derived_log_id_condition,
        )

    # Combine base and derived logs with union
    combined_subq = alias_utils.subquery_with_unique_alias(
        base_subq.union_all(derived_subq),
        prefix=safe_alias,
    )

    # Wrap the combined subquery with vector column support
    return (
        _maybe_vector_column(combined_subq, key, session)
        if is_vector
        else combined_subq
    )


def _join_subqueries(
    lhs_subq,
    rhs_subq,
    expr,
    inferred_type,
    session=None,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
):
    """
    Given two subqueries lhs_subq and rhs_subq and an expression expr that combines
    their respective columns, produce a new subquery that merges them (by log_event_id),
    with 'expr' as the 'value' column.

    This is useful for arithmetic operations and comparisons. The resulting
    subquery can be used in further operations.

    If both subqueries have a __comp_idx__ column (used in comprehensions),
    the join condition will also include matching on __comp_idx__ to prevent
    duplicate rows, and the output will preserve the __comp_idx__ column.

    Similarly, if both subqueries have a __parent_idx__ column (used in nested comprehensions),
    the join condition will also include matching on __parent_idx__ to ensure proper nesting,
    and the output will preserve the __parent_idx__ column.
    """
    # Get the value columns for both sides
    lhs_val, lhs_type = _select_value(
        lhs_subq,
        session,
        project_id=project_id,
        context_id=context_id,
    )
    rhs_val, rhs_type = _select_value(
        rhs_subq,
        session,
        project_id=project_id,
        context_id=context_id,
    )

    # Check if both sides have __comp_idx__ (used in comprehensions)
    has_idx_lhs = hasattr(lhs_subq.c, "__comp_idx__")
    has_idx_rhs = hasattr(rhs_subq.c, "__comp_idx__")

    # Check if both sides have __parent_idx__ (used in nested comprehensions)
    has_parent_idx_lhs = hasattr(lhs_subq.c, "__parent_idx__")
    has_parent_idx_rhs = hasattr(rhs_subq.c, "__parent_idx__")

    # Build the join condition
    join_cond = lhs_subq.c.log_event_id == rhs_subq.c.log_event_id
    # 1 If both sides carry a __parent_idx__, match on that first
    if has_parent_idx_lhs and has_parent_idx_rhs:
        join_cond = and_(
            join_cond,
            lhs_subq.c.__parent_idx__ == rhs_subq.c.__parent_idx__,
        )

    # 2 Nested-loop case: one side's parent = the other side's comp
    elif has_parent_idx_lhs and has_idx_rhs:
        join_cond = and_(
            join_cond,
            rhs_subq.c.__comp_idx__ == lhs_subq.c.__parent_idx__,
        )
    elif has_parent_idx_rhs and has_idx_lhs:
        join_cond = and_(
            join_cond,
            lhs_subq.c.__comp_idx__ == rhs_subq.c.__parent_idx__,
        )

    # 3 Same-level comprehensions
    elif has_idx_lhs and has_idx_rhs:
        join_cond = and_(join_cond, lhs_subq.c.__comp_idx__ == rhs_subq.c.__comp_idx__)

    # Build the select columns
    select_cols = [
        func.coalesce(lhs_subq.c.log_event_id, rhs_subq.c.log_event_id).label(
            "log_event_id",
        ),
    ]

    if has_idx_lhs and has_idx_rhs:
        # nested case: rhs also has __parent_idx__  -> rhs is the inner loop
        if has_parent_idx_rhs and not has_parent_idx_lhs:
            select_cols.append(rhs_subq.c.__comp_idx__.label("__comp_idx__"))
        # symmetric nested case
        elif has_parent_idx_lhs and not has_parent_idx_rhs:
            select_cols.append(lhs_subq.c.__comp_idx__.label("__comp_idx__"))
        # same-level comprehension
        else:
            select_cols.append(
                func.coalesce(lhs_subq.c.__comp_idx__, rhs_subq.c.__comp_idx__).label(
                    "__comp_idx__",
                ),
            )
    elif has_idx_lhs:
        select_cols.append(lhs_subq.c.__comp_idx__.label("__comp_idx__"))
    elif has_idx_rhs:
        select_cols.append(rhs_subq.c.__comp_idx__.label("__comp_idx__"))

    # Include __parent_idx__ in the output if it exists
    if has_parent_idx_lhs and has_parent_idx_rhs:
        select_cols.append(
            func.coalesce(lhs_subq.c.__parent_idx__, rhs_subq.c.__parent_idx__).label(
                "__parent_idx__",
            ),
        )
    elif has_parent_idx_lhs:
        select_cols.append(lhs_subq.c.__parent_idx__.label("__parent_idx__"))
    elif has_parent_idx_rhs:
        select_cols.append(rhs_subq.c.__parent_idx__.label("__parent_idx__"))

    # Add the value and inferred_type columns
    select_cols.append(
        case(
            # If either side is NULL, the result is NULL
            (
                or_(
                    lhs_val.is_(None),
                    rhs_val.is_(None),
                ),
                None,
            ),
            else_=expr,
        ).label("value"),
    )
    select_cols.append(literal(inferred_type).label("inferred_type"))

    # Generate a unique alias to prevent collisions in nested queries
    alias_name = alias_utils.unique_alias("join_subq")

    j = alias_utils.subquery_with_unique_alias(
        select(*select_cols).select_from(lhs_subq).outerjoin(rhs_subq, join_cond),
        prefix=alias_name,
    )
    return j


def _substring_expr(lhs, rhs):
    """
    Build a SQLAlchemy expression that checks if `lhs` is a substring of `rhs`,
    ignoring double-quotes in their JSON string forms.
    """
    lhs_str = func.replace(cast(lhs, String), '"', "")
    rhs_str = func.replace(cast(rhs, String), '"', "")
    return rhs_str.like("%" + lhs_str + "%")


def _cast_to_final_type(expr, final_type):
    """
    Cast a SQLAlchemy expression to a specific type.
    """
    from sqlalchemy import (
        TIMESTAMP,
        Boolean,
        Date,
        Float,
        Integer,
        Interval,
        String,
        Time,
    )
    from sqlalchemy.dialects.postgresql import JSONB

    if final_type == "str":
        return func.replace(cast(expr, String), '"', "")
    elif final_type == "int":
        return cast(expr, Integer)
    elif final_type == "float":
        return cast(expr, Float)
    elif final_type == "bool":
        return cast(expr, Boolean)
    elif final_type == "datetime":
        return cast(expr, TIMESTAMP)
    elif final_type == "date":
        return cast(expr, Date)
    elif final_type == "time":
        return cast(expr, Time)
    elif final_type == "timedelta":
        return cast(expr, Interval)
    elif final_type == "jsonb":
        return cast(expr, JSONB)

    return expr


def _get_field_type_from_db(
    key: str,
    session,
    project_id: int,
    context_id: Optional[int] = None,
) -> Optional[str]:
    """
    Query FieldType table for type information.
    """
    from orchestra.db.models.orchestra_models import FieldType

    if project_id is None:
        return None

    stmt = select(FieldType.field_type).where(
        FieldType.project_id == project_id,
        FieldType.field_name == key,
    )
    if context_id is not None:
        stmt = stmt.where(FieldType.context_id == context_id)
    else:
        stmt = stmt.where(FieldType.context_id.is_(None))

    return session.execute(stmt).scalar()


def _extract_field_name_from_jsonb_expr(expr) -> Optional[str]:
    """
    Extract field name from a JSONB extraction expression.
    """
    from sqlalchemy import Column
    from sqlalchemy.sql.functions import Function

    # This is tricky with SQLAlchemy expressions.
    # e.g. col.op('->>')('field')
    # The structure is BinaryExpression(left=col, right='field', operator='->>')
    if isinstance(expr, BinaryExpression):
        if isinstance(expr.operator, str) and expr.operator in ("->>", "->"):
            if hasattr(expr.right, "value"):
                # Only return string field names, not integer indices
                val = expr.right.value
                if isinstance(val, str):
                    return val
        # Handle custom operators that might not be strings but stringify to -> or ->>
        elif str(expr.operator) in ("->>", "->"):
            if hasattr(expr.right, "value"):
                # Only return string field names, not integer indices
                val = expr.right.value
                if isinstance(val, str):
                    return val

    # Handle cast wrapper: cast(expr, Type)
    if isinstance(expr, Cast):
        return _extract_field_name_from_jsonb_expr(expr.clause)

    if isinstance(expr, BinaryExpression):
        op_str = str(expr.operator)
        if hasattr(expr.operator, "opstring"):
            op_str = expr.operator.opstring

        if hasattr(expr.right, "value") and op_str in ("->>", "->", "#>", "#>>"):
            # Only return string field names, not integer indices
            val = expr.right.value
            if isinstance(val, str):
                return val

        # Fallback: if accessing LogEvent.data and returning non-boolean, assume extraction
        # This handles cases where operator string might be ambiguous or custom
        if isinstance(expr.left, Column) and expr.left.name == "data":
            # Exclude boolean comparisons (like @>, ?, etc. which return Boolean)
            # Extractions usually return JSONB, String, Text
            from sqlalchemy import Boolean

            if not isinstance(expr.type, Boolean):
                if hasattr(expr.right, "value"):
                    # Only return string field names, not integer indices
                    val = expr.right.value
                    if isinstance(val, str):
                        return val

    # Handle Function calls (recurse into arguments)
    if isinstance(expr, Function):
        for clause in expr.clauses:
            res = _extract_field_name_from_jsonb_expr(clause)
            if res:
                return res

    return None


def _infer_expression_type(
    expr,
    session,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
) -> str:
    """
    Infer the type of a SQLAlchemy expression.
    """
    from sqlalchemy.orm.attributes import InstrumentedAttribute
    from sqlalchemy.sql.functions import Function
    from sqlalchemy.sql.schema import Column

    # Check for annotations (e.g. from zip function or local_scope)
    if hasattr(expr, "_annotations") and "inferred_type" in expr._annotations:
        return expr._annotations["inferred_type"]

    if hasattr(expr, "value"):  # BindParameter or similar
        return LogDAO.infer_type("", expr.value)

    # Check for ORM Column references (e.g., LogEvent.created_at)
    # These have a `type` attribute with the SQL type
    if isinstance(expr, (Column, InstrumentedAttribute)) or hasattr(expr, "type"):
        col_type = getattr(expr, "type", None)
        if col_type is not None:
            type_map = {
                TIMESTAMP: "datetime",
                Date: "date",
                Time: "time",
                Interval: "timedelta",
                Float: "float",
                Integer: "int",
                Boolean: "bool",
                String: "str",
            }
            col_type_class = (
                type(col_type) if not isinstance(col_type, type) else col_type
            )
            for sqla_type, str_type in type_map.items():
                try:
                    if issubclass(col_type_class, sqla_type):
                        return str_type
                except TypeError:
                    pass

    # Recognize labeled aggregation expressions
    if hasattr(expr, "element") and getattr(expr, "name", "") == "reduction_metric":
        return "float"

    if isinstance(expr, Cast):
        type_map = {
            Float: "float",
            Integer: "int",
            Boolean: "bool",
            String: "str",
            TIMESTAMP: "datetime",
            Date: "date",
            Time: "time",
            Interval: "timedelta",
            JSONB: "dict",  # Defaulting to dict, could be list
        }
        # Handle type classes vs instances
        expr_type = type(expr.type) if not isinstance(expr.type, type) else expr.type
        # Check against values in type_map keys (which are classes)
        for sqla_type, str_type in type_map.items():
            if issubclass(expr_type, sqla_type):
                return str_type
        return "str"

    # Check for JSONB-returning functions
    if isinstance(expr, Function):
        if expr.name.lower() == "to_jsonb":
            return "jsonb"
        if expr.name.lower() == "jsonb_build_object":
            return "dict"
        if expr.name.lower() in ("jsonb_build_array", "jsonb_agg"):
            return "list"
        if expr.name.lower() == "jsonb_array_length":
            return "int"

    field_name = _extract_field_name_from_jsonb_expr(expr)
    if field_name and project_id is not None:
        ft = _get_field_type_from_db(field_name, session, project_id, context_id)
        if ft:
            # Normalize to SQL-compatible type (handles Pydantic schemas, Optional[T], etc.)
            from orchestra.web.api.log.utils.type_utils import get_sql_casting_type

            normalized = get_sql_casting_type(ft)
            return normalized if normalized else ft

    # Check for JSONB operators (-> returns jsonb, ->> returns text)
    if isinstance(expr, BinaryExpression):
        # Check if operator is "->" (string) or has string representation "->"
        op_str = str(expr.operator)
        op_s = getattr(expr.operator, "opstring", "")

        if op_str == "->" or op_s == "->":
            return "jsonb"

        # Check for boolean comparison operators
        if op_str in (
            "=",
            "!=",
            "<",
            ">",
            "<=",
            ">=",
            "is",
            "is not",
            "in",
            "not in",
            "like",
            "ilike",
        ):
            return "bool"
        if op_s in (
            "=",
            "!=",
            "<",
            ">",
            "<=",
            ">=",
            "is",
            "is not",
            "in",
            "not in",
            "like",
            "ilike",
        ):
            return "bool"

        # Check for arithmetic operators that return numeric types
        # SQLAlchemy uses built-in function names like truediv, sub, mul, etc.
        arithmetic_ops = ("+", "-", "*", "/", "%", "**", "//")
        builtin_arithmetic = ("truediv", "sub", "mul", "add", "mod", "floordiv", "pow")
        op_name = getattr(expr.operator, "__name__", "")

        if (
            op_str in arithmetic_ops
            or op_s in arithmetic_ops
            or op_name in builtin_arithmetic
            or "truediv" in op_str
            or "div" in op_str.lower()
        ):
            # Check if this is datetime/timestamp subtraction which returns interval
            if op_str == "-" or op_s == "-" or op_name == "sub":
                # Check operand types - if both are datetime/timestamp, result is timedelta
                lhs_type = _infer_expression_type(
                    expr.left,
                    session,
                    project_id,
                    context_id,
                )
                rhs_type = _infer_expression_type(
                    expr.right,
                    session,
                    project_id,
                    context_id,
                )
                if lhs_type == "datetime" and rhs_type == "datetime":
                    return "timedelta"
                if lhs_type == "timedelta" and rhs_type == "timedelta":
                    return "timedelta"
            # Check for interval/timedelta operations
            if op_str == "*" or op_s == "*" or op_name == "mul":
                lhs_type = _infer_expression_type(
                    expr.left,
                    session,
                    project_id,
                    context_id,
                )
                rhs_type = _infer_expression_type(
                    expr.right,
                    session,
                    project_id,
                    context_id,
                )
                if lhs_type == "timedelta" or rhs_type == "timedelta":
                    return "timedelta"
            if (
                op_str == "/"
                or op_s == "/"
                or op_name == "truediv"
                or "truediv" in op_str
            ):
                lhs_type = _infer_expression_type(
                    expr.left,
                    session,
                    project_id,
                    context_id,
                )
                # interval / number returns interval
                if lhs_type == "timedelta":
                    return "timedelta"
                return "float"
            # Other arithmetic - check operand types
            # For now, assume float to be safe (can be refined later)
            return "float"

    # Check for Unary operators (NOT)
    from sqlalchemy.sql.expression import BooleanClauseList, UnaryExpression

    if isinstance(expr, UnaryExpression):
        if expr.operator.__name__ == "not_" or str(expr.operator) == "not":
            return "bool"

    # Check for BooleanClauseList (AND/OR)
    if isinstance(expr, BooleanClauseList):
        return "bool"

    # Check for Subquery or Alias with inferred_type column
    from sqlalchemy.sql.selectable import ScalarSelect

    target_expr = expr
    if isinstance(expr, ScalarSelect):
        # Check if the selected column has annotations
        if hasattr(expr.element, "selected_columns"):
            for col in expr.element.selected_columns:
                if hasattr(col, "_annotations") and "inferred_type" in col._annotations:
                    return col._annotations["inferred_type"]
                if hasattr(col, "inferred_type"):
                    return col.inferred_type
        target_expr = expr.element

    if hasattr(target_expr, "c") and "inferred_type" in target_expr.c:
        inferred_col = target_expr.c.inferred_type
        # print(f"DEBUG: inferred_col type: {type(inferred_col)}")
        if hasattr(inferred_col, "value"):
            return str(inferred_col.value)
        if hasattr(inferred_col, "element") and hasattr(inferred_col.element, "value"):
            return str(inferred_col.element.value)

    return "str"


def _parse_rhs_list_or_dict_if_needed(rhs_dict, rhs_val):
    """
    Parse the RHS value if it is a JSON string, list, or dictionary.

    Args:
        rhs_dict (dict): The RHS dictionary containing the value to parse.
        rhs_val: The RHS value which can be a BindParameter, list, or dict.

    Returns:
        list, dict, or None: Parsed list or dictionary if successful, otherwise None.
    """
    if not rhs_dict:
        return None

    if isinstance(rhs_val, BindParameter):
        val = rhs_val.value
    else:
        val = rhs_val

    if isinstance(val, str) and val.strip():
        try:
            parsed = json.loads(val)
            if isinstance(parsed, (list, dict)):
                return parsed
        except Exception:
            pass

    if isinstance(val, dict):
        # Unwrap type literal dicts from parser into their raw values
        if val.get("type") == "type_literal":
            return val.get("value")
        # Handle list type dicts from parser
        if val.get("type") == "list":
            return val.get("value")
        return val

    if isinstance(val, list):
        # If this is a list of type literal dicts, unwrap to their values
        if val and all(
            isinstance(it, dict) and it.get("type") == "type_literal" for it in val
        ):
            return [it.get("value") for it in val]
        return val

    return None


def _get_parent_idx(col_collection):
    """Return the column that carries the outer-loop index, or None."""
    if "__parent_idx__" in col_collection.keys():
        return col_collection.__parent_idx__
    if "__comp_idx__" in col_collection.keys():
        return col_collection.__comp_idx__
    return None


def _flatten_target(target):
    """Recursively flatten a tuple/list target into a set of identifiers."""
    if isinstance(target, dict) and target.get("type") == "identifier":
        return {target["value"]}
    elif isinstance(target, (list, tuple)):
        names = set()
        for elt in target:
            names.update(_flatten_target(elt))
        return names
    return set()


def _replace_identifier(ast_node, original, replacement):
    """Recursively replace identifiers in the AST node: replace occurrences of 'original' with 'replacement'."""
    # Get the set of original names to replace
    orig_names = _flatten_target(original)

    # If ast_node is a dict representing an identifier
    if isinstance(ast_node, dict) and ast_node.get("type") == "identifier":
        if ast_node.get("value") in orig_names:
            # Return a deep copy of the replacement
            return copy.deepcopy(replacement)
        return ast_node
    # If ast_node is a list, iterate over it
    if isinstance(ast_node, list):
        return [_replace_identifier(child, original, replacement) for child in ast_node]
    # If ast_node is a dict, recursively replace for every key
    if isinstance(ast_node, dict):
        new_node = {}
        for key, value in ast_node.items():
            new_node[key] = _replace_identifier(value, original, replacement)
        return new_node
    # For literals or other types, return as is
    return ast_node


def _maybe_vector_column(expr, key, session, model: str | None = None):
    """
    Outer-joins the given expression with embeddings table to include vector data.

    Args:
        expr: The subquery expression to join with embeddings
        key: The key to match in embeddings
        session: SQLAlchemy session
        model: Optional model name to filter vectors by specific embedding model

    Returns:
        A new subquery that includes vector data if available
    """
    # Build the join condition
    # Exclude soft-deleted embeddings to ensure they don't participate in vector searches
    join_condition = and_(
        Embedding.ref_id == expr.c.log_event_id,
        Embedding.key == literal(key),
        Embedding.is_deleted
        == False,  # noqa: E712 - SQLAlchemy requires == for SQL generation
    )

    # Add model filter if provided
    if model is not None:
        join_condition = and_(join_condition, Embedding.model == literal(model))

    # Create a subquery that joins with embeddings
    vector_subq = (
        select(
            expr.c.log_event_id.label("log_event_id"),
            case(
                (Embedding.vector != None, Embedding.vector),
                else_=None,
            ).label("vector_value"),
            expr.c.jsonb_value.label("jsonb_value"),
            expr.c.timestamp_value.label("timestamp_value"),
            expr.c.time_value.label("time_value"),
            expr.c.date_value.label("date_value"),
            expr.c.timedelta_value.label("timedelta_value"),
            expr.c.str_value.label("str_value"),
            expr.c.int_value.label("int_value"),
            expr.c.float_value.label("float_value"),
            expr.c.bool_value.label("bool_value"),
            # Use 'vector' as inferred_type if vector exists, otherwise use original type
            case(
                (Embedding.vector != None, literal("vector")),
                else_=expr.c.inferred_type,
            ).label("inferred_type"),
        )
        .select_from(expr)
        .outerjoin(
            Embedding,
            join_condition,
        )
    )

    # Add any additional columns that might be present in the original expression
    if hasattr(expr.c, "__comp_idx__"):
        vector_subq = vector_subq.add_columns(expr.c.__comp_idx__.label("__comp_idx__"))

    if hasattr(expr.c, "__parent_idx__"):
        vector_subq = vector_subq.add_columns(
            expr.c.__parent_idx__.label("__parent_idx__"),
        )

    return alias_utils.subquery_with_unique_alias(
        vector_subq,
        prefix="vector_column",
    )


def _build_subquery_for_base_call(
    list_of_ids_expr,
    key_expr,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
):
    """
    Build a subselect that retrieves columns for a given list_of_ids and a key.
    e.g. log_event_id in [101,102] AND key='score'

    EAV mode implementation.
    """
    # Evaluate the expressions if they are BindParameter or subquery
    # Typically, list_of_ids_expr might be a literal => e.g. [101,102]
    if isinstance(list_of_ids_expr, BindParameter):
        base_ids = list_of_ids_expr.value
    elif isinstance(list_of_ids_expr, list):
        base_ids = list_of_ids_expr
    else:
        # If it's a subquery or expression, we do session.execute(...)
        base_ids = session.execute(select(list_of_ids_expr)).scalar()
        if not isinstance(base_ids, list):
            base_ids = [base_ids]

    # If base_ids is a string, parse it as JSON
    if isinstance(base_ids, str):
        try:
            base_ids = json.loads(base_ids)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON format for base_ids: {base_ids}")

    # Filter the key_expr subquery to only include rows with log_event_id in base_ids
    # When is_vector is True, explicitly select the vector column
    key_val, key_type = _select_value(
        key_expr,
        session,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )
    parent_idx_col = None
    outer_base = None
    if local_scope and "__comp_idx__" in local_scope:
        parent_idx_col = local_scope["__comp_idx__"][0]
        outer_base = next(iter(local_scope["__comp_base__"].values()), None)

    select_cols = [
        key_expr.c.log_event_id.label("log_event_id"),
        key_val.label("value"),
        literal(key_type).label("inferred_type"),
    ]

    if parent_idx_col is not None:
        select_cols.insert(1, parent_idx_col.label("__parent_idx__"))

    from_clause = key_expr
    if parent_idx_col is not None and outer_base is not None:
        from_clause = key_expr.join(
            outer_base,
            outer_base.c.log_event_id == key_expr.c.log_event_id,
        )

    # Sanitize the subquery name to ensure it's a valid SQL identifier
    # Replace any non-alphanumeric characters with underscores
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", str(key_expr.name))
    if not safe_name:
        safe_name = "subq"

    filtered_subquery = alias_utils.subquery_with_unique_alias(
        select(*select_cols)
        .select_from(from_clause)
        .where(key_expr.c.log_event_id.in_(base_ids)),
        prefix=f"base_call_{safe_name}",
    )
    return filtered_subquery


def _embeddable(text: Union[str | None]) -> bool:
    """
    Check if the text is valid for embedding.
    """
    if text is None or text.strip() == "":
        return False
    return True


def _queue_embeddings_for_generation(
    session: Session,
    id_to_text: dict[int, str],
    model: Optional[str],
    dimensions: Optional[int],
    key: str,
) -> None:
    """
    Queue embeddings for background generation instead of creating them synchronously.

    This function:
    1. Checks which embeddings already exist (excluding soft-deleted ones)
    2. Queues missing embeddings in the embedding_queue table
    3. Background worker (triggered by Cloud Scheduler) processes the queue

    Args:
        session: SQLAlchemy session
        id_to_text: Dictionary mapping log_event_id to text string
        model: Embedding model to use (defaults to DEFAULT_EMBEDDING_MODEL if None)
        dimensions: Optional number of dimensions for the embedding
        key: The key identifier for these embeddings
    """
    from orchestra.db.models.orchestra_models import EmbeddingQueue

    if not id_to_text:
        return

    model_name = model or DEFAULT_EMBEDDING_MODEL

    # 1. Find which embeddings already exist (excluding soft-deleted)
    all_ids = list(id_to_text.keys())
    existing_refs = (
        session.execute(
            select(Embedding.ref_id).where(
                and_(
                    Embedding.key == key,
                    Embedding.model == model_name,
                    Embedding.ref_id.in_(all_ids),
                    Embedding.is_deleted
                    == False,  # noqa: E712 - SQLAlchemy requires == for SQL generation
                ),
            ),
        )
        .scalars()
        .all()
    )
    existing_set = set(existing_refs)

    # 2. Queue only missing embeddings
    ids_to_queue = [
        id for id in all_ids if id not in existing_set and _embeddable(id_to_text[id])
    ]

    if not ids_to_queue:
        return

    # 3. Bulk insert into queue (use ON CONFLICT DO NOTHING to handle race conditions)
    queue_entries = [
        {
            "ref_id": log_event_id,
            "key": key,
            "text": id_to_text[log_event_id],
            "model": model_name,
            "dimensions": dimensions,
            "status": "pending",
            "retry_count": 0,
        }
        for log_event_id in ids_to_queue
    ]

    stmt = insert(EmbeddingQueue).values(queue_entries)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_embedding_queue")
    session.execute(stmt)
    session.commit()

    # Log for monitoring - embedding worker runs on schedule via Cloud Scheduler
    logging.info(
        f"Queued {len(ids_to_queue)} embeddings for generation (model={model_name}). "
        f"Worker will process on next scheduled run.",
    )


def _get_or_generate_embedding_sync(
    session: Session,
    log_event_id: int,
    text: str,
    key: str,
    model: str,
    dimensions: Optional[int] = None,
) -> Optional[list]:
    """
    Get embedding from DB, or generate synchronously if missing.

    This is a fallback for when embeddings are queried before background
    processing completes. It ensures queries always return results, even
    if slightly slower.

    Args:
        session: SQLAlchemy session
        log_event_id: Log event ID
        text: Text to embed
        key: Embedding key
        model: Model name
        dimensions: Optional dimensions

    Returns:
        Embedding vector, or None if text is not embeddable
    """
    # Try to fetch from DB (excluding soft-deleted)
    embedding = (
        session.query(Embedding)
        .filter_by(
            ref_id=log_event_id,
            key=key,
            model=model,
        )
        .filter(Embedding.is_deleted == False)  # noqa: E712
        .first()
    )

    if embedding:
        return embedding.vector

    # Not found - check if it's embeddable
    if not _embeddable(text):
        return None

    # Generate synchronously (rare case - log warning)
    logging.warning(
        f"Embedding not found for log_event {log_event_id}, generating synchronously. "
        f"This indicates the background worker is behind.",
    )

    try:
        vector = _get_embedding(text, model, dimensions)

        # Insert into DB (use upsert to handle race conditions)
        stmt = insert(Embedding).values(
            ref_id=log_event_id,
            key=key,
            model=model,
            vector=vector,
            is_deleted=False,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_embedding",
            set_={
                "vector": stmt.excluded.vector,
                "is_deleted": False,
            },
        )
        session.execute(stmt)
        session.commit()

        return vector

    except Exception as e:
        logging.error(f"Failed to generate embedding synchronously: {e}")
        return None


def _ensure_vectors_exist(
    session: Session,
    id_to_text: dict[int, str],
    model: Optional[str],
    dimensions: Optional[int],
    key: str,
) -> None:
    """
    For each log_event_id/text pair, ensure a Embedding row with
    (ref_id=log_event_id,key=key,model=model or default,vector=embedding) exists.
    Bulk-insert any missing rows.

    Args:
        session: SQLAlchemy session
        id_to_text: Dictionary mapping log_event_id to text string
        model: Embedding model to use (defaults to DEFAULT_EMBEDDING_MODEL if None)
        dimensions: Optional number of dimensions for the embedding
        key: The key identifier for these embeddings
    """
    # If no texts to process, return immediately
    if not id_to_text:
        return

    # Normalize model name
    model_name = model or DEFAULT_EMBEDDING_MODEL

    # 1. Find which texts actually need embedding
    # Only count ACTIVE embeddings as existing - soft-deleted ones should be regenerated
    all_ids = list(id_to_text.keys())
    existing_refs = (
        session.execute(
            select(Embedding.ref_id).where(
                and_(
                    Embedding.key == key,
                    Embedding.model == model_name,
                    Embedding.ref_id.in_(all_ids),
                    Embedding.is_deleted
                    == False,  # noqa: E712 - SQLAlchemy requires == for SQL generation
                ),
            ),
        )
        .scalars()
        .all()
    )
    existing_set = set(existing_refs)

    ids_to_embed = [
        id for id in all_ids if id not in existing_set and _embeddable(id_to_text[id])
    ]
    if not ids_to_embed:
        return

    texts_to_embed = [id_to_text[id] for id in ids_to_embed]

    # 2. Get embeddings in batches and parallelize batches with unify.map
    # OpenAI recommends batch sizes of 2048 for their models.
    BATCH_SIZE = 2048
    text_batches = [
        texts_to_embed[i : i + BATCH_SIZE]
        for i in range(0, len(texts_to_embed), BATCH_SIZE)
    ]
    embedding_batches = unify.map(
        _get_embeddings_batch,
        text_batches,
        model=model_name,
        dimensions=dimensions,
        mode="threading",
        name="embedding_creation",
    )

    # Flatten the list of lists of embeddings
    all_embeddings = [embedding for batch in embedding_batches for embedding in batch]

    # 3. Prepare rows for upsert
    # Use INSERT ... ON CONFLICT DO UPDATE to handle both:
    # - New embeddings (insert)
    # - Soft-deleted embeddings (resurrect by setting is_deleted=False and updating vector)
    rows_to_upsert = []
    for i, log_event_id in enumerate(ids_to_embed):
        embedding_vector = all_embeddings[i]
        rows_to_upsert.append(
            {
                "ref_id": log_event_id,
                "key": key,
                "model": model_name,
                "vector": embedding_vector,
                "is_deleted": False,
            },
        )

    # 4. Bulk upsert vectors using INSERT ... ON CONFLICT DO UPDATE
    # This handles race conditions and soft-deleted rows gracefully
    if rows_to_upsert:
        try:
            stmt = insert(Embedding).values(rows_to_upsert)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_embedding",  # Unique constraint on (ref_id, model, key)
                set_={
                    "vector": stmt.excluded.vector,
                    "is_deleted": False,  # Resurrect soft-deleted embeddings
                },
            )
            session.execute(stmt)
            session.commit()
        except IntegrityError:
            session.rollback()  # Handle unexpected race condition

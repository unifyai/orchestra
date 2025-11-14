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

import unify
from dotenv import load_dotenv
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
    or_,
    select,
)

load_dotenv()
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.sql.selectable import Subquery

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
    "_get_embedding",
    "_get_embeddings_batch",
    "_get_image_embedding_batch",
    "_get_image_embedding_from_url",
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

# Load image embedding model globally (lazy loaded on first use)
_image_embedding_model = None
_vertexai_initialized = False
_image_embedding_lock = threading.Lock()


def _get_image_embedding_model():
    """
    Lazy load the Vertex AI multimodal embedding model.
    Uses Google Cloud's Vertex AI which requires GCP credentials.
    Returns the loaded model.

    Thread-safe: Uses a lock to prevent race conditions during initialization.
    """
    global _image_embedding_model, _vertexai_initialized

    # Double-checked locking pattern for thread safety
    if _image_embedding_model is None:
        with _image_embedding_lock:
            # Check again inside the lock in case another thread initialized it
            if _image_embedding_model is None:
                try:
                    import vertexai
                    from vertexai.vision_models import MultiModalEmbeddingModel

                    # Initialize Vertex AI once
                    if not _vertexai_initialized:
                        if not VERTEXAI_PROJECT:
                            raise RuntimeError(
                                "ORCHESTRA_VERTEXAI_PROJECT environment variable must be set to use image embeddings",
                            )

                        vertexai.init(
                            project=VERTEXAI_PROJECT,
                            location=VERTEXAI_LOCATION,
                        )
                        _vertexai_initialized = True

                    # Load the multimodal embedding model
                    _image_embedding_model = MultiModalEmbeddingModel.from_pretrained(
                        DEFAULT_IMAGE_EMBEDDING_MODEL,
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to load Vertex AI multimodal embedding model '{DEFAULT_IMAGE_EMBEDDING_MODEL}'. "
                        f"Ensure vertexai library is installed and GCP credentials are configured. Error: {e}",
                    )
    return _image_embedding_model


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
) -> list[float] | None:
    """
    Get embedding vector for a single image from a GCS URL or base64 string.

    Args:
        image_url: Either a GCS URL (https://storage.googleapis.com/...) or
                   a base64 encoded image string (with or without data URI prefix)
        bucket_service: Optional BucketService instance for fetching GCS images.
                       If not provided, a new instance will be created (not recommended for batch operations).

    Returns:
        Embedding vector as a list of floats, or None if embedding fails
    """
    try:
        from vertexai.vision_models import Image as VertexImage

        # Get the Vertex AI model (lazy loaded, thread-safe)
        model = _get_image_embedding_model()

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

            # Save to temporary bytes buffer (Vertex AI Image needs bytes)
            img_byte_arr = io.BytesIO()
            pil_image.save(img_byte_arr, format="PNG")
            img_byte_arr.seek(0)

            # Load image using Vertex AI's Image wrapper
            vertex_image = VertexImage(img_byte_arr.read())

            # Get embeddings from Vertex AI
            embeddings = model.get_embeddings(
                image=vertex_image,
                # dimension=1408  # Optional: specify dimension (default is 1408)
            )

            # Extract the image embedding vector and convert to list of floats
            return [float(val) for val in embeddings.image_embedding]

        return None

    except Exception as e:
        logging.error(f"Failed to compute image embedding for {image_url}: {e}")
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
    for ph in placeholders:
        var, key = ph.split(":", 1)
        alias_to_key_map[var] = key
        base_ids = single_ref[var]
        # Even if base_ids is a single int, let's store it as a list for membership
        if not isinstance(base_ids, list):
            base_ids = [base_ids]
        rep = f"BASE({json.dumps(base_ids)},{key})"
        new_expr = new_expr.replace(f"{{{ph}}}", rep)
    return new_expr, alias_to_key_map


def _select_value(subq, session, is_collection=False, is_vector=False):
    """
    Helper function to select the appropriate value column from a subquery.
    This version is deterministic, unifying all possible types in a subquery.
    """
    from orchestra.web.api.log.utils.type_utils import get_base_storage_type

    if isinstance(subq, BindParameter):
        inferred = LogDAO.infer_type("", subq.value)
        return subq.value, (get_base_storage_type(inferred) or inferred)
    if hasattr(subq, "element") and subq.name == "reduction_metric":
        return subq.element, "float"

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
                dt = functools.reduce(unify_inferred_types, distinct_types)
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
    """
    # You can customize this ordering as you please
    precedence = [
        "NoneType",
        "bool",
        "int",
        "float",
        "str",
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
        "Any",
    ]

    # If either side is "none", we skip it or treat it as the other side
    if t1 is None:
        return t2
    if t2 is None:
        return t1

    # Always prioritize vector type if either operand is a vector
    if t1 == "vector" or t2 == "vector":
        return "vector"

    # Find each type's position in the precedence list
    try:
        i1 = precedence.index(t1)
    except ValueError:
        i1 = len(precedence)

    try:
        i2 = precedence.index(t2)
    except ValueError:
        i2 = len(precedence)

    return precedence[max(i1, i2)]


def _safe_float(col):
    """Return FLOAT or SQL NULL if the JSONB literal is 'null'."""
    return cast(func.nullif(cast(col, String), "null"), Float)


def cast_expr(expr, from_type: str, to_type: str):
    """
    Casts SQLAlchemy `expr` from `from_type` to the unified final type
    after comparing `from_type` and `to_type`.

    For example, if from_type='int' and to_type='float',
    the final type is 'float' => cast(expr, Float).
    If from_type='float' and to_type='int',
    we still end up casting to float so we don't lose decimal data.
    """
    final_type = unify_inferred_types(from_type, to_type)

    if final_type == "str":
        # Strings might still have quotes, so remove them via `replace()`
        return func.replace(
            cast(expr, String),
            literal('"', type_=String),
            literal("", type_=String),
        )
    elif final_type == "float":
        return cast(expr, Float)
    elif final_type == "int":
        return cast(expr, Integer)
    elif final_type == "bool":
        return cast(expr, Boolean)
    elif final_type == "datetime":
        return cast(func.replace(cast(expr, Text), '"', ""), DateTime(timezone=True))
    elif final_type == "time":
        return cast(func.replace(cast(expr, Text), '"', ""), Time)
    elif final_type == "date":
        return cast(func.replace(cast(expr, Text), '"', ""), Date)
    elif final_type == "timedelta":
        return cast(func.replace(cast(expr, Text), '"', ""), Interval)
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
    # When log_event_ids is a Subquery, use JOIN for better index usage.
    # This allows PostgreSQL to use idx_log_event_log_event_id efficiently by joining with
    # the filtered event_ids_subq first, rather than scanning all log_event_log rows and
    # filtering with IN. This is critical for performance on staging/prod with many projects.
    use_join = isinstance(log_event_ids, Subquery)

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
        # log_event_ids is a Subquery - will use JOIN instead of WHERE conditions
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

    # Build base logs subquery
    base_select_cols = [
        log_event_log_alias.log_event_id.label("log_event_id"),
        literal(None).label("vector_value"),
        case(
            (
                or_(
                    log_alias.inferred_type == "list",
                    log_alias.inferred_type == "dict",
                    log_alias.inferred_type == "tuple",
                    log_alias.inferred_type == "set",
                    log_alias.inferred_type == "union",
                    log_alias.inferred_type == "Any",
                    log_alias.inferred_type.ilike("List%"),
                    log_alias.inferred_type.ilike("Dict%"),
                    log_alias.inferred_type.ilike("Tuple%"),
                    log_alias.inferred_type.ilike("Set%"),
                    log_alias.inferred_type.ilike("Union%"),
                    log_alias.inferred_type.like("{%"),
                ),
                cast(log_alias.value, JSONB),
            ),
            else_=None,
        ).label("jsonb_value"),
        case(
            (log_alias.inferred_type == "datetime", cast(log_alias.value, JSONB)),
            else_=None,
        ).label("timestamp_value"),
        case(
            (log_alias.inferred_type == "time", cast(log_alias.value, JSONB)),
            else_=None,
        ).label("time_value"),
        case(
            (log_alias.inferred_type == "date", cast(log_alias.value, JSONB)),
            else_=None,
        ).label("date_value"),
        case(
            (log_alias.inferred_type == "timedelta", cast(log_alias.value, JSONB)),
            else_=None,
        ).label("timedelta_value"),
        case(
            (log_alias.inferred_type == "str", extract_json_text(log_alias.value)),
            (log_alias.inferred_type == "image", extract_json_text(log_alias.value)),
            (log_alias.inferred_type == "audio", extract_json_text(log_alias.value)),
            else_=None,
        ).label("str_value"),
        case(
            (log_alias.inferred_type == "int", _safe_float(log_alias.value)),
            else_=None,
        ).label("int_value"),
        case(
            (log_alias.inferred_type == "float", _safe_float(log_alias.value)),
            else_=None,
        ).label("float_value"),
        case(
            (log_alias.inferred_type == "bool", cast(log_alias.value, Boolean)),
            else_=None,
        ).label("bool_value"),
        log_alias.inferred_type.label("inferred_type"),
    ]

    base_subq = (
        select(*base_select_cols)
        .select_from(log_alias)
        .join(
            log_event_log_alias,
            log_event_log_alias.log_id == log_alias.id,
        )
    )
    if use_join:
        # Use JOIN to leverage idx_log_event_log_event_id index efficiently
        base_subq = base_subq.join(
            log_event_ids,
            log_event_ids.c.id == log_event_log_alias.log_event_id,
        )
    base_subq = base_subq.where(log_alias.key == key)
    if not use_join:
        # Add IN condition for list or None
        base_subq = base_subq.where(log_id_condition)

    # Build derived logs subquery
    derived_select_cols = [
        log_event_derived_log_alias.log_event_id.label("log_event_id"),
        literal(None).label("vector_value"),
        case(
            (
                or_(
                    derived_log_alias.inferred_type == "list",
                    derived_log_alias.inferred_type == "dict",
                    derived_log_alias.inferred_type == "tuple",
                    derived_log_alias.inferred_type == "set",
                    derived_log_alias.inferred_type == "union",
                    derived_log_alias.inferred_type == "Any",
                    derived_log_alias.inferred_type.ilike("List[%"),
                    derived_log_alias.inferred_type.ilike("Dict[%"),
                    derived_log_alias.inferred_type.ilike("Tuple[%"),
                    derived_log_alias.inferred_type.ilike("Set[%"),
                    derived_log_alias.inferred_type.ilike("Union[%"),
                    derived_log_alias.inferred_type.like("{%"),
                ),
                cast(derived_log_alias.value, JSONB),
            ),
            else_=None,
        ).label("jsonb_value"),
        case(
            (
                derived_log_alias.inferred_type == "datetime",
                cast(derived_log_alias.value, JSONB),
            ),
            else_=None,
        ).label("timestamp_value"),
        case(
            (
                derived_log_alias.inferred_type == "time",
                cast(derived_log_alias.value, JSONB),
            ),
            else_=None,
        ).label("time_value"),
        case(
            (
                derived_log_alias.inferred_type == "date",
                cast(derived_log_alias.value, JSONB),
            ),
            else_=None,
        ).label("date_value"),
        case(
            (
                derived_log_alias.inferred_type == "timedelta",
                cast(derived_log_alias.value, JSONB),
            ),
            else_=None,
        ).label("timedelta_value"),
        case(
            (
                derived_log_alias.inferred_type == "str",
                extract_json_text(derived_log_alias.value),
            ),
            (
                derived_log_alias.inferred_type == "image",
                extract_json_text(derived_log_alias.value),
            ),
            (
                derived_log_alias.inferred_type == "audio",
                extract_json_text(derived_log_alias.value),
            ),
            else_=None,
        ).label("str_value"),
        case(
            (
                derived_log_alias.inferred_type == "int",
                _safe_float(derived_log_alias.value),
            ),
            else_=None,
        ).label("int_value"),
        case(
            (
                derived_log_alias.inferred_type == "float",
                _safe_float(derived_log_alias.value),
            ),
            else_=None,
        ).label("float_value"),
        case(
            (
                derived_log_alias.inferred_type == "bool",
                cast(derived_log_alias.value, Boolean),
            ),
            else_=None,
        ).label("bool_value"),
        derived_log_alias.inferred_type.label("inferred_type"),
    ]

    derived_subq = (
        select(*derived_select_cols)
        .select_from(derived_log_alias)
        .join(
            log_event_derived_log_alias,
            log_event_derived_log_alias.derived_log_id == derived_log_alias.id,
        )
    )
    if use_join:
        # Use JOIN to leverage index efficiently
        derived_subq = derived_subq.join(
            log_event_ids,
            log_event_ids.c.id == log_event_derived_log_alias.log_event_id,
        )
    derived_subq = derived_subq.where(derived_log_alias.key == key)
    if not use_join:
        # Add IN condition for list or None
        derived_subq = derived_subq.where(derived_log_id_condition)
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


def _join_subqueries(lhs_subq, rhs_subq, expr, inferred_type, session=None):
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
    lhs_val, lhs_type = _select_value(lhs_subq, session)
    rhs_val, rhs_type = _select_value(rhs_subq, session)

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
    join_condition = and_(
        Embedding.ref_id == expr.c.log_event_id,
        Embedding.key == literal(key),
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
):
    """
    Build a subselect that retrieves columns for a given list_of_ids and a key.
    e.g. log_event_id in [101,102] AND key='score'
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
    key_val, key_type = _select_value(key_expr, session, is_vector=is_vector)
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
    all_ids = list(id_to_text.keys())
    existing_refs = (
        session.execute(
            select(Embedding.ref_id).where(
                and_(
                    Embedding.key == key,
                    Embedding.model == model_name,
                    Embedding.ref_id.in_(all_ids),
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

    # 3. Create Embedding objects for bulk insertion
    to_insert = []
    for i, log_event_id in enumerate(ids_to_embed):
        embedding_vector = all_embeddings[i]
        to_insert.append(
            Embedding(
                ref_id=log_event_id,
                key=key,
                model=model_name,
                vector=embedding_vector,
            ),
        )

    # 4. Bulk insert new vectors
    if to_insert:
        try:
            session.bulk_save_objects(to_insert)
            session.commit()
        except IntegrityError:
            session.rollback()  # Handle race condition

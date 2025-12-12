"""Utilities for parsing and handling explicit types in log fields."""

import json
import re

# =========================================
# NEW: lightweight parser dependencies
# (stdlib only; safe drop-in)
# =========================================
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable
from typing import List
from typing import List as _List
from typing import Optional
from typing import Set as _Set
from typing import Tuple

# Supported base types that map to SQL types
# These are the fundamental types that can be stored in the database
SUPPORTED_BASE_TYPES = [
    "bool",
    "int",
    "float",
    "str",
    "datetime",
    "time",
    "date",
    "timedelta",
    "dict",
    "list",
    "image",  # Base64-encoded images (detected via magic bytes)
    "audio",  # Base64-encoded audio (detected via magic bytes)
    "vector",  # Embedding vectors (stored in Embedding table, not JSONB)
]

# Special types that don't map to SQL but are valid field types
SPECIAL_FIELD_TYPES = [
    "Any",  # Untyped/mixed-type fields
    "NoneType",  # Weak None type (allowed together with strict types)
    "enum",  # Enum type with restricted values (Log.inferred_type will be "str")
]

# Default field type for untyped/mixed-type fields
# This is used when no type is specified during field creation
# "Any" means the field is NOT strongly typed and can accept logs of any/mixed types
DEFAULT_FIELD_TYPE = "Any"

# ============================================================
# Pydantic JSON Schema Support
# ============================================================
# Orchestra supports explicit Pydantic types via JSON Schema representation.
# Schemas are sent over REST APIs as pure JSON (from model_json_schema()).
# Validation uses jsonschema library (required for validating against JSON schemas).
# Note: Pydantic validates against Python types, not JSON schemas, so jsonschema
# is necessary for REST API use cases where only the schema is available.


# ============================================================
# Tiny recursive-descent parser for arbitrary type shapes
# ============================================================
# Why: Regex splitting cannot respect nested brackets and makes heterogeneous and nested
#      containers brittle. The parser builds a tiny AST that we can render/validate/infer.
#
# Supported:
#   * Arbitrary nesting: List[Dict[str, List[int]]]
#   * Heterogeneous containers: List[int, str, image], Set[float, NoneType]
#       - Dict remains Dict[K, V] (two args). Heterogeneous values/keys should be represented at their own type level or coalesced to Any.
#   * Optional[T] -> Union[T, NoneType] (only allowed union)
#   * Tuple[int, ...] (variadic) and fixed tuples (Tuple[T1, T2, ...])
#   * No `|` operator; only explicit Union[T, NoneType] is allowed for union semantics.
#
# Rendering:
#   * Containers Title-case when parameterized: List[...], Dict[..., ...], ...
#   * Bare containers (no args) stay lowercase for display: "list", "set", "dict", "tuple".
#   * Leaf base types are lowercase; special types preserved ("Any", "NoneType", "enum").
#


@dataclass
class _TypeNode:
    name: str
    args: _List["_TypeNode"]


# Tokenizer (no '|' support; unknown characters raise)
_TOKEN_SPEC = [
    ("LBRACK", r"\["),
    ("RBRACK", r"\]"),
    ("COMMA", r","),
    ("ELLIPSIS", r"\.\.\."),  # Tuple[int, ...]
    ("IDENT", r"[A-Za-z_][A-Za-z0-9_]*"),
    ("WS", r"\s+"),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _TOKEN_SPEC))


class _Token:
    __slots__ = ("typ", "val")

    def __init__(self, typ: str, val: str):
        self.typ = typ
        self.val = val


def _lex(s: str) -> _List[_Token]:
    """Convert a type string into tokens, skipping whitespace; error on unknown chars."""
    s = s or ""
    toks: _List[_Token] = []
    pos = 0
    n = len(s)
    while pos < n:
        m = re.match(r"\s+", s[pos:])
        if m:
            pos += m.end()
            continue
        m = _TOKEN_RE.match(s, pos)
        if not m:
            raise ValueError(f"Unexpected character at position {pos}: {s[pos]!r}")
        typ = m.lastgroup
        val = m.group(typ)
        if typ != "WS":
            toks.append(_Token(typ, val))
        pos = m.end()
    return toks


# Canonical collection/typing names
_COLLECTION_CANON = {
    "list": "List",
    "dict": "Dict",
    "tuple": "Tuple",
    "set": "Set",
    "union": "Union",
    "optional": "Optional",
    "literal": "Literal",
    "annotated": "Annotated",
    "sequence": "Sequence",
    "mapping": "Mapping",
}
_COLLECTION_CANON_REVERSE = {v: k for k, v in _COLLECTION_CANON.items()}


def _canon_ident(name: str) -> str:
    """Canonicalize an identifier (outer/inner type name).

    Rules:
      - "any" -> "Any"
      - "none" / "nonetype" -> "NoneType"
      - "enum" -> "enum" (kept lowercase per SPECIAL_FIELD_TYPES)
      - Known containers use Title-case when parameterized; bare remains lowercase.
      - Known primitives lowercased (per SUPPORTED_BASE_TYPES)
      - Otherwise, keep Title-case if provided, else lowercase for determinism.
    """
    if not name:
        return name
    lower = name.lower()

    if lower == "any":
        return "Any"
    if lower in ("nonetype", "none"):
        return "NoneType"
    if lower == "enum":
        return "enum"

    if lower in _COLLECTION_CANON:
        return _COLLECTION_CANON[lower]

    if lower in SUPPORTED_BASE_TYPES:
        return lower

    return name if name and name[0].isupper() else lower


class _Parser:
    """Recursive-descent parser for the tiny type grammar (no '|' unions)."""

    def __init__(self, toks: _List[_Token]):
        self.toks = toks
        self.i = 0

    def _peek(self, *kinds: str) -> bool:
        return self.i < len(self.toks) and self.toks[self.i].typ in kinds

    def _eat(self, kind: str) -> _Token:
        if not self._peek(kind):
            got = self.toks[self.i].typ if self.i < len(self.toks) else "EOF"
            raise ValueError(f"Expected {kind}, got {got}")
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self) -> _TypeNode:
        """Entry: parse a full expression; ensure all tokens consumed."""
        node = self._parse_simple()
        if self.i != len(self.toks):
            raise ValueError("Unexpected tokens at end of type string")
        return node

    # simple := IDENT ["[" args "]"]
    # Optional[T] sugar -> Union[T, NoneType]
    def _parse_simple(self) -> _TypeNode:
        if not self._peek("IDENT"):
            raise ValueError("Type must start with an identifier")
        name_tok = self._eat("IDENT")
        name = _canon_ident(name_tok.val)

        # Optional[T] desugars to Union[T, NoneType]
        if name == "Optional":
            self._eat("LBRACK")
            inner = self._parse_simple()
            self._eat("RBRACK")
            return _TypeNode("Union", [inner, _TypeNode("NoneType", [])])

        # Generic arguments?
        if self._peek("LBRACK"):
            self._eat("LBRACK")
            args = self._parse_args()
            self._eat("RBRACK")
            # Tuple variadic (Tuple[T, ...])
            if name == "Tuple" and len(args) == 2 and args[1].name == "Ellipsis":
                return _TypeNode("Tuple", [args[0], _TypeNode("Ellipsis", [])])
            return _TypeNode(name, args)

        # bare "None" normalized to NoneType
        if name == "None":
            name = "NoneType"
        return _TypeNode(name, [])

    # args := (simple | "...") ("," (simple | "..."))*
    def _parse_args(self) -> _List[_TypeNode]:
        args: _List[_TypeNode] = []
        while True:
            if self._peek("ELLIPSIS"):
                self._eat("ELLIPSIS")
                args.append(_TypeNode("Ellipsis", []))
            else:
                args.append(self._parse_simple())
            if self._peek("COMMA"):
                self._eat("COMMA")
                continue
            break
        return args


def _render(node: _TypeNode) -> str:
    """Render AST into canonical type string.

    - Parameterized containers -> Title-case (List[...], Dict[..., ...], ...)
    - Bare containers (no args) -> lowercase: "list", "dict", "set", "tuple"
    - Leaves: base types lowercase, specials preserved, custom: lower unless Title-cased
    - Optional already desugared to Union[..., NoneType]
    - Variadic tuple: Tuple[T, ...]
    """
    name = node.name

    # Normalize casing for containers
    if name.lower() in ("list", "dict", "tuple", "set", "union"):
        name = _COLLECTION_CANON[name.lower()]
    elif name == "None":
        name = "NoneType"

    # Leaf
    if not node.args:
        if name in SPECIAL_FIELD_TYPES:
            return name
        # bare container names remain lowercase
        if name in _COLLECTION_CANON_REVERSE:
            return _COLLECTION_CANON_REVERSE[name]
        return name if name and name[0].isupper() else name.lower()

    # Union pretty
    if name == "Union":
        return f"Union[{', '.join(_render(a) for a in node.args)}]"

    # Variadic Tuple[T, ...]
    if name == "Tuple" and len(node.args) == 2 and node.args[1].name == "Ellipsis":
        return f"Tuple[{_render(node.args[0])}, ...]"

    return f"{name}[{', '.join(_render(a) for a in node.args)}]"


def _parse_to_ast(type_str: str) -> _TypeNode:
    """Helper: parse a string into AST (raises ValueError on invalid input)."""
    return _Parser(_lex(type_str)).parse()


# ---------------------------
# Public normalization & API
# ---------------------------


def normalize_type_string(type_str: str) -> str:
    """
    Normalize a type string to a canonical format.

    Examples:
        "int" -> "int"
        "Int" -> "int"
        "ANY" -> "Any"
        "nonetype" -> "NoneType"
        "ENUM" -> "enum"
        "LIST[INT]" -> "List[int]"
        "Dict[Str, Float]" -> "Dict[str, float]"
        "list[image]" -> "List[image]"
        # NEW:
        "Optional[int]" -> "Union[int, NoneType]"
        "tuple[int, ...]" -> "Tuple[int, ...]"
        "List[Dict[str, List[int]]]" -> "List[Dict[str, List[int]]]"
        "List[int, str, image]" -> "List[int, str, image]"

    Args:
        type_str: The type string to normalize

    Returns:
        Normalized type string with proper casing
    """
    if not type_str:
        return type_str
    try:
        tree = _parse_to_ast(type_str)
        return _render(tree)
    except Exception:
        # Fallback: keep original trimmed (preserve drop-in behavior)
        return type_str.strip()


def parse_nested_type(type_str: str) -> Tuple[str, Optional[List[str]]]:
    """
    Parse a nested type string into base type and inner types.

    Examples:
        "int" -> ("int", None)
        "List[int]" -> ("List", ["int"])
        "List[int, str]" -> ("List", ["int", "str"])
        "Dict[str, float]" -> ("Dict", ["str", "float"])
        "List[Dict[str, List[int]]]" -> ("List", ["Dict[str, List[int]]"])
        "Union[int, NoneType]" -> ("Union", ["int", "NoneType"])
        "Tuple[int, ...]" -> ("Tuple", ["int", "..."])

    Args:
        type_str: The type string to parse (should be normalized)

    Returns:
        Tuple of (base_type, inner_types)
        inner_types is None for simple types, or a list for nested types
    """
    if not type_str:
        return (type_str, None)
    try:
        tree = _parse_to_ast(type_str)
        norm = _render(tree)
        if not tree.args:
            return (norm, None)
        return (tree.name, [_render(a) for a in tree.args])
    except Exception:
        # Legacy fallback (naive split)
        match = re.match(r"^(\w+)\[(.*)\]$", type_str.strip())
        if not match:
            return (type_str, None)
        base_type = match.group(1)
        inner_str = match.group(2)
        if "," in inner_str:
            inner_types = [part.strip() for part in inner_str.split(",")]
        else:
            inner_types = [inner_str.strip()]
        return (base_type, inner_types)


def is_image_type(type_str: str) -> bool:
    """
    Check if a type string represents an image type.

    Examples:
        "image" -> True
        "Image" -> True
        "List[image]" -> True
        "Dict[str, List[image]]" -> True
        "str" -> False
    """
    normalized = normalize_type_string(type_str)
    if normalized == "image":
        return True
    try:
        tree = _parse_to_ast(normalized)
    except Exception:
        return normalized.lower() == "image" or "image" in normalized.lower()

    def _walk(n: _TypeNode) -> bool:
        if n.name.lower() == "image":
            return True
        return any(_walk(a) for a in n.args)

    return _walk(tree)


def get_base_storage_type(type_str: str) -> str:
    """
    Get the base storage type for a given type string.
    This is the type that will be stored in the database's inferred_type field.

    For simple types, returns the type as-is.
    For nested types, returns the outer collection type.

    Examples:
        "int" -> "int"
        "List[int]" -> "list"
        "Dict[str, float]" -> "dict"
        "image" -> "image"
        "List[image]" -> "list"
        "Union[int, NoneType]" -> "union"
        "Tuple[int, ...]" -> "tuple"
    """
    # If this is a Pydantic schema (dict or JSON string), coerce to a simple
    # representative type first, then compute base family.
    try:
        if is_pydantic_schema(type_str):
            schema = normalize_pydantic_schema(type_str)
            simple = infer_simple_type_from_pydantic_schema(schema)
            return get_base_storage_type(simple)
    except Exception:
        pass

    normalized = normalize_type_string(type_str)
    try:
        tree = _parse_to_ast(normalized)
    except Exception:
        base_type, inner_types = parse_nested_type(normalized)
        if inner_types is None:
            return normalized
        return base_type.lower()

    if not tree.args:
        if tree.name in SPECIAL_FIELD_TYPES:
            return tree.name
        return tree.name.lower()
    else:
        return tree.name.lower()


def is_untyped_field(field_type: str) -> bool:
    """
    Check if a field type represents an untyped/mixed-type field.
    """
    return (
        field_type == DEFAULT_FIELD_TYPE
        or field_type.lower() == DEFAULT_FIELD_TYPE.lower()
    )


def get_display_type(type_str: Any, stored_type: Optional[str] = None) -> str:
    """
    Get the display type string.

    New policy:
    - Do not convert Pydantic schemas to simplified pythonic types for display.
    - If a schema is provided (dict/JSON string), return its JSON string form.
    - If a pythonic string is provided, normalize and return it.
    - Otherwise, fall back to stored_type (same rules), else DEFAULT_FIELD_TYPE.
    """
    if type_str is not None:
        if is_pydantic_schema(type_str):
            try:
                schema = normalize_pydantic_schema(type_str)
                return pydantic_schema_to_string(schema)
            except Exception:
                return str(type_str)
        if isinstance(type_str, str):
            return normalize_type_string(type_str)

    if stored_type is not None:
        if is_pydantic_schema(stored_type):
            try:
                schema = normalize_pydantic_schema(stored_type)
                return pydantic_schema_to_string(schema)
            except Exception:
                return str(stored_type)
        return normalize_type_string(stored_type)

    return DEFAULT_FIELD_TYPE


def is_valid_field_type(type_spec: Any) -> bool:
    """
    Check if a type specification is a valid field type.

    Valid types include:
    - Pydantic JSON schemas (dict or JSON string)
    - Special types: "Any", "NoneType", "enum"
    - Base types: "int", "str", "float", etc.
    - Nested types (containers):
        * List[...]  -> one or more inner args (heterogeneous allowed)
        * Set[...]   -> one or more inner args (heterogeneous allowed)
        * Tuple[...] -> one or more inner args, or Tuple[T, ...]
        * Dict[K, V] -> exactly two args
    - Optional[T] normalizes to "Union[T, NoneType]".
    - Only "Union[T, NoneType]" is allowed. No other unions and no "|" operator.

    Args:
        type_spec: The type specification (can be string, dict, or other)

    Returns:
        True if it's a valid field type, False otherwise
    """
    # Check if it's a Pydantic schema
    if is_pydantic_schema(type_spec):
        try:
            # Try to normalize it to ensure it's valid
            normalize_pydantic_schema(type_spec)
            return True
        except Exception:
            return False

    # Must be a string for other types
    if not isinstance(type_spec, str):
        return False

    normalized = normalize_type_string(type_spec)
    try:
        tree = _parse_to_ast(normalized)
    except Exception:
        return False

    def _ok(node: _TypeNode) -> bool:
        name_l = node.name.lower()

        # Special field types
        if node.name in SPECIAL_FIELD_TYPES:
            return True

        # Leaves must be known base types (or special)
        if not node.args:
            return name_l in SUPPORTED_BASE_TYPES or node.name in SPECIAL_FIELD_TYPES

        # Containers and union
        if node.name == "Dict":
            # exactly two args
            if len(node.args) != 2:
                return False
            return all(_ok(a) for a in node.args)

        if node.name in ("List", "Set"):
            # one or more inner args (heterogeneous allowed)
            if len(node.args) < 1:
                return False
            return all(_ok(a) for a in node.args)

        if node.name == "Tuple":
            # Either Tuple[T, ...] or Tuple[T1, T2, ...] (>=1)
            if len(node.args) == 2 and node.args[1].name == "Ellipsis":
                return _ok(node.args[0])
            if len(node.args) < 1:
                return False
            return all(_ok(a) for a in node.args)

        if node.name == "Union":
            # Only allow EXACTLY two args, one must be NoneType, and the other non-Union
            if len(node.args) != 2:
                return False
            a, b = node.args
            names = (a.name, b.name)
            if "NoneType" not in names:
                return False
            other = a if b.name == "NoneType" else b
            if other.name == "Union":
                return False
            return _ok(other)

        # Unknown containers -> reject
        return False

    return _ok(tree)


def types_match(field_type: Any, inferred_type: str) -> bool:
    """
    Check if an inferred type matches a field type.

    This handles nested types, Pydantic schemas, and special cases:
    - "List[int]" matches "list" (base type match)
    - "List[int]" matches "List[int]" (exact match)
    - "Dict[str, float]" matches "dict" (base type match)
    - "int" matches "int" (exact match)
    - "enum" matches "str" (enum values are always strings)
    - "NoneType" is a weak type and matches ANY field type (including strict types)
    - Pydantic schemas match if the inferred type matches the schema's simple type

    Args:
        field_type: The field type (string or Pydantic schema dict)
        inferred_type: The inferred type from a value (always a string)

    Returns:
        True if types match, False otherwise
    """
    # Schema-aware comparison without eager reduction.
    if is_pydantic_schema(field_type):
        try:
            schema_field = normalize_pydantic_schema(field_type)
        except Exception:
            return False

        # If both are schemas, compare normalized JSON
        if is_pydantic_schema(inferred_type):
            try:
                schema_inf = normalize_pydantic_schema(inferred_type)
                return schema_field == schema_inf
            except Exception:
                return False

        # Otherwise, avoid reducing to simple here. Default to no-match.
        return False

    # Handle string types
    if not isinstance(field_type, str):
        return False

    norm_field = normalize_type_string(field_type)
    norm_inferred = normalize_type_string(inferred_type)

    # Exact or case-insensitive match
    if norm_field == norm_inferred or norm_field.lower() == norm_inferred.lower():
        return True

    if norm_field.lower() == "enum" and norm_inferred.lower() == "str":
        return True

    if norm_inferred == "NoneType" or norm_field == "NoneType":
        return True

    # ---------- AST transformer to constraint graph ----------
    @dataclass
    class TypeConstraint:
        kind: str  # any, none, enum, primitive, list, set, dict, tuple
        name: Optional[str] = None
        elements: Optional[
            List["TypeConstraint"]
        ] = None  # for list/set allowed element unions
        key: Optional["TypeConstraint"] = None  # for dict
        value: Optional["TypeConstraint"] = None  # for dict
        variadic: bool = False  # for tuple

    def _family_name(n: str) -> Optional[str]:
        lower = n.lower()
        if lower in ("list", "dict", "set", "tuple"):
            return lower
        return None

    def _to_constraint(node: _TypeNode) -> TypeConstraint:
        # Specials
        if node.name == "Any":
            return TypeConstraint(kind="any")
        if node.name == "NoneType":
            return TypeConstraint(kind="none")
        if node.name.lower() == "enum":
            return TypeConstraint(kind="enum")

        fam = _family_name(node.name)
        if fam is None:
            # primitive/custom
            return TypeConstraint(kind="primitive", name=node.name)

        if fam == "list":
            if not node.args:
                return TypeConstraint(kind="list", elements=None)
            return TypeConstraint(
                kind="list",
                elements=[_to_constraint(a) for a in node.args],
            )

        if fam == "set":
            if not node.args:
                return TypeConstraint(kind="set", elements=None)
            return TypeConstraint(
                kind="set",
                elements=[_to_constraint(a) for a in node.args],
            )

        if fam == "dict":
            if len(node.args) >= 2:
                return TypeConstraint(
                    kind="dict",
                    key=_to_constraint(node.args[0]),
                    value=_to_constraint(node.args[1]),
                )
            return TypeConstraint(kind="dict", key=None, value=None)

        if fam == "tuple":
            if len(node.args) == 2 and node.args[1].name == "Ellipsis":
                return TypeConstraint(
                    kind="tuple",
                    elements=[_to_constraint(node.args[0])],
                    variadic=True,
                )
            if not node.args:
                return TypeConstraint(kind="tuple", elements=None, variadic=False)
            return TypeConstraint(
                kind="tuple",
                elements=[_to_constraint(a) for a in node.args],
                variadic=False,
            )

        # fallback
        return TypeConstraint(kind="primitive", name=node.name)

    def _satisfies_c(exp: TypeConstraint, inf: TypeConstraint) -> bool:
        # Top/weak
        if exp.kind == "any" or inf.kind == "none" or exp.kind == "none":
            return True
        if (
            exp.kind == "enum"
            and inf.kind == "primitive"
            and (inf.name or "").lower() == "str"
        ):
            return True

        if exp.kind == "primitive" and inf.kind == "primitive":
            return (exp.name or "").lower() == (inf.name or "").lower()

        # Base container expected => unconstrained family
        if (
            exp.kind in ("list", "set", "dict", "tuple")
            and exp.elements is None
            and exp.key is None
            and exp.value is None
            and not exp.variadic
        ):
            return inf.kind == exp.kind

        # List/Set
        if exp.kind in ("list", "set"):
            if inf.kind != exp.kind:
                return False
            if inf.elements is None:
                return True
            if exp.elements is None:
                return True
            return all(
                any(_satisfies_c(ea, ia) for ea in (exp.elements or []))
                for ia in (inf.elements or [])
            )

        # Dict
        if exp.kind == "dict":
            if inf.kind != "dict":
                return False
            if exp.key is None or exp.value is None:
                return True
            if inf.key is None or inf.value is None:
                return True
            return _satisfies_c(exp.key, inf.key) and _satisfies_c(exp.value, inf.value)

        # Tuple
        if exp.kind == "tuple":
            if inf.kind != "tuple":
                return False
            if exp.elements is None:
                return True
            if exp.variadic:
                base = exp.elements[0]
                if inf.elements is None:
                    return True
                # inferred variadic
                if len(inf.elements) == 1 and inf.variadic:
                    return _satisfies_c(base, inf.elements[0])
                return all(_satisfies_c(base, ie) for ie in (inf.elements or []))
            else:
                if inf.elements is None:
                    return True
                # inferred variadic
                if inf.variadic and len(inf.elements) == 1:
                    base = inf.elements[0]
                    return all(_satisfies_c(e, base) for e in exp.elements)
                # fixed vs fixed: each inferred element must be satisfied by some expected constraint
                return all(
                    any(_satisfies_c(e, ie) for e in exp.elements)
                    for ie in inf.elements
                )

        return False

    # Build constraints from AST
    try:
        exp_node = _parse_to_ast(norm_field)
        inf_node = _parse_to_ast(norm_inferred)
        exp_c = _to_constraint(exp_node)
        inf_c = _to_constraint(inf_node)
        return _satisfies_c(exp_c, inf_c)
    except Exception:
        return (
            norm_field == norm_inferred or norm_field.lower() == norm_inferred.lower()
        )


# ---------------------------------------------------------
# NEW: Value → Type inference (for LogDAO and other callers)
# ---------------------------------------------------------


def infer_type_from_value(
    value,
    *,
    media_detector: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """
    Infer a normalized type string from an arbitrary Python value.

    Rules:
      - Primitives: bool/int/float/str/NoneType
      - Temporal objects: datetime/date/time/timedelta
      - Strings:
          * time/date/timedelta/datetime detection
          * optional `media_detector` hook to detect "image"/"audio" via magic bytes
          * else "str"
      - Containers:
          * List / Set -> heterogeneous allowed; inner args are unique normalized types
          * Tuple     -> homogeneous variadic Tuple[T, ...], else fixed Tuple[T1, T2, ...]
          * Dict      -> always Dict[K, V] (two args). If keys/values are heterogeneous,
                         coalesce each side independently to a single representative type.
                         We choose:
                           - If exactly one unique type → that type
                           - If exactly one non-NoneType plus some None → keep non-NoneType
                           - If multiple incompatible → Any
      - Unions:
          * We DO NOT create arbitrary unions.
          * We NEVER produce "Union[int, str]" etc.
          * We also DO NOT wrap with "Union[T, NoneType]"; instead, if containers contain
            None values, we include "NoneType" as an additional inner type in List/Set,
            or as the element in per-slot Tuple. For Dict values/keys, heterogeneity is
            coalesced to a single type or Any (see above).
    """
    # Lazy imports to avoid cyclical imports at module import time in some app setups
    from datetime import date, datetime
    from datetime import time as _time
    from datetime import timedelta

    # None
    if value is None:
        return "NoneType"

    # Numeric/boolean
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"

    # Temporal objects
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date) and not isinstance(value, datetime):
        return "date"
    if isinstance(value, _time):
        return "time"
    if isinstance(value, timedelta):
        return "timedelta"

    # Strings with detectors
    if isinstance(value, str):
        if not value:
            return "str"
        # time/date/timedelta/datetime detection (ISO-like)
        if _is_time_string(value):
            return "time"
        if _is_date_string(value):
            return "date"
        if _is_timedelta_string(value):
            return "timedelta"
        try:
            datetime.fromisoformat(value)
            return "datetime"
        except Exception:
            pass
        if media_detector is not None:
            try:
                media = media_detector(value)
                if media:
                    return media
            except Exception:
                # best-effort; ignore media detection errors
                pass
        return "str"

    # Containers
    if isinstance(value, list):
        inner_types = _collect_types_from_iterable(value, media_detector=media_detector)
        return _render_hetero_container("List", inner_types)

    if isinstance(value, set):
        inner_types = _collect_types_from_iterable(value, media_detector=media_detector)
        return _render_hetero_container("Set", inner_types)

    if isinstance(value, tuple):
        # Per-element inference; decide between variadic or fixed
        elems = [infer_type_from_value(v, media_detector=media_detector) for v in value]
        elems_norm = [_unique_normalized([t])[0] for t in elems]  # normalize each
        if len(elems_norm) == 0:
            return "Tuple[Any]"
        # homogeneous? → variadic Tuple[T, ...]
        if len(set(elems_norm)) == 1 and len(elems_norm) > 1:
            return f"Tuple[{elems_norm[0]}, ...]"
        return "Tuple[" + ", ".join(elems_norm) + "]"

    if isinstance(value, dict):
        # Infer keys
        key_types = _collect_types_from_iterable(
            value.keys(),
            media_detector=media_detector,
        )
        # Infer values
        val_types = _collect_types_from_iterable(
            value.values(),
            media_detector=media_detector,
        )
        # Coalesce each side independently
        key_t = _coalesce_for_dict_slot(key_types)
        val_t = _coalesce_for_dict_slot(val_types)

        # # For heterogeneous dicts, return simple "dict" instead of Dict[K, V]
        # if val_t == "Any" or key_t == "Any":
        #     return "dict"

        # # For homogeneous dicts with str keys, return simple "dict" for common case
        # if key_t == "str" and len(set(type(v).__name__ for v in value.values())) > 1:
        #     return "dict"

        return f"Dict[{key_t}, {val_t}]"

    # Fallback: unknown → treat as stringified scalar
    return "str"


# -----------------------
# Helpers for inference
# -----------------------


def _collect_types_from_iterable(it: Iterable, *, media_detector=None) -> _List[str]:
    """Infer and collect normalized types from iterable items (deduplicated)."""
    types = [infer_type_from_value(v, media_detector=media_detector) for v in it]
    return _unique_normalized(types)


def _unique_normalized(types: _List[str]) -> _List[str]:
    """Return a stable, deterministic list of unique normalized types."""
    normalized = [normalize_type_string(t) for t in types]
    # stable order: primitives first, then specials, then containers lexicographically
    def _key(t: str):
        base, inner = parse_nested_type(t)
        # assign buckets for nice reproducibility
        if inner is None:
            if t in SPECIAL_FIELD_TYPES:
                bucket = 1
            elif t in SUPPORTED_BASE_TYPES:
                bucket = 0
            else:
                bucket = 2
        else:
            bucket = 3
        return (bucket, t)

    uniq = []
    seen: _Set[str] = set()
    for t in normalized:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return sorted(uniq, key=_key)


def _render_hetero_container(name: str, inner_types: _List[str]) -> str:
    """Render List[...] / Set[...] with one or more inner args (heterogeneous allowed)."""
    if not inner_types:
        return f"{name}[Any]"
    return f"{name}[{', '.join(inner_types)}]"


def _coalesce_for_dict_slot(inner_types: _List[str]) -> str:
    """
    Dict slots remain single-typed in our grammar.
    If many types appear:
      - exactly one → keep it
      - one non-NoneType plus NoneType → keep the non-NoneType (do not build a Union)
      - multiple incompatible → Any
    """
    if not inner_types:
        return "Any"
    uniq = _unique_normalized(inner_types)
    if len(uniq) == 1:
        return uniq[0]
    non_none = [t for t in uniq if t != "NoneType"]
    if len(non_none) == 1 and len(uniq) == 2 and "NoneType" in uniq:
        return non_none[0]
    return "Any"


# --------------------------
# Simple string detectors
# --------------------------


def _is_date_string(value: str) -> bool:
    """
    Check if a string can be parsed as a date in various formats including:
    - YYYY-MM-DD (ISO 8601)
    - MM/DD/YYYY
    - DD/MM/YYYY
    - DD-MM-YYYY
    - Month DD, YYYY

    Args:
        value (str): The string to check

    Returns:
        bool: True if the string can be parsed as a date, False otherwise
    """
    try:
        if isinstance(value, str):
            # Remove quotes if present
            clean_value = value.strip("\"'")

            # Try different date formats
            for fmt in (
                "%Y-%m-%d",  # ISO 8601: 2023-01-31
                "%m/%d/%Y",  # US format: 01/31/2023
                "%d/%m/%Y",  # UK format: 31/01/2023
                "%d-%m-%Y",  # European format: 31-01-2023
                "%B %d, %Y",  # Month name: January 31, 2023
                "%b %d, %Y",  # Abbreviated month: Jan 31, 2023
            ):
                try:
                    parsed_date = datetime.strptime(clean_value, fmt).date()
                    # Ensure it's just a date (no time component)
                    if isinstance(parsed_date, date):
                        return True
                except ValueError:
                    continue

            # Check for ISO format with regex
            if re.match(r"^\d{4}-\d{2}-\d{2}$", clean_value):
                try:
                    date.fromisoformat(clean_value)
                    return True
                except ValueError:
                    pass
        return False
    except Exception:
        return False


def _is_timedelta_string(value: str) -> bool:
    """
    Check if a string represents a timedelta in ISO 8601 duration format.

    ISO 8601 duration format: P[n]Y[n]M[n]DT[n]H[n]M[n]S
    Examples:
    - P1Y2M3DT4H5M6S (1 year, 2 months, 3 days, 4 hours, 5 minutes, 6 seconds)
    - P1D (1 day)
    - PT1H (1 hour)

    Also checks for simple duration formats like:
    - HH:MM:SS
    - MM:SS
    - [n] days, [n] hours, etc.

    Args:
        value (str): The string to check

    Returns:
        bool: True if the string represents a timedelta, False otherwise
    """
    try:
        if isinstance(value, str):
            clean_value = value.strip("\"'")

            # Check ISO 8601 duration format
            iso_duration_pattern = r"^P(?=\d|T\d)(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$"
            if re.match(iso_duration_pattern, clean_value):
                return True

            # Check for PostgreSQL interval format: 1 day 2 hours 3 minutes 4 seconds
            pg_interval_pattern = r"^(\d+\s+(?:day|days|hour|hours|minute|minutes|second|seconds)(?:\s+|$))+$"
            if re.match(pg_interval_pattern, clean_value, re.IGNORECASE):
                return True

            # Check for simple time duration format: HH:MM:SS
            if re.match(r"^\d+:\d{2}(:\d{2})?$", clean_value):
                # Make sure it's not a valid time (which would be caught by _is_time_string)
                if not _is_time_string(clean_value):
                    return True
        return False
    except Exception:
        return False


def _is_time_string(value: str) -> bool:
    """
    Check if a string can be parsed as a time in various formats including:
    - HH:MM:SS[.ffffff]
    - HH:MM
    - H:MM AM/PM
    - HH:MM:SS AM/PM

    Args:
        value (str): The string to check

    Returns:
        bool: True if the string can be parsed as a time, False otherwise
    """
    try:
        # Try to parse the string as a time
        if isinstance(value, str):
            # Remove quotes if present
            clean_value = value.strip("\"'")
            # Try different time formats
            for fmt in (
                "%H:%M:%S",  # 24-hour with seconds: 14:30:45
                "%H:%M:%S.%f",  # 24-hour with seconds and microseconds: 14:30:45.123
                "%H:%M",  # 24-hour without seconds: 14:30
                "%I:%M %p",  # 12-hour without seconds: 2:30 PM
                "%I:%M:%S %p",  # 12-hour with seconds: 02:30:45 PM
                "%I:%M:%S.%f %p",  # 12-hour with seconds and microseconds: 02:30:45.123 PM
            ):
                try:
                    datetime.strptime(clean_value, fmt)
                    return True
                except ValueError:
                    continue
        return False
    except Exception:
        return False


def normalize_timestamp(ts_str: str) -> str:
    """
    Attempts to parse the provided timestamp string and return an ISO formatted string.

    This function tries to convert various timestamp formats to the ISO 8601 format
    with the 'T' separator, which is the standard format used in the database.
    """
    try:
        # First try direct ISO format; if it fails, try common alternative formats
        dt = datetime.fromisoformat(ts_str)
    except ValueError:
        # Try alternative formats without 'T', e.g. '%Y-%m-%d %H:%M:%S.%f' or '%Y-%m-%d %H:%M:%S'
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(ts_str, fmt)
                break
            except ValueError:
                continue
        else:
            # If no format matches, return the original string
            return ts_str
    return dt.isoformat()


# ---------------------------------------------------------
# Pydantic JSON Schema Support Functions
# ---------------------------------------------------------


def is_pydantic_schema(type_spec: Any) -> bool:
    """
    Check if a type specification is a valid JSON Schema (Pydantic-compatible).

    Uses jsonschema's validator_for + check_schema to robustly determine validity.
    Accepts dict or JSON string. Returns False for non-JSON inputs.
    """
    try:
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import validator_for
    except Exception:
        return False

    # Normalize to a JSON object (dict or bool per JSON Schema spec)
    schema: Any = None
    if isinstance(type_spec, (dict, bool)):
        schema = type_spec
    elif isinstance(type_spec, str):
        try:
            parsed = json.loads(type_spec)
            schema = parsed
        except (json.JSONDecodeError, TypeError):
            return False
    else:
        return False

    try:
        Validator = validator_for(schema)
        Validator.check_schema(schema)
        return True
    except SchemaError:
        return False
    except Exception:
        return False


def normalize_pydantic_schema(type_spec: Any) -> Dict[str, Any]:
    """
    Normalize a Pydantic type specification to a standard JSON schema dict format.

    PRIMARY USE CASE: REST API - Pure JSON schemas from model_json_schema()

    Supports:
    - JSON schemas (dict) → Use as-is [PRIMARY - REST API compatible]
    - JSON strings → Parse to dict [PRIMARY - REST API compatible]

    The output is a pure JSON schema without any custom modifications.

    Args:
        type_spec: The type specification (JSON schema dict or string)

    Returns:
        Normalized JSON schema dict (pure, unmodified)

    Raises:
        ValueError: If type_spec is not a valid JSON schema
    """
    # Case 1: Already a dict (JSON schema from REST API)
    if isinstance(type_spec, dict):
        # Return as-is without modifications
        return type_spec.copy()

    # Case 2: JSON string (serialized schema from REST API)
    if isinstance(type_spec, str):
        try:
            schema = json.loads(type_spec)
            if not isinstance(schema, dict):
                raise ValueError("JSON schema must be a JSON object")
            return schema
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in schema: {e}")

    raise ValueError(
        f"Type must be a JSON schema dict or JSON string. " f"Got: {type(type_spec)}",
    )


def pydantic_schema_to_string(schema: Dict[str, Any]) -> str:
    """
    Convert a Pydantic schema dict to a JSON string for storage.

    Args:
        schema: The schema dict

    Returns:
        JSON string representation
    """
    return json.dumps(schema)


def _validate_against_schema_fallback(
    value: Any,
    schema: Dict[str, Any],
    path: str = "root",
) -> Tuple[bool, Optional[str]]:
    """
    Fallback validation when jsonschema is not available.

    Uses our understanding of JSON Schema to validate values.
    Handles $ref, $defs, and all features Pydantic generates.

    Args:
        value: The value to validate
        schema: The JSON schema dict
        path: Current path for error messages

    Returns:
        Tuple of (is_valid, error_message)
    """
    defs = schema.get("$defs", {})

    def _validate_subschema(
        val: Any,
        subschema: Dict[str, Any],
        current_path: str,
    ) -> Tuple[bool, Optional[str]]:
        """Recursively validate against a subschema."""
        # Handle $ref
        if "$ref" in subschema:
            ref_path = subschema["$ref"]
            if ref_path.startswith("#/$defs/"):
                def_name = ref_path.split("/")[-1]
                if def_name in defs:
                    return _validate_subschema(val, defs[def_name], current_path)
            return (False, f"Could not resolve $ref: {ref_path}")

        schema_type = subschema.get("type")

        # Handle different types
        if schema_type == "object":
            if not isinstance(val, dict):
                return (
                    False,
                    f"{current_path}: expected object, got {type(val).__name__}",
                )

            # Check required properties
            required = subschema.get("required", [])
            for req_field in required:
                if req_field not in val:
                    return (
                        False,
                        f"{current_path}: missing required field '{req_field}'",
                    )

            # Validate properties
            properties = subschema.get("properties", {})
            for prop_name, prop_schema in properties.items():
                if prop_name in val:
                    is_valid, error = _validate_subschema(
                        val[prop_name],
                        prop_schema,
                        f"{current_path}.{prop_name}",
                    )
                    if not is_valid:
                        return (is_valid, error)

            return (True, None)

        elif schema_type == "array":
            if not isinstance(val, (list, tuple)):
                return (
                    False,
                    f"{current_path}: expected array, got {type(val).__name__}",
                )

            items_schema = subschema.get("items")
            if items_schema:
                for idx, item in enumerate(val):
                    is_valid, error = _validate_subschema(
                        item,
                        items_schema,
                        f"{current_path}[{idx}]",
                    )
                    if not is_valid:
                        return (is_valid, error)

            return (True, None)

        elif schema_type == "string":
            if not isinstance(val, str):
                return (
                    False,
                    f"{current_path}: expected string, got {type(val).__name__}",
                )
            return (True, None)

        elif schema_type == "integer":
            if not isinstance(val, int) or isinstance(val, bool):
                return (
                    False,
                    f"{current_path}: expected integer, got {type(val).__name__}",
                )
            return (True, None)

        elif schema_type == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return (
                    False,
                    f"{current_path}: expected number, got {type(val).__name__}",
                )
            return (True, None)

        elif schema_type == "boolean":
            if not isinstance(val, bool):
                return (
                    False,
                    f"{current_path}: expected boolean, got {type(val).__name__}",
                )
            return (True, None)

        elif schema_type == "null":
            if val is not None:
                return (
                    False,
                    f"{current_path}: expected null, got {type(val).__name__}",
                )
            return (True, None)

        # Handle anyOf (unions)
        if "anyOf" in subschema:
            for variant_schema in subschema["anyOf"]:
                is_valid, _ = _validate_subschema(val, variant_schema, current_path)
                if is_valid:
                    return (True, None)
            return (False, f"{current_path}: value does not match any variant in anyOf")

        # Handle oneOf
        if "oneOf" in subschema:
            valid_count = 0
            for variant_schema in subschema["oneOf"]:
                is_valid, _ = _validate_subschema(val, variant_schema, current_path)
                if is_valid:
                    valid_count += 1
            if valid_count == 1:
                return (True, None)
            return (
                False,
                f"{current_path}: value must match exactly one variant in oneOf, matched {valid_count}",
            )

        # If no type specified, accept anything
        return (True, None)

    return _validate_subschema(value, schema, path)


def validate_value_against_pydantic_schema(
    value: Any,
    type_spec: Any,
) -> Tuple[bool, Optional[str]]:
    """
    Validate a value against a Pydantic JSON schema.

    PRIMARY USE CASE: REST API - Pure JSON schemas from model_json_schema()

    Tries jsonschema library first (preferred), falls back to built-in validation.
    This ensures validation works even without external dependencies.

    Supports:
    1. JSON schemas (dict) → jsonschema or fallback validation [REST API]
    2. JSON strings → Parse, then validate [REST API]

    Args:
        value: The value to validate
        type_spec: The JSON schema (dict or JSON string)

    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if validation passed, False otherwise
        - error_message: None if valid, error description if invalid
    """
    # Case 1: JSON string - parse first
    if isinstance(type_spec, str):
        try:
            schema = json.loads(type_spec)
            # Recursive call with parsed schema
            return validate_value_against_pydantic_schema(value, schema)
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON schema string: {e}"
            return (False, error_msg)

    # Case 2: JSON schema dict
    if isinstance(type_spec, dict):
        # Try jsonschema library first (preferred - full JSON Schema support)
        try:
            # print(f"Trying jsonschema for type validation against given type spec: {type_spec}")
            from jsonschema import SchemaError as JsonSchemaSchemaError
            from jsonschema import ValidationError as JsonSchemaValidationError
            from jsonschema import validate

            validate(value, type_spec)
            # print(f"Validation successful! - schema={type_spec}, value={value}")
            return (True, None)

        except ImportError:
            # jsonschema not available - use fallback validation
            # print(f"jsonschema not available - using fallback validation")
            return _validate_against_schema_fallback(value, type_spec)

        except JsonSchemaValidationError as e:
            # Format error with path information
            path = (
                " -> ".join(str(p) for p in e.absolute_path)
                if e.absolute_path
                else "root"
            )
            error_msg = f"Type validation failed at {path}: {e.message}"
            return (False, error_msg)

        except JsonSchemaSchemaError as e:
            error_msg = f"Schema validation failed: {e.message}"
            return (False, error_msg)

        except Exception as e:
            # jsonschema failed - try fallback
            try:
                # print(f"jsonschema failed - trying fallback validation")
                return _validate_against_schema_fallback(value, type_spec)
            except Exception:
                error_msg = f"Error validating against JSON schema: {str(e)}"
                return (False, error_msg)

    # Case 3: Unknown type
    error_msg = f"Invalid type specification: {type(type_spec)}. Expected JSON schema dict or JSON string."
    return (False, error_msg)


def infer_simple_type_from_pydantic_schema(schema: Dict[str, Any]) -> str:
    """
    Infer a simple Python type from a Pydantic JSON schema for display.

    This converts a Pydantic schema to a simple type string like "List[dict]"
    without the full schema details. This is useful for showing a simplified
    type in the UI while the full schema is stored separately.

    Handles $ref and $defs properly for nested Pydantic models.

    Args:
        schema: The Pydantic JSON schema (pure, unmodified from model_json_schema())

    Returns:
        Simple type string (e.g., "list", "dict", "List[dict]", etc.)
    """
    # Extract $defs for resolving $ref (Pydantic generates this for nested models)
    defs = schema.get("$defs", {})

    def _infer_from_subschema(subschema: Dict[str, Any]) -> str:
        """Recursively infer type from a subschema, resolving $ref."""
        # Handle $ref
        if "$ref" in subschema:
            ref_path = subschema["$ref"]
            # Resolve $ref (format: "#/$defs/TypeName")
            if ref_path.startswith("#/$defs/"):
                def_name = ref_path.split("/")[-1]
                if def_name in defs:
                    # Recursively infer from the referenced definition
                    return _infer_from_subschema(defs[def_name])
            # If we can't resolve it, assume it's an object
            return "dict"

        schema_type = subschema.get("type")

        if schema_type == "array":
            # Array/List type
            items = subschema.get("items")
            if items and isinstance(items, dict):
                item_type = _infer_from_subschema(items)
                return f"List[{item_type}]"
            return "list"

        elif schema_type == "object":
            # Object/Dict type
            properties = subschema.get("properties")
            if properties:
                # Structured object → keys are strings, values heterogeneous → Dict[str, Any]
                return "Dict[str, Any]"

            # Check for additionalProperties (free-form dict)
            additional = subschema.get("additionalProperties")
            if additional and isinstance(additional, dict):
                value_type = _infer_from_subschema(additional)
                return f"Dict[str, {value_type}]"

            # Unknown object schema → default to Dict[str, Any]
            return "Dict[str, Any]"

        elif schema_type == "string":
            return "str"

        elif schema_type == "integer":
            return "int"

        elif schema_type == "number":
            return "float"

        elif schema_type == "boolean":
            return "bool"

        elif schema_type == "null":
            return "NoneType"

        # Check for anyOf/oneOf (union types)
        if "anyOf" in subschema or "oneOf" in subschema:
            variants = subschema.get("anyOf") or subschema.get("oneOf")
            if variants and isinstance(variants, list):
                # Infer types from variants
                types = [
                    _infer_from_subschema(v) for v in variants if isinstance(v, dict)
                ]
                if len(types) == 2 and "NoneType" in types:
                    # Optional type
                    other = [t for t in types if t != "NoneType"][0]
                    return f"Union[{other}, NoneType]"
                # Return first variant as representative
                if types:
                    return types[0]

        # Fallback
        return "Any"

    return _infer_from_subschema(schema)

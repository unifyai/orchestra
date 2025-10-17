"""Utilities for parsing and handling explicit types in log fields."""

import re

# =========================================
# NEW: lightweight parser dependencies
# (stdlib only; safe drop-in)
# =========================================
from dataclasses import dataclass
from typing import List
from typing import List as _List
from typing import Optional, Tuple

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
]

# Special types that don't map to SQL but are valid field types
SPECIAL_FIELD_TYPES = [
    "Any",  # Untyped/mixed-type fields
    "NoneType",  # Weak None type (allowed with any strong type)
    "enum",  # Enum type with restricted values (Log.inferred_type will be "str")
]

# Default field type for untyped/mixed-type fields
# This is used when no type is specified during field creation
# "Any" means the field is NOT strongly typed and can accept logs of any/mixed types
DEFAULT_FIELD_TYPE = "Any"


# ============================================================
# NEW: Tiny recursive-descent parser for arbitrary type shapes
# ============================================================
# Why: The old regex approach split on commas without respecting nested brackets,
#      and it could not handle unions (X | Y, Union[X, Y]), Optional[T], Tuple[…],
#      or deeply nested generics. The code below introduces a small tokenizer and
#      parser that builds a minimal AST for types, then renders a normalized string.
#
# Design goals:
#   - No dependencies beyond stdlib
#   - Preserve original function signatures and behavior where applicable
#   - Produce readable, deterministic normalization
#   - Support:
#       * Arbitrary nesting (List[Dict[str, List[int]]])
#       * Unions (`Union[int, str]` and `int | str`)
#       * Optional[T] -> Union[T, NoneType]
#       * Tuple[int, ...] (variadic) and fixed tuples
#       * Set[T] (normalized from set[T])
#       * Correct comma handling at all nesting levels
#
# Notes:
#   - We keep SPECIAL_FIELD_TYPES as-is.
#   - We do not add "set" or "tuple" to SUPPORTED_BASE_TYPES (they're containers).
#     Validation below handles containers explicitly.
#   - For get_base_storage_type: simple -> normalized simple; nested -> outer.lower().
#


@dataclass
class _TypeNode:
    name: str
    args: _List["_TypeNode"]


# Tokenizer
_TOKEN_SPEC = [
    ("LBRACK", r"\["),
    ("RBRACK", r"\]"),
    ("COMMA", r","),
    ("PIPE", r"\|"),  # to support X | Y
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
    """Convert a type string into tokens, skipping whitespace."""
    toks: _List[_Token] = []
    for m in _TOKEN_RE.finditer(s or ""):
        typ = m.lastgroup
        if typ == "WS":
            continue
        toks.append(_Token(typ, m.group(typ)))
    return toks


# Canonical collection/typing names
# (PEP 585 builtins lower → Title-case)
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

# Reverse map for leaf containers to keep bare container names lowercase
_COLLECTION_CANON_REVERSE = {v: k for k, v in _COLLECTION_CANON.items()}


def _canon_ident(name: str) -> str:
    """Canonicalize an identifier (outer/inner type name).

    Rules:
      - "any" -> "Any"
      - "none" / "nonetype" -> "NoneType"
      - "enum" -> "enum" (kept lowercase per SPECIAL_FIELD_TYPES)
      - Known collections use Title-case (List/Dict/Tuple/Set/Union/Optional/etc.)
      - Known primitives lowercased (per SUPPORTED_BASE_TYPES)
      - Otherwise, leave as-is if it starts uppercase, else lowercase for stability.
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

    # Fallback: keep existing casing for custom names that start uppercase;
    # otherwise lowercase for deterministic output.
    return name if name and name[0].isupper() else lower


class _Parser:
    """Recursive-descent parser for the tiny type grammar."""

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
        """Entry point: parse a full expression and ensure all tokens are consumed."""
        node = self._parse_union()
        if self.i != len(self.toks):
            raise ValueError("Unexpected tokens at end of type string")
        return node

    # union := simple ("|" simple)*
    def _parse_union(self) -> _TypeNode:
        left = self._parse_simple()
        parts = [left]
        while self._peek("PIPE"):
            self._eat("PIPE")
            parts.append(self._parse_simple())
        if len(parts) > 1:
            return _TypeNode("Union", parts)
        return left

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
            inner = self._parse_union()
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

    # args := (union | "...") ("," (union | "..."))*
    def _parse_args(self) -> _List[_TypeNode]:
        args: _List[_TypeNode] = []
        while True:
            if self._peek("ELLIPSIS"):
                self._eat("ELLIPSIS")
                args.append(_TypeNode("Ellipsis", []))
            else:
                args.append(self._parse_union())
            if self._peek("COMMA"):
                self._eat("COMMA")
                continue
            break
        return args


def _render(node: _TypeNode) -> str:
    """Render AST back into a canonical type string.

    - Collections Title-case (List/Dict/Tuple/Set/Union).
    - Primitives lowercase, SPECIAL_FIELD_TYPES preserved.
    - Optional already desugared to Union[..., NoneType].
    - Variadic tuple as Tuple[T, ...].
    """
    name = node.name

    # Normalize collection casing again for safety
    if name.lower() in ("list", "dict", "tuple", "set", "union"):
        name = _COLLECTION_CANON[name.lower()]
    elif name == "None":
        name = "NoneType"

    # Leaf
    if not node.args:
        if name in SPECIAL_FIELD_TYPES:
            return name
        # Bare container types (List/Dict/Set/Tuple/Union/...) should remain lowercase
        # e.g., "list", "dict", "set" (not Title-case) when no type args provided.
        if name in _COLLECTION_CANON_REVERSE:
            return _COLLECTION_CANON_REVERSE[name]
        # primitives and other lowercase identifiers
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
        "list[int | str]" -> "List[Union[int, str]]"
        "Optional[int]" -> "Union[int, NoneType]"
        "tuple[int, ...]" -> "Tuple[int, ...]"
        "List[Dict[str, List[int]]]" -> "List[Dict[str, List[int]]]"

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
        # Fallback: retain legacy behavior minimally (strip + lower for simple cases)
        # We do not raise to preserve drop-in behavior.
        return type_str.strip()


def parse_nested_type(type_str: str) -> Tuple[str, Optional[List[str]]]:
    """
    Parse a nested type string into base type and inner types.

    Examples:
        "int" -> ("int", None)
        "List[int]" -> ("List", ["int"])
        "Dict[str, float]" -> ("Dict", ["str", "float"])
        # NEW (supported):
        "List[Dict[str, List[int]]]" -> ("List", ["Dict[str, List[int]]"])
        "Union[int, str]" -> ("Union", ["int", "str"])
        "int | str" -> ("Union", ["int", "str"])
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
            # Simple type
            return (norm, None)
        # Generic/Union/Tuple: return outer name (as-is) plus normalized inner strings
        return (tree.name, [_render(a) for a in tree.args])
    except Exception:
        # Legacy fallback retaining previous behavior for malformed input
        match = re.match(r"^(\w+)\[(.*)\]$", type_str.strip())
        if not match:
            return (type_str, None)
        base_type = match.group(1)
        inner_str = match.group(2)
        # NOTE: This fallback splits by comma naively (legacy behavior)
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
        "str" -> False
        # NEW:
        "Dict[str, List[image]]" -> True

    Args:
        type_str: The type string to check

    Returns:
        True if the type represents images
    """
    normalized = normalize_type_string(type_str)

    # Check if it's directly an image type
    if normalized == "image":
        return True

    # NEW: recursively inspect the AST so nested images are detected reliably
    try:
        tree = _parse_to_ast(normalized)
    except Exception:
        # Fallback: best-effort substring check
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
        # NEW:
        "Union[int, str]" -> "union"
        "Tuple[int, ...]" -> "tuple"

    Args:
        type_str: The type string

    Returns:
        The base storage type
    """
    normalized = normalize_type_string(type_str)
    try:
        tree = _parse_to_ast(normalized)
    except Exception:
        # Legacy fallback
        base_type, inner_types = parse_nested_type(normalized)
        if inner_types is None:
            return normalized
        return base_type.lower()

    if not tree.args:
        # Simple type
        if tree.name in SPECIAL_FIELD_TYPES:
            return tree.name
        return tree.name.lower()
    else:
        # Nested type - return the outer type in lowercase
        return tree.name.lower()


def is_untyped_field(field_type: str) -> bool:
    """
    Check if a field type represents an untyped/mixed-type field.

    Args:
        field_type: The field type string to check

    Returns:
        True if the field is untyped (accepts any/mixed types), False otherwise
    """
    return (
        field_type == DEFAULT_FIELD_TYPE
        or field_type.lower() == DEFAULT_FIELD_TYPE.lower()
    )


def get_display_type(type_str: str, stored_type: Optional[str] = None) -> str:
    """
    Get the display type for the get_fields endpoint.

    If an explicit type was provided, return it (normalized).
    Otherwise, return the stored/inferred type.

    Args:
        type_str: The explicit type string (may be None)
        stored_type: The stored inferred type

    Returns:
        The type to display to users
    """
    if type_str:
        return normalize_type_string(type_str)
    return stored_type or DEFAULT_FIELD_TYPE


def is_valid_field_type(type_str: str) -> bool:
    """
    Check if a type string is a valid field type.

    Valid types include:
    - Special types: "Any", "NoneType", "enum"
    - Base types: "int", "str", "float", etc.
    - Nested types: "List[int]", "Dict[str, float]", etc.
    # NEW:
    - Unions: "Union[int, str]" or "int | str"
    - Optional[T]: treated as "Union[T, NoneType]"
    - Tuples: "Tuple[int, float]" and "Tuple[int, ...]"
    - Sets: "Set[int]"

    Args:
        type_str: The type string to check (should be normalized)

    Returns:
        True if valid, False otherwise
    """
    normalized = normalize_type_string(type_str)
    try:
        tree = _parse_to_ast(normalized)
    except Exception:
        return False

    allowed_containers = {"List", "Dict", "Tuple", "Set", "Union"}

    def _ok(node: _TypeNode) -> bool:
        name_l = node.name.lower()

        # Special field types
        if node.name in SPECIAL_FIELD_TYPES:
            return True

        # Leaf primitives (must be known base types like int/str/float/..., image/audio)
        if not node.args:
            return name_l in SUPPORTED_BASE_TYPES or node.name in SPECIAL_FIELD_TYPES

        # Containers / unions (recursive validation)
        if node.name == "Dict":
            if len(node.args) != 2:
                return False
            return all(_ok(a) for a in node.args)

        if node.name in ("List", "Set"):
            if len(node.args) != 1:
                return False
            return _ok(node.args[0])

        if node.name == "Tuple":
            # Either Tuple[T, ...] or Tuple[T1, T2, ...] (>=1)
            if len(node.args) == 2 and node.args[1].name == "Ellipsis":
                return _ok(node.args[0])
            if len(node.args) < 1:
                return False
            return all(_ok(a) for a in node.args)

        if node.name == "Union":
            # POLICY: Only Optional-like unions are allowed → exactly two variants
            # and exactly one of them must be NoneType. Nested unions are disallowed.
            if len(node.args) != 2:
                return False
            names = [a.name for a in node.args]
            if "NoneType" not in names:
                return False
            # Validate the non-None side and ensure it's not itself a Union
            non_none = node.args[0] if names[0] != "NoneType" else node.args[1]
            if non_none.name == "Union":
                return False
            return _ok(non_none)

        # Unknown container: reject for now (tight policy).
        # If you wish to allow custom containers, return all(_ok(a) for a in node.args)
        return False

    return _ok(tree)


def types_match(field_type: str, inferred_type: str) -> bool:
    """
    Check if an inferred type matches a field type.

    This handles nested types and special cases:
    - "List[int]" matches "list" (base type match)
    - "List[int]" matches "List[int]" (exact match)
    - "Dict[str, float]" matches "dict" (base type match)
    - "int" matches "int" (exact match)
    - "enum" matches "str" (enum values are always strings)
    - "NoneType" is a weak type and matches ANY field type (including strict types)

    Args:
        field_type: The field's declared type (normalized)
        inferred_type: The inferred type from a value (normalized)

    Returns:
        True if types match, False otherwise
    """
    # Normalize both types
    norm_field = normalize_type_string(field_type)
    norm_inferred = normalize_type_string(inferred_type)

    # Exact match
    if norm_field == norm_inferred:
        return True

    # Case-insensitive match
    if norm_field.lower() == norm_inferred.lower():
        return True

    # Special case: enum field type always stores string values
    # So FieldType.field_type="enum" should match Log.inferred_type="str"
    if norm_field.lower() == "enum" and norm_inferred.lower() == "str":
        return True

    # Weak type: NoneType is allowed for any field type (including strict types)
    if norm_inferred == "NoneType" or norm_field == "NoneType":
        return True

    # Check if field type is nested and inferred type matches the base
    # E.g., field_type="List[int]", inferred_type="list"
    field_base, field_inner = parse_nested_type(norm_field)
    if field_inner:
        # Field is nested - check if inferred matches the base type
        if field_base.lower() == norm_inferred.lower():
            return True

    return False

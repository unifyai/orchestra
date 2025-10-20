import ast
import re
import textwrap

from fastapi import HTTPException

from orchestra.db.dao.log_dao import (
    _is_date_string,
    _is_time_string,
    _is_timedelta_string,
    normalize_timestamp,
)

SIMILARITY_FUNCS = {"l2", "cosine", "ip", "l1", "hamming", "jaccard", "phash_distance"}

__all__ = ["str_filter_exp_to_dict", "str_filter_exp_to_dict_using_ast"]


def parse_nested(s, pos):
    """
    Given a string s and a starting position pos, parse_nested()
    finds the substring (with balanced parentheses/brackets/braces)
    and returns (the_substring, new_position).

    For example, if s = "x['key'][0]" and pos points to the first bracket,
    parse_nested might return ("['key']", pos_after_closing_bracket).
    """
    start_pos = pos
    stack = []
    while pos < len(s):
        c = s[pos]
        if c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack:
                raise RuntimeError(f"Unmatched closing bracket {c!r} at position {pos}")
            open_bracket = stack.pop()
            if (
                (open_bracket == "(" and c != ")")
                or (open_bracket == "[" and c != "]")
                or (open_bracket == "{" and c != "}")
            ):
                raise RuntimeError(
                    f"Mismatched brackets {open_bracket!r} and {c!r} at positions {start_pos} and {pos}",
                )
            if not stack:
                pos += 1  # Include the closing bracket
                break
        elif c in ("'", '"'):
            # Skip over string literals inside the brackets
            quote_char = c
            pos += 1
            while pos < len(s):
                if s[pos] == "\\":
                    pos += 2  # Skip escaped characters
                elif s[pos] == quote_char:
                    pos += 1
                    break
                else:
                    pos += 1
            continue
        pos += 1
    else:
        raise RuntimeError(f"Unmatched brackets starting at position {start_pos}")
    return s[start_pos:pos], pos


def _relabel_identifiers(tokens, field_names):
    new_tokens = []
    for kind, value in tokens:
        if kind == "IDENTIFIER":
            if field_names and (value not in field_names):
                # TODO: what to do here?
                pass
            else:
                pass
        new_tokens.append((kind, value))
    return new_tokens


# DEPRECATED: The following tokenizer and parser are kept for backward compatibility
# New code should use str_filter_exp_to_dict_using_ast() instead
def _tokenize(s):
    paren_count = 0
    for c in s:
        if c == "(":
            paren_count += 1
        elif c == ")":
            paren_count -= 1
        if paren_count < 0:
            raise RuntimeError("Unmatched closing parenthesis")
    if paren_count != 0:
        raise RuntimeError("Unbalanced parentheses")

    token_specification = [
        # 1) Numbers or None
        ("NUMBER", r"-?(\d+(\.\d*)?|\.\d+)|None"),
        # 2) String literals
        ("STRING", r'"(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\''),
        # 3) Booleans
        ("BOOLEAN", r"(?<!\w)(?:True|False)(?!\w)"),
        # 4) Functions/Keywords (with word boundaries)
        ("ROUND", r"(?<!\w)round(?!\w)"),
        ("ROUND_TIMESTAMP", r"(?<!\w)round_timestamp(?!\w)"),
        (
            "FUNC",
            r"(?<!\w)(?:len|exists|version|str(?=\()|isNone|time|date|now|max|min|sum|mean|median|mode|var|std|count)(?!\w)",
        ),
        ("BASEFUNC", r"(?<!\w)BASE(?!\w)"),
        # 5) Operators. Note we catch 'not in', 'is not' first:
        (
            "OP",
            r"==|!=|<=|>=|<|>|(?<!\w)(?:not in|is not|in|not|and|or|is)(?!\w)|\*\*|//|\+|\-|\*|/|%",
        ),
        # 6) Identifiers (allow dashes, underscores, slashes, digits, etc.)
        #    We allow them as a single "word" if no whitespace in between
        (
            "IDENTIFIER",
            r"[A-Za-z0-9_/]+(?:-[A-Za-z0-9_/]+)*",
        ),
        # 7) Parentheses / Comma / Bracket
        ("LPAREN", r"\("),
        ("RPAREN", r"\)"),
        ("COMMA", r","),
        ("BRACKET_OPEN", r"[\[\{]"),
        # 8) Whitespace to skip
        ("SKIP", r"[ \t]+"),
        # 9) Any single character that doesn't match
        ("MISMATCH", r"."),
    ]
    tok_regex = "|".join("(?P<%s>%s)" % pair for pair in token_specification)
    get_token = re.compile(tok_regex).match
    line = s
    pos = 0
    tokens = []
    mo = get_token(line, pos)
    while mo is not None:
        kind = mo.lastgroup
        value = mo.group()
        if kind == "NUMBER":
            value = (
                None
                if value == "None"
                else float(value) if "." in value else int(value)
            )
            tokens.append(("NUMBER", value))
        elif kind == "STRING":
            # Remove the surrounding quotes and unescape
            unquoted_value = value[1:-1]
            # If you want to allow embedded quotes or backslashes:
            unquoted_value = (
                unquoted_value.replace(r"\"", '"')
                .replace(r"\'", "'")
                .replace(r"\\", "\\")
            )

            # Check for special string types
            if _is_date_string(unquoted_value):
                tokens.append(("OTHER", unquoted_value))
            elif _is_time_string(unquoted_value):
                tokens.append(("OTHER", unquoted_value))
            elif _is_timedelta_string(unquoted_value):
                tokens.append(("OTHER", unquoted_value))
            else:
                # check if is timestamp
                try:
                    # First try to normalize the timestamp if it's in a non-standard format
                    normalized_value = normalize_timestamp(unquoted_value)
                    tokens.append(("STRING", normalized_value))
                except:
                    # If it's not a valid timestamp, just use the unquoted value
                    tokens.append(("STRING", unquoted_value))
        elif kind == "BOOLEAN":
            value = True if value == "True" else False
            tokens.append(("BOOLEAN", value))
        elif kind == "IDENTIFIER":
            tokens.append((kind, value))
        elif kind in (
            "FUNC",
            "BASEFUNC",
            "ROUND",
            "ROUND_TIMESTAMP",
            "OP",
            "LPAREN",
            "RPAREN",
            "COMMA",
        ):
            tokens.append((kind, value))
        elif kind == "BRACKET_OPEN":
            # We found a [ or {, so let's parse the entire bracketed substring
            # with parse_nested, and store it as an "OTHER" token
            nested_content, new_pos = parse_nested(line, mo.start())
            tokens.append(("OTHER", nested_content))
            pos = new_pos
            mo = get_token(line, pos)
            continue
        elif kind == "SKIP":
            pass  # Ignore whitespace
        elif kind == "MISMATCH":
            raise RuntimeError(f"Unexpected character {value!r} at position {pos}")

        pos = mo.end()
        mo = get_token(line, pos)

    tokens.append(("EOF", ""))
    return tokens


# DEPRECATED: The following parser class is kept for backward compatibility
# New code should use str_filter_exp_to_dict_using_ast() instead
class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.current_token = tokens[0]

    def advance(self):
        self.pos += 1
        if self.pos < len(self.tokens):
            self.current_token = self.tokens[self.pos]
        else:
            self.current_token = ("EOF", "")

    def parse(self):
        result = self.expr()
        if self.current_token[0] != "EOF":
            raise RuntimeError("Unexpected token at end")
        return result

    def expr(self):
        return self.or_expr()

    def or_expr(self):
        node = self.and_expr()
        while self.current_token[0] == "OP" and self.current_token[1] == "or":
            op = self.current_token[1]
            self.advance()
            right = self.and_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def and_expr(self):
        node = self.not_expr()
        while self.current_token[0] == "OP" and self.current_token[1] == "and":
            op = self.current_token[1]
            self.advance()
            right = self.not_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def not_expr(self):
        if self.current_token[0] == "OP" and self.current_token[1] == "not":
            op = self.current_token[1]
            self.advance()
            rhs = self.not_expr()
            return {"operand": op, "rhs": rhs}
        else:
            return self.comp_expr()

    def comp_expr(self):
        node = self.add_sub_expr()
        while self.current_token[0] == "OP" and self.current_token[1] in (
            "==",
            "!=",
            "<",
            ">",
            "<=",
            ">=",
            "in",
            "not in",
            "is",
            "is not",
        ):
            op = self.current_token[1]
            self.advance()
            right = self.add_sub_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def add_sub_expr(self):
        node = self.mul_div_expr()
        while self.current_token[0] == "OP" and self.current_token[1] in ("+", "-"):
            op = self.current_token[1]
            self.advance()
            right = self.mul_div_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def mul_div_expr(self):
        node = self.power_expr()
        while self.current_token[0] == "OP" and self.current_token[1] in (
            "*",
            "/",
            "//",
            "%",
        ):
            op = self.current_token[1]
            self.advance()
            right = self.power_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def power_expr(self):
        node = self.primary()
        while self.current_token[0] == "OP" and self.current_token[1] == "**":
            op = self.current_token[1]
            self.advance()
            right = self.power_expr()  # Note: power is right-associative
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def primary(self):
        # Step 1: parse the core primary expression
        if self.current_token[0] == "FUNC":
            fn = self.current_token[1]
            self.advance()
            if self.current_token[0] == "LPAREN":
                self.advance()
                expr = {} if fn == "now" else self.expr()
                if self.current_token[0] == "RPAREN":
                    self.advance()
                else:
                    raise RuntimeError('Expected ")" after function call')
                node = {"operand": fn, "rhs": expr}
            else:
                raise RuntimeError('Expected "(" after function call')

        # --- 2) handle round(...) with 1 or 2 arguments ---
        elif self.current_token[0] in ("ROUND", "ROUND_TIMESTAMP"):
            fn = self.current_token[0].lower()
            self.advance()  # consume the 'round' / 'round_timestamp' token
            if self.current_token[0] == "LPAREN":
                self.advance()
                # parse the first arg
                first_arg = self.expr()
                args = [first_arg]

                # check if there's a comma -> second arg
                if self.current_token[0] == "COMMA":
                    self.advance()
                    second_arg = self.expr()
                    args.append(second_arg)

                # expect a closing parenthesis
                if self.current_token[0] != "RPAREN":
                    raise RuntimeError(f"Expected ')' after {fn}(...) arguments")
                self.advance()  # consume RPAREN
                node = {"operand": fn, "rhs": args}
            else:
                raise RuntimeError(f"Expected '(' after {fn}")

        # --- 3) handle BASE(...) with 2 arguments ---
        elif self.current_token[0] == "BASEFUNC":
            fn = "BASE"
            self.advance()  # consume BASEFUNC
            if self.current_token[0] == "LPAREN":
                self.advance()
                # parse first arg
                first_arg = self.expr()
                # expect a comma
                if self.current_token[0] != "COMMA":
                    raise RuntimeError(f"Expected ',' after {fn}( arg1")
                self.advance()  # consume comma
                # parse second arg
                second_arg = self.expr()
                if self.current_token[0] != "RPAREN":
                    raise RuntimeError(f"Expected ')' after {fn}(...) arguments")
                self.advance()  # consume RPAREN
                node = {"operand": fn, "rhs": [first_arg, second_arg]}
            else:
                raise RuntimeError(f"Expected '(' after {fn}")

        # --- 4) parentheses grouping ---
        elif self.current_token[0] == "LPAREN":
            self.advance()
            node = self.expr()
            if self.current_token[0] == "RPAREN":
                self.advance()
            else:
                raise RuntimeError('Expected ")"')

        # --- 5) booleans ---
        elif self.current_token[0] == "BOOLEAN":
            node = self.current_token[1]
            self.advance()

        # --- 6) identifiers (including subsequent indexing) ---
        elif self.current_token[0] == "IDENTIFIER":
            node = {"type": "identifier", "value": self.current_token[1]}
            self.advance()

        elif self.current_token[0] == "NUMBER":
            node = self.current_token[1]
            self.advance()

        elif self.current_token[0] == "STRING":
            node = {"type": "string", "value": self.current_token[1]}
            self.advance()

        elif self.current_token[0] == "OTHER":
            node = {"type": "other", "value": self.current_token[1]}
            self.advance()

        else:
            raise RuntimeError(f"Unexpected token {self.current_token}")

        # Step 2: now handle any subsequent indexing: e.g. x[0], BASE(...)[1], etc.
        while self.current_token[0] == "OTHER":
            bracket_str = self.current_token[1]
            if bracket_str.startswith("[") or bracket_str.startswith("{"):
                # remove outer brackets
                inside_str = bracket_str[
                    1:-1
                ]  # drop the leading '[' or '{' and the trailing ']' or '}'
                # tokenize the inside substring
                sub_tokens = _tokenize(inside_str)
                sub_parser = _Parser(sub_tokens)
                inside_expr = sub_parser.parse()
                node = {
                    "operand": "INDEX",
                    "lhs": node,
                    "rhs": inside_expr,
                }
                # consume this bracketed token
                self.advance()
            else:
                # if for some reason it's "OTHER" not starting with bracket, just break or raise
                break

        return node


# AST-based Filtering #
# -------------------#


def _ast_op_to_str(op: ast.AST) -> str:
    """
    Converts AST operator nodes to string representations used by the filter dictionary.

    Args:
        op: An AST operator node (e.g., ast.Add, ast.Sub, etc.)

    Returns:
        String representation of the operator
    """
    # Binary operators
    if isinstance(op, ast.Add):
        return "+"
    elif isinstance(op, ast.Sub):
        return "-"
    elif isinstance(op, ast.Mult):
        return "*"
    elif isinstance(op, ast.Div):
        return "/"
    elif isinstance(op, ast.FloorDiv):
        return "//"
    elif isinstance(op, ast.Mod):
        return "%"
    elif isinstance(op, ast.Pow):
        return "**"

    # Comparison operators
    elif isinstance(op, ast.Eq):
        return "=="
    elif isinstance(op, ast.NotEq):
        return "!="
    elif isinstance(op, ast.Lt):
        return "<"
    elif isinstance(op, ast.LtE):
        return "<="
    elif isinstance(op, ast.Gt):
        return ">"
    elif isinstance(op, ast.GtE):
        return ">="
    elif isinstance(op, ast.Is):
        return "is"
    elif isinstance(op, ast.IsNot):
        return "is not"
    elif isinstance(op, ast.In):
        return "in"
    elif isinstance(op, ast.NotIn):
        return "not in"

    # Boolean operators
    elif isinstance(op, ast.And):
        return "and"
    elif isinstance(op, ast.Or):
        return "or"
    elif isinstance(op, ast.Not):
        return "not"

    # Unary operators
    elif isinstance(op, ast.USub):
        return "-"
    elif isinstance(op, ast.UAdd):
        return "+"

    # Default case
    else:
        raise ValueError(f"Unsupported operator type: {type(op)}")


def _transform_ast(node: ast.AST, preserve_string_literals: bool = False) -> dict:
    """
    Recursively transforms an AST node into a filter dictionary.

    Args:
        node: An AST node

    Returns:
        A dictionary representation of the node in the format expected by build_sql_query
    """
    # Handle literals (constants)
    if isinstance(node, ast.Constant):
        # Check for raw base64 image data URI first
        if isinstance(node.value, str):
            if node.value.startswith("data:image/") and ";base64," in node.value:
                return {"type": "image", "value": node.value}
        # Skip timestamp normalization if we're in a string method context
        if preserve_string_literals:
            return node.value

        try:
            # First try to normalize the timestamp if it's in a non-standard format
            normalized_value = normalize_timestamp(node.value)
        except Exception as e:
            normalized_value = node.value
        return normalized_value

    # Handle variable names (identifiers)
    elif isinstance(node, ast.Name):
        return {"type": "identifier", "value": node.id}

    # Handle unary operations (not, +, -)
    elif isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return {
                "operand": "not",
                "rhs": _transform_ast(node.operand, preserve_string_literals),
            }
        elif isinstance(node.op, ast.USub):
            # Handle negative numbers
            if isinstance(node.operand, ast.Constant):
                return -node.operand.value
            else:
                # For more complex expressions, use a binary operation with 0
                return {
                    "operand": "-",
                    "lhs": 0,
                    "rhs": _transform_ast(node.operand, preserve_string_literals),
                }
        elif isinstance(node.op, ast.UAdd):
            # Positive sign, just return the operand
            return _transform_ast(node.operand, preserve_string_literals)

    # Handle binary operations (+, -, *, /, etc.)
    elif isinstance(node, ast.BinOp):
        return {
            "lhs": _transform_ast(node.left, preserve_string_literals),
            "operand": _ast_op_to_str(node.op),
            "rhs": _transform_ast(node.right, preserve_string_literals),
        }

    # Handle boolean operations (and, or)
    elif isinstance(node, ast.BoolOp):
        # For multiple operands (a and b and c), we need to nest them
        result = _transform_ast(node.values[0], preserve_string_literals)
        for value in node.values[1:]:
            result = {
                "lhs": result,
                "operand": _ast_op_to_str(node.op),
                "rhs": _transform_ast(value, preserve_string_literals),
            }
        return result

    # Handle comparisons (==, !=, <, >, etc.)
    elif isinstance(node, ast.Compare):
        # For multiple comparisons (a < b < c), we need to handle each pair
        result = _transform_ast(node.left, preserve_string_literals)
        for op, comparator in zip(node.ops, node.comparators):
            result = {
                "lhs": result,
                "operand": _ast_op_to_str(op),
                "rhs": _transform_ast(comparator, preserve_string_literals),
            }
        return result

    # Handle function calls
    elif isinstance(node, ast.Call):
        func_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else ast.unparse(node.func).strip()
        )

        # Special-case vector similarity functions
        if func_name in SIMILARITY_FUNCS and len(node.args) == 2:
            lhs = _transform_ast(node.args[0], preserve_string_literals)
            rhs = _transform_ast(node.args[1], preserve_string_literals)
            return {"lhs": lhs, "operand": func_name, "rhs": rhs}

        # Handle special functions
        if func_name in (
            "len",
            "exists",
            "version",
            "str",
            "isNone",
            "time",
            "date",
            "now",
            "round",
            "round_timestamp",
            "num_tokens",
            "mean",
            "sum",
            "var",
            "std",
            "min",
            "max",
            "median",
            "mode",
            "count",
            "phash",
        ):
            if func_name == "count":
                func_name = "len"
            # For functions with a single argument
            if len(node.args) == 1:
                return {
                    "operand": func_name,
                    "rhs": _transform_ast(node.args[0], preserve_string_literals),
                }
            # For functions with multiple arguments (like round with precision)
            else:
                return {
                    "operand": func_name,
                    "rhs": [
                        _transform_ast(arg, preserve_string_literals)
                        for arg in node.args
                    ],
                }
        # Handle isinstance(x, "type") by lowering to: type(x) == "type"
        elif func_name == "isinstance":
            # Support isinstance(x, "type") and isinstance(x, ("type1", "type2", ...))
            if len(node.args) != 2:
                raise HTTPException(
                    status_code=400,
                    detail="isinstance() requires exactly 2 arguments",
                )
            value_expr = _transform_ast(node.args[0], preserve_string_literals)

            def _as_type_literal(arg_node):
                if isinstance(arg_node, ast.Constant) and isinstance(
                    arg_node.value,
                    str,
                ):
                    return {"type": "type_literal", "value": arg_node.value}
                raise HTTPException(
                    status_code=400,
                    detail="isinstance() second argument must be a string literal or tuple/list of string literals",
                )

            rhs_arg = node.args[1]
            type_expr = {"operand": "type", "rhs": value_expr}

            # Tuple/List of string literals -> use membership: type(x) in [..]
            if isinstance(rhs_arg, (ast.Tuple, ast.List)):
                type_list = [_as_type_literal(elt) for elt in rhs_arg.elts]
                return {"lhs": type_expr, "operand": "in", "rhs": type_list}
            # Single string literal
            rhs_literal = _as_type_literal(rhs_arg)
            return {"lhs": type_expr, "operand": "==", "rhs": rhs_literal}

        # Handle embed function (multi-arg: text, optional model, optional dimensions)
        elif func_name == "embed":
            return {
                "operand": "embed",
                "rhs": [
                    _transform_ast(arg, preserve_string_literals) for arg in node.args
                ],
            }
        # Handle embed_image function (single-arg: base64 image)
        elif func_name == "embed_image":
            # Single argument function - rhs should be the direct argument, not a list
            if len(node.args) != 1:
                raise ValueError("embed_image() requires exactly 1 argument")
            return {
                "operand": "embed_image",
                "rhs": _transform_ast(node.args[0], preserve_string_literals),
            }
        # Handle BASE function
        elif func_name == "BASE":
            # BASE takes exactly 2 arguments: event_ids and key
            if len(node.args) != 2:
                raise ValueError("BASE function requires exactly 2 arguments")
            return {
                "operand": "BASE",
                "rhs": [
                    _transform_ast(arg, preserve_string_literals) for arg in node.args
                ],
            }
        # Handle zip function
        elif func_name == "zip":
            return {
                "operand": "zip",
                "rhs": [
                    _transform_ast(arg, preserve_string_literals) for arg in node.args
                ],
            }
        # Handle string methods (lower, upper, capitalize, strip, etc.)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in (
            "lower",
            "upper",
            "capitalize",
            "strip",
            "lstrip",
            "rstrip",
            "startswith",
            "endswith",
            "contains",
            "match",
            "replace",
            "substring",
        ):
            attr = node.func.attr
            # Validate argument counts per method
            if attr in ("lower", "upper", "capitalize") and len(node.args) > 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"str.{attr}() invalid args: expected 0, got {len(node.args)}",
                )
            elif attr in ("strip", "lstrip", "rstrip") and len(node.args) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"str.{attr}() invalid args: expected 0 or 1, got {len(node.args)}",
                )
            elif attr in ("startswith", "endswith") and len(node.args) != 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"str.{attr}() invalid args: expected 1, got {len(node.args)}",
                )
            elif attr == "contains" and len(node.args) != 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"str.contains() invalid args: expected 1, got {len(node.args)}",
                )
            elif attr == "match" and len(node.args) != 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"str.match() invalid args: expected 1, got {len(node.args)}",
                )
            elif attr == "replace" and len(node.args) != 2:
                raise HTTPException(
                    status_code=400,
                    detail=f"str.replace() invalid args: expected 2, got {len(node.args)}",
                )
            elif attr == "substring" and not (1 <= len(node.args) <= 2):
                raise HTTPException(
                    status_code=400,
                    detail=f"str.substring() invalid args: expected 1-2, got {len(node.args)}",
                )

            # For string methods that take literal string arguments, preserve them exactly
            preserve_args = attr in (
                "startswith",
                "endswith",
                "contains",
                "match",
                "replace",
            )

            return {
                "operand": "str_method",
                "method": attr,
                "rhs": _transform_ast(node.func.value, preserve_string_literals),
                "args": [
                    _transform_ast(a, preserve_string_literals=preserve_args)
                    for a in node.args
                ],
            }
        # Handle dict methods (keys, values, items, get, setdefault)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in (
            "keys",
            "values",
            "items",
            "get",
            "setdefault",
        ):
            if node.func.attr in ("get", "setdefault"):
                # Handle dict.get(key, default) method
                if len(node.args) == 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"dict.{node.func.attr}() requires at least one argument",
                    )
                if len(node.args) > 2:
                    raise HTTPException(
                        status_code=400,
                        detail=f"dict.{node.func.attr}() accepts at most two arguments",
                    )

                container = _transform_ast(node.func.value, preserve_string_literals)
                key_expr = _transform_ast(node.args[0], preserve_string_literals)
                default_expr = None
                if len(node.args) > 1:
                    default_expr = _transform_ast(
                        node.args[1],
                        preserve_string_literals,
                    )

                method = "get" if node.func.attr == "get" else "setdefault"

                filter_dict = {
                    "operand": "dict_method",
                    # Route setdefault through the same executor branch as get()
                    "method": method,
                    "rhs": container,
                    "key": key_expr,
                    "default": default_expr,
                    "default_supplied": len(node.args) > 1,
                }

                return filter_dict
            else:
                # Handle other dict methods (keys, values, items)
                return {
                    "operand": "dict_method",
                    "method": node.func.attr,
                    "rhs": _transform_ast(node.func.value, preserve_string_literals),
                }

        # Handle other function calls
        else:
            # Default handling for other functions
            return {
                "operand": func_name,
                "rhs": [
                    _transform_ast(arg, preserve_string_literals) for arg in node.args
                ],
            }

    # Handle subscripts (indexing with [] or {})
    elif isinstance(node, ast.Subscript):
        if isinstance(node.slice, ast.Slice):
            # Handle slice operations [lower:upper]
            lower = (
                None
                if node.slice.lower is None
                else _transform_ast(node.slice.lower, preserve_string_literals)
            )
            upper = (
                None
                if node.slice.upper is None
                else _transform_ast(node.slice.upper, preserve_string_literals)
            )
            return {
                "operand": "SLICE",
                "lhs": _transform_ast(node.value, preserve_string_literals),
                "rhs": [lower, upper],
            }
        else:
            # Handle regular index operations
            return {
                "operand": "INDEX",
                "lhs": _transform_ast(node.value, preserve_string_literals),
                "rhs": _transform_ast(node.slice, preserve_string_literals),
            }

    # Handle property access (e.g., my_dict.key -> my_dict['key'])
    elif isinstance(node, ast.Attribute):
        return {
            "operand": "INDEX",
            "lhs": _transform_ast(node.value, preserve_string_literals),
            "rhs": node.attr,
        }

    # Handle lists and tuples
    elif isinstance(node, (ast.List, ast.Tuple)):
        return [_transform_ast(elt, preserve_string_literals) for elt in node.elts]

    # Handle dictionaries
    elif isinstance(node, ast.Dict):
        return {
            _transform_ast(k, preserve_string_literals): _transform_ast(
                v,
                preserve_string_literals,
            )
            for k, v in zip(node.keys, node.values)
        }

    # Handle string literals that might be parsed as Expr nodes
    elif isinstance(node, ast.Expr):
        return _transform_ast(node.value, preserve_string_literals)

    # Handle the root Expression node from ast.parse(mode='eval')
    elif isinstance(node, ast.Expression):
        return _transform_ast(node.body, preserve_string_literals)

    # Handle if expressions
    if isinstance(node, ast.IfExp):
        return {
            "operand": "if_expr",
            "test": _transform_ast(node.test, preserve_string_literals),
            "body": _transform_ast(node.body, preserve_string_literals),
            "orelse": _transform_ast(node.orelse, preserve_string_literals),
        }

    # Handle list comprehensions
    if isinstance(node, ast.ListComp):
        comp = node.generators[0]
        return {
            "operand": "list_comp",
            "elt": _transform_ast(node.elt, preserve_string_literals),
            "target": _transform_ast(comp.target, preserve_string_literals),
            "iter": _transform_ast(comp.iter, preserve_string_literals),
            "ifs": [_transform_ast(iff, preserve_string_literals) for iff in comp.ifs],
        }

    # Handle dictionary comprehensions
    if isinstance(node, ast.DictComp):
        comp = node.generators[0]
        return {
            "operand": "dict_comp",
            "key_elt": _transform_ast(node.key, preserve_string_literals),
            "val_elt": _transform_ast(node.value, preserve_string_literals),
            "target": _transform_ast(comp.target, preserve_string_literals),
            "iter": _transform_ast(comp.iter, preserve_string_literals),
            "ifs": [_transform_ast(iff, preserve_string_literals) for iff in comp.ifs],
        }

    # Default case for unsupported nodes
    raise ValueError(f"Unsupported AST node type: {type(node)}")


def str_filter_exp_to_dict_using_ast(expr: str, field_names=None) -> dict:
    """
    Converts a string filter expression to a filter dictionary using Python's AST.
    Args:
        expr: The filter expression string
        field_names: Optional dictionary of field names from get_field_types

    Returns:
        A filter dictionary that can be used with build_sql_query

    Raises:
        HTTPException: If the expression is invalid or cannot be parsed
    """
    try:
        # Handle problematic field names by creating placeholders
        special_fields = {}
        problematic_chars = {"-", "/", "+", "*", "&", "|", "^"}
        processed_expr = textwrap.dedent(expr).replace("\n", " ")

        if field_names:
            # Replace problematic field names with placeholders
            for field_name in field_names:
                if any(char in field_name for char in problematic_chars):
                    placeholder = f"__FIELD_PLACEHOLDER_{len(special_fields)}__"
                    special_fields[field_name] = placeholder

                    # Replace the field name with its placeholder
                    escaped_field = re.escape(field_name)
                    processed_expr = re.sub(
                        r"\b" + escaped_field + r"\b",
                        placeholder,
                        processed_expr,
                    )

        # Parse the preprocessed expression
        tree = ast.parse(processed_expr, mode="eval")

        # Transform the AST into a filter dictionary
        filter_dict = _transform_ast(tree)

        # Restore original field names if needed
        if special_fields:
            # Create reverse mapping
            reverse_mapping = {
                placeholder: field_name
                for field_name, placeholder in special_fields.items()
            }

            # Helper function to restore field names
            def restore_field_names(obj):
                if isinstance(obj, dict):
                    if (
                        obj.get("type") == "identifier"
                        and obj.get("value") in reverse_mapping
                    ):
                        obj["value"] = reverse_mapping[obj["value"]]
                    else:
                        for k, v in obj.items():
                            obj[k] = restore_field_names(v)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        obj[i] = restore_field_names(item)
                return obj

            filter_dict = restore_field_names(filter_dict)

        return filter_dict
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid filter expression: {e}")


def str_filter_exp_to_dict(s, field_names=None):
    """
    Converts a string filter expression to a filter dictionary.

    This function now uses the AST-based parser for more robust parsing.
    The old tokenizer-based parser is kept for backward compatibility.

    Args:
        s: The filter expression string
        field_names: Optional list of field names for validation

    Returns:
        A filter dictionary that can be used with build_sql_query

    Raises:
        HTTPException: If the expression is invalid or cannot be parsed
    """
    try:
        # Use the new AST-based parser
        return str_filter_exp_to_dict_using_ast(s, field_names)
    except Exception as e:
        # Fall back to the old tokenizer-based parser
        try:
            tokens = _tokenize(s)
            if field_names is not None:
                tokens = _relabel_identifiers(tokens, field_names)
            parser = _Parser(tokens)
            result = parser.parse()
            return result
        except Exception as fallback_e:
            # If both parsers fail, raise the original error
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filter expression: {fallback_e}",
            )

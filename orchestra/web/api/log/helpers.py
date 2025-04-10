import ast
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple, Union

from fastapi import HTTPException
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BindParameter,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Interval,
    Numeric,
    String,
    Text,
    Time,
    and_,
    case,
    cast,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.log_dao import (
    LogDAO,
    _is_date_string,
    _is_time_string,
    _is_timedelta_string,
    normalize_timestamp,
)
from orchestra.db.models.orchestra_models import (
    DerivedLog,
    JSONLog,
    JSONLogHistory,
    Log,
)

STR_TO_SQL_TYPES = {
    "bool": Boolean,
    "int": Integer,
    "float": Float,
    "str": String,
    "timestamp": DateTime,
    "time": Time,
    "date": Date,
    "timedelta": Interval,
    "dict": JSONB,
    "list": JSONB,
}


def _extract_placeholders(equation: str) -> List[str]:
    """
    Find placeholders like '{log0:score}' in the equation.
    """
    pattern = re.compile(r"\{([^}]+)\}")
    return pattern.findall(equation)


def _substitute_placeholders(equation: str, single_ref: Dict[str, int]) -> str:
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


def _compute_expression(filter_dict, log_event_alias, session, log_event_ids=None):
    """
    Use build_sql_query -> subquery or expression -> .execute() -> return single result.
    If multiple rows, pick the first or do an aggregator as needed.
    """
    expr = build_sql_query(
        filter_dict,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=True,
    )
    if isinstance(expr, Subquery):
        rows = session.execute(select(expr.c.log_event_id, expr.c.value)).fetchall()
        if not rows:
            return None
        return rows
    else:
        return session.execute(select(expr)).scalar()


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
    for (kind, value) in tokens:
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
            r"(?<!\w)(?:len|exists|version|str(?=\()|isNone|time|date|now)(?!\w)",
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
                else float(value)
                if "." in value
                else int(value)
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


def _transform_ast(node: ast.AST) -> dict:
    """
    Recursively transforms an AST node into a filter dictionary.

    Args:
        node: An AST node

    Returns:
        A dictionary representation of the node in the format expected by build_sql_query
    """
    # Handle literals (constants)
    if isinstance(node, ast.Constant):
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
            return {"operand": "not", "rhs": _transform_ast(node.operand)}
        elif isinstance(node.op, ast.USub):
            # Handle negative numbers
            if isinstance(node.operand, ast.Constant):
                return -node.operand.value
            else:
                # For more complex expressions, use a binary operation with 0
                return {"operand": "-", "lhs": 0, "rhs": _transform_ast(node.operand)}
        elif isinstance(node.op, ast.UAdd):
            # Positive sign, just return the operand
            return _transform_ast(node.operand)

    # Handle binary operations (+, -, *, /, etc.)
    elif isinstance(node, ast.BinOp):
        return {
            "lhs": _transform_ast(node.left),
            "operand": _ast_op_to_str(node.op),
            "rhs": _transform_ast(node.right),
        }

    # Handle boolean operations (and, or)
    elif isinstance(node, ast.BoolOp):
        # For multiple operands (a and b and c), we need to nest them
        result = _transform_ast(node.values[0])
        for value in node.values[1:]:
            result = {
                "lhs": result,
                "operand": _ast_op_to_str(node.op),
                "rhs": _transform_ast(value),
            }
        return result

    # Handle comparisons (==, !=, <, >, etc.)
    elif isinstance(node, ast.Compare):
        # For multiple comparisons (a < b < c), we need to handle each pair
        result = _transform_ast(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            result = {
                "lhs": result,
                "operand": _ast_op_to_str(op),
                "rhs": _transform_ast(comparator),
            }
        return result

    # Handle function calls
    elif isinstance(node, ast.Call):
        func_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else ast.unparse(node.func).strip()
        )

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
        ):
            # For functions with a single argument
            if len(node.args) == 1:
                return {"operand": func_name, "rhs": _transform_ast(node.args[0])}
            # For functions with multiple arguments (like round with precision)
            else:
                return {
                    "operand": func_name,
                    "rhs": [_transform_ast(arg) for arg in node.args],
                }
        # Handle BASE function
        elif func_name == "BASE":
            # BASE takes exactly 2 arguments: event_ids and key
            if len(node.args) != 2:
                raise ValueError("BASE function requires exactly 2 arguments")
            return {
                "operand": "BASE",
                "rhs": [_transform_ast(arg) for arg in node.args],
            }
        # Handle zip function
        elif func_name == "zip":
            return {"operand": "zip", "rhs": [_transform_ast(arg) for arg in node.args]}
        # Handle dict methods (keys, values, items)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in (
            "keys",
            "values",
            "items",
        ):
            return {
                "operand": "dict_method",
                "method": node.func.attr,
                "rhs": _transform_ast(node.func.value),
            }

        # Handle other function calls
        else:
            # Default handling for other functions
            return {
                "operand": func_name,
                "rhs": [_transform_ast(arg) for arg in node.args],
            }

    # Handle subscripts (indexing with [] or {})
    elif isinstance(node, ast.Subscript):
        return {
            "operand": "INDEX",
            "lhs": _transform_ast(node.value),
            "rhs": _transform_ast(node.slice),
        }

    # Handle lists and tuples
    elif isinstance(node, (ast.List, ast.Tuple)):
        return [_transform_ast(elt) for elt in node.elts]

    # Handle dictionaries
    elif isinstance(node, ast.Dict):
        return {
            _transform_ast(k): _transform_ast(v) for k, v in zip(node.keys, node.values)
        }

    # Handle string literals that might be parsed as Expr nodes
    elif isinstance(node, ast.Expr):
        return _transform_ast(node.value)

    # Handle the root Expression node from ast.parse(mode='eval')
    elif isinstance(node, ast.Expression):
        return _transform_ast(node.body)

    # Handle if expressions
    if isinstance(node, ast.IfExp):
        return {
            "operand": "if_expr",
            "test": _transform_ast(node.test),
            "body": _transform_ast(node.body),
            "orelse": _transform_ast(node.orelse),
        }

    # Handle list comprehensions
    if isinstance(node, ast.ListComp):
        comp = node.generators[0]
        return {
            "operand": "list_comp",
            "elt": _transform_ast(node.elt),
            "target": _transform_ast(comp.target),
            "iter": _transform_ast(comp.iter),
            "ifs": [_transform_ast(iff) for iff in comp.ifs],
        }

    # Handle dictionary comprehensions
    if isinstance(node, ast.DictComp):
        comp = node.generators[0]
        return {
            "operand": "dict_comp",
            "key_elt": _transform_ast(node.key),
            "val_elt": _transform_ast(node.value),
            "target": _transform_ast(comp.target),
            "iter": _transform_ast(comp.iter),
            "ifs": [_transform_ast(iff) for iff in comp.ifs],
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
        processed_expr = expr

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


def _select_value(subq, session, is_collection=False):
    """
    Helper function to select the appropriate value column from a subquery.
    Prioritizes 'value' if it exists, otherwise selects based on inferred types.
    """
    if isinstance(subq, BindParameter):
        return subq.value, LogDAO.infer_type("", subq.value)
    if hasattr(subq.c, "value"):
        if is_collection:
            # the assumption here is lists/dicts to have a single consistent type
            # so we can just check the first element
            first_elem = session.execute(select(subq).limit(1)).first()[0]
            dt = LogDAO.infer_type("", first_elem)
        else:
            dt = session.execute(select(subq.c.inferred_type)).first()[0]
        return subq.c.value, dt
    try:
        dt = session.execute(select(subq)).first()[
            -1
        ]  # execute the subquery to determine the type.
        d = {
            "int": subq.c.int_value,
            "float": subq.c.float_value,
            "bool": subq.c.bool_value,
            "str": subq.c.str_value,
            "timestamp": subq.c.timestamp_value,
            "time": subq.c.time_value,
            "date": subq.c.date_value,
            "timedelta": subq.c.timedelta_value,
            "list": subq.c.jsonb_value,
            "dict": subq.c.jsonb_value,
            "NoneType": subq.c.int_value,
            "image": subq.c.str_value,
        }
        return d[dt], dt
    except:
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
        "bool",
        "int",
        "float",
        "str",
        "timestamp",
        "time",
        "date",
        "timedelta",
        "list",
        "dict",
        "tuple",
        "image",
        "NoneType",
    ]

    # If either side is "none", we skip it or treat it as the other side
    if t1 is None:
        return t2
    if t2 is None:
        return t1

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
        return func.replace(cast(expr, String), '"', "")
    elif final_type == "float":
        return cast(expr, Float)
    elif final_type == "int":
        return cast(expr, Integer)
    elif final_type == "bool":
        return cast(expr, Boolean)
    elif final_type == "timestamp":
        return cast(func.replace(cast(expr, Text), '"', ""), DateTime(timezone=True))
    elif final_type == "time":
        return cast(func.replace(cast(expr, Text), '"', ""), Time)
    elif final_type == "date":
        return cast(func.replace(cast(expr, Text), '"', ""), Date)
    elif final_type == "timedelta":
        return cast(func.replace(cast(expr, Text), '"', ""), Interval)
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
):
    """
    Build a subselect that retrieves columns for a given log key.
    The returned subselect columns typically include:
      - id (to allow joining)
      - several casted columns (str_value, int_value, float_value, bool_value, jsonb_value)
    """

    def extract_json_text(col):
        # This uses the PostgreSQL operator ->> to extract the JSON scalar as text.
        return col.op("#>>")(literal_column("'{}'"))

    log_alias = aliased(Log, name="log_alias")
    derived_log_alias = aliased(DerivedLog, name="derived_log_alias")
    if log_event_ids is None:
        # TODO(yusha): figure out why empty ids were passed and remove this check once we have a better way to handle it
        log_id_condition = True
        derived_log_id_condition = True
    elif isinstance(log_event_ids, list):
        # For derived logs, we pass reference logs as list of ids
        log_id_condition = log_alias.log_event_id.in_(log_event_ids)
        derived_log_id_condition = derived_log_alias.log_event_id.in_(log_event_ids)
        log_event_condition = log_event_alias.id.in_(log_event_ids)
    else:
        # assert that log_event_ids is a subquery
        assert isinstance(log_event_ids, Subquery)
        log_id_condition = log_alias.log_event_id.in_(select(log_event_ids))
        derived_log_id_condition = derived_log_alias.log_event_id.in_(
            select(log_event_ids),
        )
        log_event_condition = log_event_alias.id.in_(select(log_event_ids))
    # Special handling for log_id field
    if key == "log_id":
        subq = (
            select(
                log_event_alias.id.label("log_event_id"),
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
            )
            .where(log_event_condition)
            .subquery(name=alias)
        )
        return subq

    # Special handling for created_at and updated_at fields from LogEvent table
    if key in ("created_at", "updated_at"):
        subq = (
            select(
                log_event_alias.id.label("log_event_id"),
                literal(None).label("jsonb_value"),
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
                literal("timestamp").label("inferred_type"),
            )
            .where(log_event_condition)
            .subquery(name=alias)
        )
        return subq

    # Build base logs subquery
    base_subq = select(
        log_alias.log_event_id.label("log_event_id"),
        case(
            (log_alias.inferred_type == "list", cast(log_alias.value, JSONB)),
            (log_alias.inferred_type == "dict", cast(log_alias.value, JSONB)),
            else_=None,
        ).label("jsonb_value"),
        case(
            (log_alias.inferred_type == "timestamp", cast(log_alias.value, JSONB)),
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
            else_=None,
        ).label("str_value"),
        case(
            (log_alias.inferred_type == "int", cast(log_alias.value, Integer)),
            else_=None,
        ).label("int_value"),
        case(
            (log_alias.inferred_type == "float", cast(log_alias.value, Float)),
            else_=None,
        ).label("float_value"),
        case(
            (log_alias.inferred_type == "bool", cast(log_alias.value, Boolean)),
            else_=None,
        ).label("bool_value"),
        log_alias.inferred_type.label("inferred_type"),
    ).where(
        log_id_condition,
        log_alias.key == key,
    )

    # Build derived logs subquery
    derived_subq = select(
        derived_log_alias.log_event_id.label("log_event_id"),
        case(
            (
                derived_log_alias.inferred_type == "list",
                cast(derived_log_alias.value, JSONB),
            ),
            (
                derived_log_alias.inferred_type == "dict",
                cast(derived_log_alias.value, JSONB),
            ),
            else_=None,
        ).label("jsonb_value"),
        case(
            (
                derived_log_alias.inferred_type == "timestamp",
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
            else_=None,
        ).label("str_value"),
        case(
            (
                derived_log_alias.inferred_type == "int",
                cast(derived_log_alias.value, Integer),
            ),
            else_=None,
        ).label("int_value"),
        case(
            (
                derived_log_alias.inferred_type == "float",
                cast(derived_log_alias.value, Float),
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
    ).where(
        derived_log_id_condition,
        derived_log_alias.key == key,
    )
    # Combine base and derived logs with union
    combined_subq = base_subq.union_all(derived_subq).subquery(name=alias)
    return combined_subq


def _join_subqueries(lhs_subq, rhs_subq, expr, inferred_type, session=None):
    """
    Given two subqueries lhs_subq and rhs_subq and an expression expr that combines
    their respective columns, produce a new subquery that merges them (by log_event_id),
    with 'expr' as the 'value' column.

    This is useful for arithmetic operations and comparisons. The resulting
    subquery can be used in further operations.
    """
    # Get the value columns for both sides
    lhs_val, lhs_type = _select_value(lhs_subq, session)
    rhs_val, rhs_type = _select_value(rhs_subq, session)

    j = (
        select(
            func.coalesce(lhs_subq.c.log_event_id, rhs_subq.c.log_event_id).label(
                "log_event_id",
            ),
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
            literal(inferred_type).label("inferred_type"),
        )
        .select_from(lhs_subq)
        .outerjoin(rhs_subq, lhs_subq.c.log_event_id == rhs_subq.c.log_event_id)
        .subquery()
    )
    return j


# Helper function for logical operators (and, or, not)
def _handle_logical_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
):
    """
    Handles logical operators ('and', 'or', 'not') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the logical operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        Subquery or SQLAlchemy condition based on the logical operator.
    """
    operand = filter_dict.get("operand")
    lhs = (
        build_sql_query(
            filter_dict.get("lhs"),
            log_event_alias,
            session,
            log_event_ids=log_event_ids,
            is_derived=is_derived,
        )
        if operand != "not"
        else None
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )

    # Check if lhs and rhs are subqueries
    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    def _true_ids(subq):
        return select(subq.c.log_event_id).where(subq.c.value.is_(True))

    def _make_bool_subq(ids_selectable):
        tmp = ids_selectable.subquery()
        return (
            select(
                tmp.c.log_event_id.label("log_event_id"),
                literal(True).label("value"),
                literal("bool").label("inferred_type"),
            )
            .select_from(tmp)
            .subquery()
        )

    # Handle "not"
    if operand == "not":
        if rhs_is_sub:
            not_expr = not_(rhs.c.value)
            return (
                select(
                    rhs.c.log_event_id.label("log_event_id"),
                    not_expr.label("value"),
                    literal("bool").label("inferred_type"),
                )
                .select_from(rhs)
                .subquery()
            )
        else:
            return not_(rhs)

    # Handle "and"/"or"
    if operand in ("and", "or"):
        if lhs_is_sub and rhs_is_sub:
            lhs_ids = _true_ids(lhs)
            rhs_ids = _true_ids(rhs)
            combined_ids = (
                lhs_ids.intersect(rhs_ids)
                if operand == "and"
                else lhs_ids.union(rhs_ids)
            )
            return _make_bool_subq(combined_ids)

        elif lhs_is_sub and not rhs_is_sub:
            if operand == "and":
                passed_ids = _true_ids(lhs).subquery()
                filtered_ids = (
                    select(passed_ids.c.log_event_id.label("log_event_id"))
                    .join(
                        log_event_alias,
                        log_event_alias.id == passed_ids.c.log_event_id,
                    )
                    .where(rhs)
                )
                return _make_bool_subq(filtered_ids)
            else:
                passed_ids = _true_ids(lhs)
                pass_rhs = select(log_event_alias.id.label("log_event_id")).where(rhs)
                combined = passed_ids.union(pass_rhs)
                return _make_bool_subq(combined)

        elif not lhs_is_sub and rhs_is_sub:
            if operand == "and":
                passed_ids = _true_ids(rhs).subquery()
                filtered_ids = (
                    select(passed_ids.c.log_event_id.label("log_event_id"))
                    .join(
                        log_event_alias,
                        log_event_alias.id == passed_ids.c.log_event_id,
                    )
                    .where(lhs)
                )
                return _make_bool_subq(filtered_ids)
            else:
                pass_rhs = _true_ids(rhs)
                pass_lhs = select(log_event_alias.id.label("log_event_id")).where(lhs)
                combined = pass_lhs.union(pass_rhs)
                return _make_bool_subq(combined)

        else:
            return and_(lhs, rhs) if operand == "and" else or_(lhs, rhs)

    raise ValueError(f"Unknown logical operand: {operand}")


def _arithmetic_expr(lval, rval, operand, lval_type, rval_type):
    # Special handling for date/time/timestamp and timedelta arithmetic
    if operand == "+" and lval_type == "timestamp" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), Interval)
        expr = lval + rval
        result_type = "timestamp"
    elif operand == "-" and lval_type == "timestamp" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "timestamp"
    elif operand == "-" and lval_type == "timestamp" and rval_type == "timestamp":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), TIMESTAMP)
        expr = lval - rval
        result_type = "timedelta"
    elif operand == "-" and lval_type == "date" and rval_type == "date":
        lval = cast(cast(lval, Text), Date)
        rval = cast(cast(rval, Text), Date)
        expr = cast(lval, TIMESTAMP) - cast(rval, TIMESTAMP)
        result_type = "timedelta"
    elif operand == "+" and lval_type == "date" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Date)
        rval = cast(rval, Interval)
        expr = lval + rval
        result_type = "date"
    elif operand == "-" and lval_type == "date" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Date)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "date"
    elif operand == "+" and lval_type == "time" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Time)
        rval = cast(cast(rval, Text), Interval)
        expr = lval + rval
        result_type = "time"
    elif operand == "-" and lval_type == "time" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Time)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "time"
    elif (
        operand == "+"
        and lval_type == "timedelta"
        and rval_type in ("timestamp", "date", "time")
    ):
        lval = cast(lval, Interval)
        if rval_type == "timestamp":
            rval = cast(cast(rval, Text), TIMESTAMP)
        elif rval_type == "date":
            rval = cast(cast(rval, Text), Date)
        else:  # time
            rval = cast(cast(rval, Text), Time)
        expr = lval + rval
        result_type = rval_type
    elif operand == "+" and lval_type == "timedelta" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Interval)
        rval = cast(cast(rval, Text), Interval)
        expr = lval + rval
        result_type = "timedelta"
    elif operand == "-" and lval_type == "timedelta" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Interval)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "timedelta"
    elif operand == "*" and lval_type == "timedelta" and rval_type in ("int", "float"):
        lval = cast(cast(lval, Text), Interval)
        rval = cast(rval, Float)
        expr = lval * rval
        result_type = "timedelta"
    elif operand == "*" and lval_type in ("int", "float") and rval_type == "timedelta":
        lval = cast(lval, Float)
        rval = cast(cast(rval, Text), Interval)
        expr = lval * rval
        result_type = "timedelta"
    elif operand == "/" and lval_type == "timedelta" and rval_type in ("int", "float"):
        lval = cast(cast(lval, Text), Interval)
        rval = cast(rval, Float)
        expr = lval / rval
        result_type = "timedelta"
    elif operand == "/" and lval_type == "timedelta" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Interval)
        rval = cast(cast(rval, Text), Interval)
        expr = func.extract("epoch", lval) / func.extract("epoch", rval)
        result_type = "float"
    else:
        lval = cast_expr(lval, lval_type, rval_type)
        rval = cast_expr(rval, rval_type, lval_type)
        if operand == "+":
            if lval_type == "str" and rval_type == "str":
                lval = func.replace(cast(lval, String), '"', "")
                rval = func.replace(cast(rval, String), '"', "")
                expr = func.concat(lval, rval)
            else:
                expr = lval + rval
        elif operand == "-":
            expr = lval - rval
        elif operand == "*":
            expr = lval * rval
        elif operand == "/":
            expr = lval / rval
        elif operand == "%":
            expr = lval % rval
        elif operand == "**":
            expr = func.power(lval, rval)
        elif operand == "//":
            expr = func.floor(lval / rval)
        result_type = unify_inferred_types(lval_type, rval_type)
    return expr, result_type


# Helper function for arithmetic operators (+, -, *, /, %)
def _handle_arithmetic_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
):
    """
    Handles arithmetic operators ('+', '-', '*', '**', '//', '/', '%') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the arithmetic operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the arithmetic operator.
    """
    operand = filter_dict.get("operand")
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)
        return _join_subqueries(lhs, rhs, expr, result_type, session=session)
    elif lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)
        return (
            select(
                lhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
                literal(result_type).label("inferred_type"),
            )
            .select_from(lhs)
            .subquery()
        )
    elif rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)
        return (
            select(
                rhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
                literal(result_type).label("inferred_type"),
            )
            .select_from(rhs)
            .subquery()
        )
    else:
        # For direct expressions (not subqueries), we can't easily determine types
        # So we'll just use the standard SQLAlchemy operators and let PostgreSQL handle the casting
        if operand == "+":
            return lhs + rhs
        elif operand == "-":
            return lhs - rhs
        elif operand == "*":
            return lhs * rhs
        elif operand == "/":
            return lhs / rhs
        elif operand == "%":
            return lhs % rhs
        elif operand == "**":
            return func.power(lhs, rhs)
        elif operand == "//":
            return func.floor(lhs / rhs)


# Helper function for comparison operators (==, !=, <, >, <=, >=, is, is not)
def _handle_comparison_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
):
    """
    Handles comparison operators ('==', '!=', '<', '>', '<=', '>=', 'is', 'is not') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the comparison operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the comparison operator.
    """
    operand = filter_dict.get("operand")
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        lval = cast_expr(lval, lval_type, rval_type)
        rval = cast_expr(rval, rval_type, lval_type)
        if operand == "==":
            expr = lval == rval
        elif operand == "!=":
            expr = lval != rval
        elif operand == "<":
            expr = lval < rval
        elif operand == ">":
            expr = lval > rval
        elif operand == "<=":
            expr = lval <= rval
        elif operand == ">=":
            expr = lval >= rval
        elif operand == "is":
            expr = lval.is_(rval)
        elif operand == "is not":
            expr = lval.isnot(rval)
        return _join_subqueries(lhs, rhs, expr, "bool", session=session)
    elif lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        lval = cast_expr(lval, lval_type, rval_type)
        rhs = cast_expr(rhs, rval_type, lval_type)
        if operand == "==":
            expr = lval == rhs
        elif operand == "!=":
            expr = lval != rhs
        elif operand == "<":
            expr = lval < rhs
        elif operand == ">":
            expr = lval > rhs
        elif operand == "<=":
            expr = lval <= rhs
        elif operand == ">=":
            expr = lval >= rhs
        elif operand == "is":
            expr = (
                lval.is_(None)
                if rhs is None or isinstance(rhs, BindParameter) and rhs.value is None
                else lval == rhs
            )
        elif operand == "is not":
            expr = (
                lval.isnot(None)
                if rhs is None or isinstance(rhs, BindParameter) and rhs.value is None
                else lval != rhs
            )
        return (
            select(
                lhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
                literal("bool").label("inferred_type"),
            )
            .select_from(lhs)
            .subquery()
        )
    elif rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        rval = cast_expr(rval, rval_type, lval_type)
        lhs = cast_expr(lhs, lval_type, rval_type)
        if operand == "==":
            expr = lhs == rval
        elif operand == "!=":
            expr = lhs != rval
        elif operand == "<":
            expr = lhs < rval
        elif operand == ">":
            expr = lhs > rval
        elif operand == "<=":
            expr = lhs <= rval
        elif operand == ">=":
            expr = lhs >= rval
        elif operand == "is":
            expr = (
                lhs.is_(None)
                if rval is None
                or isinstance(rval, BindParameter)
                and rval.value is None
                else lhs == rval
            )
        elif operand == "is not":
            expr = (
                lhs.isnot(None)
                if rval is None
                or isinstance(rval, BindParameter)
                and rval.value is None
                else lhs != rval
            )
        return (
            select(
                rhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
                literal("bool").label("inferred_type"),
            )
            .select_from(rhs)
            .subquery()
        )
    else:
        if operand == "==":
            return lhs == rhs
        elif operand == "!=":
            return lhs != rhs
        elif operand == "<":
            return lhs < rhs
        elif operand == ">":
            return lhs > rhs
        elif operand == "<=":
            return lhs <= rhs
        elif operand == ">=":
            return lhs >= rhs
        elif operand == "is":
            return lhs.is_(rhs)
        elif operand == "is not":
            return lhs.isnot(rhs)


# Helper function for membership operators (in, not in)
def _handle_membership_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
):
    """
    Handles membership operators ('in', 'not in') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the membership operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the membership operator.
    """
    operand = filter_dict.get("operand")
    is_in = operand == "in"

    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    # Both sides are subqueries
    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)

        # Check if RHS is a JSONB list for containment check
        if rval_type == "list" and is_in:
            # Use PostgreSQL's @> operator for array containment
            condition = rval.op("@>")(func.jsonb_build_array(lval))
            expr = ~condition if not is_in else condition
        elif lval_type == "list" and is_in:
            # Use PostgreSQL's @> operator for array containment
            condition = lval.op("@>")(func.jsonb_build_array(rval))
            expr = ~condition if not is_in else condition
        else:
            # Fall back to substring check for non-list types
            condition = _substring_expr(lval, rval)
            expr = ~condition if not is_in else condition

        return _join_subqueries(lhs, rhs, expr, "bool", session=session)

    # Only LHS is a subquery
    elif lhs_is_sub and not rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)

        # Check if we're trying to do membership test on a boolean column
        if lval_type == "bool" and not isinstance(lval, list):
            raise HTTPException(
                status_code=400,
                detail="Invalid membership test on a boolean column. Use equality check (==) instead of 'in'.",
            )

        # Handle JSONB array containment for list columns
        if lval_type == "list":
            # If RHS is a BindParameter or literal, we can use the @> operator
            if isinstance(rhs, BindParameter) or not isinstance(
                rhs,
                (list, dict, Subquery),
            ):
                # Create a JSON array with the single value for the containment check
                rhs_value = rhs.value if isinstance(rhs, BindParameter) else rhs

                # Use PostgreSQL's @> operator for array containment
                containment_expr = lval.op("@>")(func.jsonb_build_array(rhs_value))
                expr = containment_expr if is_in else ~containment_expr
                return (
                    select(
                        lhs.c.log_event_id.label("log_event_id"),
                        expr.label("value"),
                        literal("bool").label("inferred_type"),
                    )
                    .select_from(lhs)
                    .subquery()
                )

        # Fall back to standard handling for non-array types
        rhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("rhs"), rhs)

        if rhs_list and isinstance(rhs_list, list):
            expr = lval.in_(rhs_list) if is_in else ~lval.in_(rhs_list)
        else:
            substring_cond = _substring_expr(lval, rhs)
            expr = substring_cond if is_in else ~substring_cond

        return (
            select(
                lhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
                literal("bool").label("inferred_type"),
            )
            .select_from(lhs)
            .subquery()
        )

    # Only RHS is a subquery
    elif rhs_is_sub and not lhs_is_sub:
        rval, rval_type = _select_value(rhs, session)

        # Check if we're trying to do membership test on a boolean column
        if rval_type == "bool" and not isinstance(rval, list):
            raise HTTPException(
                status_code=400,
                detail="Invalid membership test on a boolean column. Use equality check (==) instead of 'in'.",
            )

        # Handle the case where RHS is a JSONB array and LHS is a scalar value to check for containment
        if rval_type == "list":
            # If LHS is a scalar value (not a list or subquery), we can use the @> operator
            if not isinstance(lhs, (list, dict, Subquery)):
                lhs_value = lhs.value if isinstance(lhs, BindParameter) else lhs
                # TODO: this can be avoided with more robust parsing/tokenization (AST based)
                try:
                    lhs_value = json.loads(lhs_value)
                except:
                    pass

                # Use PostgreSQL's @> operator for array containment
                # Create a JSONB array with the single value for the containment check
                containment_expr = rval.op("@>")(func.jsonb_build_array(lhs_value))
                cond = containment_expr if is_in else ~containment_expr
                return (
                    select(
                        rhs.c.log_event_id.label("log_event_id"),
                        cond.label("value"),
                        literal("bool").label("inferred_type"),
                    )
                    .select_from(rhs)
                    .subquery()
                )

        lhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("lhs"), lhs)

        if lhs_list is not None and isinstance(lhs_list, list):
            cond = rval.in_(lhs_list) if is_in else ~rval.in_(lhs_list)
        else:
            # Substring check. We'll check: "lhs in str(rval)" => substring.
            substring_cond = _substring_expr(lhs, rval)
            cond = substring_cond if is_in else ~substring_cond

        return (
            select(
                rhs.c.log_event_id.label("log_event_id"),
                cond.label("value"),
                literal("bool").label("inferred_type"),
            )
            .select_from(rhs)
            .subquery()
        )

    # Neither side is a subquery
    else:
        rhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("rhs"), rhs)

        # If we successfully parse a list, do normal membership
        if rhs_list is not None and isinstance(rhs_list, list):
            return lhs.in_(rhs_list) if is_in else ~lhs.in_(rhs_list)

        # Otherwise do substring check
        substring_cond = _substring_expr(lhs, rhs)
        return substring_cond if is_in else ~substring_cond


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

    if isinstance(val, (list, dict)):
        return val

    return None


# Helper function for functions (len, str, type, round, round_timestamp, exists, version, isNone)
def _handle_date_function(rhs_expr, session):
    """
    Handles the date() function which extracts the date component from a datetime value.

    Args:
        rhs_expr: The expression to extract the date from (datetime or string)
        session: SQLAlchemy session for executing subqueries

    Returns:
        SQLAlchemy expression that extracts the date component
    """
    if isinstance(rhs_expr, Subquery):
        val, val_type = _select_value(rhs_expr, session)

        # Create a CASE expression to handle different input types
        expr = case(
            (
                val_type == "timestamp",
                func.cast(
                    func.date_trunc(
                        "day",
                        cast(cast(val, Text), DateTime(timezone=True)),
                    ),
                    Date,
                ),
            ),
            (val_type == "str", func.cast(cast(val, Text), Date)),
            else_=None,
        )

        return (
            select(
                rhs_expr.c.log_event_id.label("log_event_id"),
                expr.label("value"),
                literal("date").label("inferred_type"),
            )
            .select_from(rhs_expr)
            .subquery()
        )
    else:
        # Handle literal values
        if isinstance(rhs_expr, BindParameter):
            val = rhs_expr.value
            if isinstance(val, datetime):
                # Extract date from datetime
                return literal(val.date().isoformat(), type_=Date)
            elif isinstance(val, str):
                # Try to parse as datetime first
                try:
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                    return literal(dt.date().isoformat(), type_=Date)
                except ValueError:
                    # If it's already a date string, just pass it as is
                    if _is_date_string(val):
                        clean_val = val.strip("\"'")
                        return literal(clean_val, type_=Date)
                    else:
                        raise ValueError(
                            f"Cannot convert {val} to date. Expected datetime or date string.",
                        )
            else:
                raise ValueError(
                    f"Cannot convert {val} to date. Expected datetime or date string.",
                )
        else:
            # Try to cast the expression to Date
            return cast(rhs_expr, Date)


def _handle_functions(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
):
    """
    Handles function-based operations ('len', 'str', 'type', 'round', 'round_timestamp',
    'exists', 'version', 'isNone', 'time', 'date', 'now') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the function and its arguments.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the provided function.
    """
    operand = filter_dict.get("operand")
    no_arg_functions = ["now"]
    two_arg_functions = ["BASE", "round", "round_timestamp"]

    if operand in no_arg_functions:
        rhs_expr = None
    elif operand in two_arg_functions:
        rhs_expr = [
            build_sql_query(
                expr,
                log_event_alias,
                session,
                log_event_ids=log_event_ids,
                is_derived=is_derived,
            )
            for expr in filter_dict.get("rhs")
        ]
    else:
        # one_arg_functions
        rhs_expr = build_sql_query(
            filter_dict.get("rhs"),
            log_event_alias,
            session,
            log_event_ids=log_event_ids,
            is_derived=is_derived,
        )

    if operand == "len":
        rval, rval_type = _select_value(rhs_expr, session)
        if isinstance(rhs_expr, Subquery):
            expr = case(
                (
                    rval_type == "list",
                    func.jsonb_array_length(
                        cast(rval, JSONB),
                    ).cast(Float),
                ),
                (
                    rval_type == "dict",
                    select(func.count())
                    .select_from(
                        func.jsonb_object_keys(
                            cast(rval, JSONB),
                        ),
                    )
                    .scalar_subquery()
                    .cast(Float),
                ),
                (
                    rval_type == "str",
                    func.length(
                        func.replace(cast(rval, String), '"', ""),
                    ).cast(Float),
                ),
                else_=0,
            ).label("value")
            return (
                select(
                    rhs_expr.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                    literal("int").label("inferred_type"),
                )
                .select_from(rhs_expr)
                .subquery()
            )
        else:
            return len(rhs_expr)

    elif operand == "str":
        if isinstance(rhs_expr, Subquery):
            val, val_type = _select_value(rhs_expr, session)
            expr = func.cast(val, String)
            return (
                select(
                    rhs_expr.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                    literal("str").label("inferred_type"),
                )
                .select_from(rhs_expr)
                .subquery()
            )
        else:
            expr = rhs_expr[0] if isinstance(rhs_expr, list) else rhs_expr
            return cast(expr, String)

    elif operand == "round":
        # 1) Normalize the "rhs_expr" into a list of length 1 or 2
        if not isinstance(rhs_expr, list):
            rhs_expr = [rhs_expr]
        if len(rhs_expr) == 1:
            # round(val)
            val_expr = rhs_expr[0]
            if isinstance(val_expr, Subquery):
                # subquery => we retrieve the numeric column
                val_col, val_type = _select_value(val_expr, session)
                # produce a new subquery
                subq = (
                    select(
                        val_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_col, Numeric)).label("value"),
                        literal("int").label("inferred_type"),
                    )
                    .select_from(val_expr)
                    .subquery()
                )
                return subq
            else:
                # val_expr is a literal or a direct SQL expression
                return func.round(cast(val_col, Numeric))

        elif len(rhs_expr) == 2:
            # round(val, digits)
            val_expr, digits_expr = rhs_expr
            if isinstance(val_expr, Subquery) and isinstance(digits_expr, Subquery):
                val_col, val_type = _select_value(val_expr, session)
                dig_col = _select_value(digits_expr, session)
                subq = (
                    select(
                        val_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_col, Numeric), dig_col).label("value"),
                        literal("int").label("inferred_type"),
                    )
                    .select_from(val_expr)
                    .join(
                        digits_expr,
                        val_expr.c.log_event_id == digits_expr.c.log_event_id,
                    )
                    .subquery()
                )
            elif isinstance(val_expr, Subquery):
                val_col, val_type = _select_value(val_expr, session)
                # If digits_expr is literal or bind param, we can pass it directly:
                subq = (
                    select(
                        val_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_col, Numeric), digits_expr).label("value"),
                        literal("int").label("inferred_type"),
                    )
                    .select_from(val_expr)
                    .subquery()
                )
                return subq
            elif isinstance(digits_expr, Subquery):
                dig_col, dig_type = _select_value(digits_expr, session)
                # In that case, val_expr might be a literal
                subq = (
                    select(
                        digits_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_expr, Numeric), dig_col).label("value"),
                        literal("int").label("inferred_type"),
                    )
                    .select_from(digits_expr)
                    .subquery()
                )
                return subq
            else:
                # both val_expr and digits_expr are non-subquery expressions (literals or direct SQL)
                return func.round(cast(val_expr, Numeric), digits_expr)
        else:
            raise ValueError("round(...) expects 1 or 2 arguments.")
    elif operand == "round_timestamp":
        if len(rhs_expr) != 2:
            raise ValueError(
                "round_timestamp(...) expects exactly 2 arguments: (timestamp_expr, seconds_expr)",
            )

        ts_expr = rhs_expr[0]
        sec_expr = rhs_expr[1]

        ts_is_sub = isinstance(ts_expr, Subquery)
        sec_is_sub = isinstance(sec_expr, Subquery)

        def _pg_round_timestamp(ts_col, seconds_col):
            ts_text = cast(ts_col, String)
            ts_cast = cast(ts_text, TIMESTAMP)
            return func.to_timestamp(
                func.round(
                    func.extract("epoch", ts_cast) / seconds_col,
                )
                * seconds_col,
            )

        if ts_is_sub and sec_is_sub:
            ts_col, ts_type = _select_value(ts_expr, session)
            sec_col, sec_type = _select_value(sec_expr, session)

            # build a subquery that joins them on log_event_id
            subq = (
                select(
                    ts_expr.c.log_event_id.label("log_event_id"),
                    _pg_round_timestamp(ts_col, sec_col).label("value"),
                    literal("timestamp").label("inferred_type"),
                )
                .select_from(ts_expr)
                .join(sec_expr, ts_expr.c.log_event_id == sec_expr.c.log_event_id)
                .subquery()
            )

        elif ts_is_sub:
            ts_col, ts_type = _select_value(ts_expr, session)
            if isinstance(sec_expr, BindParameter) and isinstance(
                sec_expr.value,
                (int, float),
            ):
                subq = (
                    select(
                        ts_expr.c.log_event_id.label("log_event_id"),
                        _pg_round_timestamp(ts_col, sec_expr.value).label("value"),
                        literal("timestamp").label("inferred_type"),
                    )
                    .select_from(ts_expr)
                    .subquery()
                )
                return subq
            else:
                raise ValueError(
                    "round_timestamp() can't handle that form of seconds_expr (unless subquery).",
                )

        elif sec_is_sub:
            if isinstance(ts_expr, BindParameter) and isinstance(
                ts_expr.value,
                (datetime, str),
            ):
                ts_literal = literal(ts_expr.value, type_=TIMESTAMP)
                sec_col, sec_type = _select_value(sec_expr, session)

                subq = (
                    select(
                        sec_expr.c.log_event_id.label("log_event_id"),
                        _pg_round_timestamp(ts_literal, sec_expr.value).label("value"),
                        literal("timestamp").label("inferred_type"),
                    )
                    .select_from(sec_expr)
                    .subquery()
                )
                return subq
            else:
                raise ValueError(
                    "round_timestamp() can't handle that form of timestamp_expr (unless subquery).",
                )

        else:
            if not isinstance(ts_expr, BindParameter) and not isinstance(
                ts_expr,
                (datetime, str),
            ):
                raise ValueError(
                    "Expected a literal datetime or string for the timestamp.",
                )
            if not isinstance(sec_expr, BindParameter) and not isinstance(
                sec_expr,
                (int, float),
            ):
                raise ValueError(
                    "Expected an integer or float literal for the rounding seconds.",
                )

            ts_lit = literal(ts_expr.value, type_=TIMESTAMP)
            return _pg_round_timestamp(ts_lit, sec_expr.value)

    elif operand == "exists":
        if (
            isinstance(filter_dict.get("rhs"), dict)
            and filter_dict["rhs"].get("type") == "identifier"
        ):
            identifier = filter_dict["rhs"]["value"]
            subq = select(Log.id).filter(
                Log.log_event_id == log_event_alias.id,
                Log.key == identifier,
            )
            return subq.exists()
        else:
            raise ValueError(
                f"Invalid argument for 'exists' function: {filter_dict}",
            )

    elif operand == "version":
        if (
            isinstance(filter_dict.get("rhs"), dict)
            and filter_dict["rhs"].get("type") == "identifier"
        ):
            identifier = filter_dict["rhs"]["value"]
            version_subq = (
                select(
                    Log.log_event_id.label("log_event_id"),
                    Log.version.label("value"),
                    literal("int").label("inferred_type"),
                )
                .select_from(Log)
                .join(log_event_alias, Log.log_event_id == log_event_alias.id)
                .where(
                    Log.key == identifier,
                )
                .subquery()
            )
            return version_subq
        elif (
            isinstance(filter_dict.get("rhs"), dict)
            and filter_dict["rhs"].get("operand") == "BASE"
        ):
            base_args = filter_dict["rhs"].get("rhs", [])
            if len(base_args) != 2:
                raise ValueError(
                    "BASE(...) requires exactly 2 arguments: (event_id, key)",
                )

            event_ids = base_args[0]

            if base_args[1].get("type") == "identifier":
                identifier = base_args[1]["value"]
            else:
                raise ValueError(
                    f"Second argument to BASE must be an identifier, got: {base_args[1]}",
                )

            row_number = (
                func.row_number().over(order_by=Log.log_event_id).label("log_event_id")
            )
            version_subq = (
                select(
                    row_number.label("log_event_id"),
                    Log.version.label("value"),
                    literal("int").label("inferred_type"),
                )
                .select_from(Log)
                .where(
                    Log.log_event_id.in_(event_ids) if event_ids else True,
                    Log.key == identifier,
                )
                .subquery()
            )
            return version_subq
        else:
            raise ValueError(f"Invalid argument for 'version' function: {filter_dict}")

    elif operand == "BASE":
        if len(rhs_expr) != 2:
            raise ValueError("BASE(...) requires exactly 2 arguments: (event_id, key)")

        event_id_expr = rhs_expr[0]
        key_expr = rhs_expr[1]
        return _build_subquery_for_base_call(
            event_id_expr,
            key_expr,
            session,
            log_event_ids,
        )
    elif operand == "isNone":
        if isinstance(filter_dict.get("rhs"), dict):
            rhs_expr = build_sql_query(
                filter_dict.get("rhs"),
                log_event_alias,
                session,
                log_event_ids=log_event_ids,
                is_derived=is_derived,
            )
        else:
            rhs_expr = [
                build_sql_query(
                    expr,
                    log_event_alias,
                    session,
                    log_event_ids=log_event_ids,
                    is_derived=is_derived,
                )
                for expr in filter_dict.get("rhs")
            ]

        # If the rhs_expr is a Subquery, select its value and check is_(None)
        if isinstance(rhs_expr, Subquery):
            rval, rval_type = _select_value(rhs_expr, session)
            if rval is None:
                return None
            expr = rval.is_(None)
            return (
                select(
                    rhs_expr.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                    literal("bool").label("inferred_type"),
                )
                .select_from(rhs_expr)
                .subquery()
            )
        else:
            # For non-subquery cases, simply return the boolean expression
            return rhs_expr.is_(None)

    elif operand == "time":
        if isinstance(rhs_expr, Subquery):
            val, val_type = _select_value(rhs_expr, session)

            # Create a CASE expression to handle different input types
            expr = case(
                (
                    val_type == "timestamp",
                    func.cast(
                        func.date_trunc(
                            "microseconds",
                            cast(cast(val, Text), DateTime(timezone=True)),
                        ),
                        Time,
                    ),
                ),
                (val_type == "str", func.cast(cast(val, Text), Time)),
                (val_type == "time", func.cast(cast(val, Text), Time)),
                else_=None,
            )

            return (
                select(
                    rhs_expr.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                    literal("time").label("inferred_type"),
                )
                .select_from(rhs_expr)
                .subquery()
            )
        else:
            # Handle literal values
            if isinstance(rhs_expr, BindParameter):
                val = rhs_expr.value
                if isinstance(val, datetime):
                    # Extract time from datetime
                    return literal(val.time().isoformat(), type_=Time)
                elif isinstance(val, str) and _is_time_string(val):
                    # Parse time string - handle 12-hour format
                    clean_val = val.strip("\"'")
                    try:
                        # Try 12-hour format first
                        if " PM" in clean_val or " AM" in clean_val:
                            # Try different 12-hour formats
                            for fmt in ("%I:%M %p", "%I:%M:%S %p", "%I:%M:%S.%f %p"):
                                try:
                                    dt = datetime.strptime(clean_val, fmt)
                                    return literal(dt.time().isoformat(), type_=Time)
                                except ValueError:
                                    continue

                        # Try 24-hour formats
                        for fmt in ("%H:%M:%S", "%H:%M:%S.%f", "%H:%M"):
                            try:
                                dt = datetime.strptime(clean_val, fmt)
                                return literal(dt.time().isoformat(), type_=Time)
                            except ValueError:
                                continue

                        # If we can't parse it, just pass it as is
                        return literal(clean_val, type_=Time)
                    except Exception:
                        # If all parsing fails, just pass the string as is
                        return literal(clean_val, type_=Time)
                else:
                    raise ValueError(
                        f"Cannot convert {val} to time. Expected datetime or time string.",
                    )
            else:
                # Try to cast the expression to Time
                return cast(rhs_expr, Time)

    elif operand == "date":
        # Handle the date function: extract date component from a datetime
        return _handle_date_function(rhs_expr, session)
    elif operand == "now":
        # Handle the now function: return current timestamp with timezone
        # Create a subquery that returns the current timestamp for each log_event_id
        if log_event_ids is None or log_event_ids == []:
            # If no log_event_ids provided, return a literal timestamp
            return literal(datetime.now(timezone.utc).isoformat(), type_=TIMESTAMP)

        # Create a subquery with the current timestamp for each log_event_id
        if isinstance(log_event_ids, list):
            # For a list of IDs, create a subquery with those IDs
            ids_subq = select(
                literal(id).label("log_event_id") for id in log_event_ids
            ).subquery()
            now_subq = (
                select(
                    ids_subq.c.log_event_id.label("log_event_id"),
                    func.timezone("UTC", func.now()).label(
                        "value",
                    ),  # Use timezone-aware timestamp
                    literal("timestamp").label("inferred_type"),
                )
                .select_from(ids_subq)
                .subquery()
            )
        else:
            # For a subquery of IDs, use it directly
            ids_subq = log_event_ids
            row_number = (
                func.row_number().over(order_by=ids_subq.c.id).label("log_event_id")
            )
            # Return a subquery with current timestamp for each log_event_id
            event_id_col = row_number if is_derived else log_event_ids.c.id
            now_subq = (
                select(
                    event_id_col.label("log_event_id"),
                    func.timezone("UTC", func.now()).label(
                        "value",
                    ),  # Use timezone-aware timestamp
                    literal("timestamp").label("inferred_type"),
                )
                .select_from(ids_subq)
                .subquery()
            )
        return now_subq
    else:
        raise ValueError(f"Unknown function operand: {operand}")


def _handle_index_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
):
    """
    Handle the INDEX operator in a filter expression.

    Args:
        filter_dict (dict): The filter expression dictionary containing "lhs" and "rhs".
        log_event_alias: The alias for the log event.
        session: The database session.

    Returns:
        Subquery: A subquery that extracts the sub-value from the LHS JSON object/array using the RHS key/index.
    """
    lhs_node = filter_dict.get("lhs")
    rhs_node = filter_dict.get("rhs")

    lhs_expr = build_sql_query(
        lhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )
    rhs_expr = build_sql_query(
        rhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
    )

    if isinstance(lhs_expr, Subquery):
        input_type = session.execute(select(lhs_expr.c.inferred_type)).first()[0]
        is_collection = input_type in ["list", "dict"]
        lhs_valcol, lhs_type = _select_value(
            lhs_expr,
            session,
            is_collection=is_collection,
        )
        if isinstance(rhs_expr, Subquery):
            # Potentially advanced scenario: the user wrote x[y], where y is a subquery.
            # We'll pick the .value from y, interpret it as a string or integer, and then do -> or ->> extraction.
            rhs_valcol, rhs_type = _select_value(rhs_expr, session)
            # We must join them on log_event_id as well:
            subq = (
                select(
                    lhs_expr.c.log_event_id.label("log_event_id"),
                    # We'll do a JSON extraction. If rhs is a string => valcol->rhs.
                    # If integer => valcol->rhs, but we must cast integer to text for JSON operator in PG.
                    # The simplest approach is to do:
                    func.jsonb_extract_path(
                        lhs_valcol,
                        func.cast(rhs_valcol, String),
                    ).label("value"),
                    literal(rhs_type).label("inferred_type"),
                )
                .select_from(lhs_expr)
                .join(rhs_expr, lhs_expr.c.log_event_id == rhs_expr.c.log_event_id)
                .subquery()
            )
            return subq
        else:
            rhs_expr = (
                rhs_expr.value if isinstance(rhs_expr, BindParameter) else rhs_expr
            )
            if lhs_type == "str":
                # For strings, we need to use PostgreSQL's substring function
                # PostgreSQL is 1-indexed, so we need to adjust the index
                if isinstance(rhs_expr, int):
                    # Convert 0-indexed to 1-indexed for PostgreSQL
                    pg_index = rhs_expr + 1
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        literal(pg_index),
                        literal(1),
                    )
                elif isinstance(rhs_expr, BindParameter) and isinstance(
                    rhs_expr.value,
                    int,
                ):
                    # Convert 0-indexed to 1-indexed for PostgreSQL
                    pg_index = rhs_expr.value + 1
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        literal(pg_index),
                        literal(1),
                    )
                else:
                    # If it's not a simple integer index, try to cast it
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        cast(rhs_expr, Integer) + 1,
                        literal(1),
                    )
            # Standard JSONB indexing for non-string types
            elif isinstance(rhs_expr, int):
                extracted = lhs_valcol[rhs_expr]  # Postgres list indexing
            elif isinstance(rhs_expr, str):
                extracted = lhs_valcol[rhs_expr]  # Postgres dict indexing
            else:
                # fallback
                extracted = lhs_valcol[rhs_expr]

            # Build the subquery
            # TODO: add strong typing for lists/dicts to reason about the inferred_type when indexing.
            result = session.execute(select(extracted)).first()[0]
            inferred_type = LogDAO.infer_type("", result)
            subq = (
                select(
                    lhs_expr.c.log_event_id.label("log_event_id"),
                    extracted.label("value"),
                    literal(inferred_type).label("inferred_type"),
                )
                .select_from(lhs_expr)
                .subquery()
            )
            return subq

    else:
        # If LHS is not a subquery => e.g. LHS is a python dict or list literal
        if isinstance(lhs_expr, (dict, list)):
            # Then we do a python-level extraction if the rhs is also python-literal
            if isinstance(rhs_expr, (int, str)):
                # Just do dictionary or list indexing:
                try:
                    extracted_value = lhs_expr[rhs_expr]
                except (KeyError, IndexError, TypeError):
                    extracted_value = None
                return literal(extracted_value)
            else:
                raise ValueError(
                    "Cannot index a python dict/list with a subquery or complex expr.",
                )
        else:
            raise ValueError(
                "INDEX operator expects LHS to be a subquery (JSON) or a python list/dict literal.",
            )


def _build_subquery_for_base_call(
    list_of_ids_expr,
    key_expr,
    session,
    log_event_ids,
    is_derived=False,
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
    key_val, key_type = _select_value(key_expr, session)
    row_number = (
        func.row_number().over(order_by=key_expr.c.log_event_id).label("log_event_id")
    )
    filtered_subquery = (
        select(
            row_number,  # use sequential log_event_ids as 1,2,3, etc..
            key_val.label("value"),
            literal(key_type).label("inferred_type"),
        )
        .select_from(key_expr)
        .where(key_expr.c.log_event_id.in_(base_ids))
        .subquery()
    )
    return filtered_subquery


def build_sql_query(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
):
    """
    Recursively build SQLAlchemy filter or expression from filter_dict.

    Args:
        filter_dict (dict): The filter dictionary.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression
    """

    # Base cases
    if not isinstance(filter_dict, dict):
        return literal(filter_dict)

    if "type" in filter_dict:
        if filter_dict["type"] == "identifier":
            key = filter_dict["value"]
            if isinstance(log_event_ids, dict):
                event_ids = log_event_ids.get(key)
            else:
                event_ids = log_event_ids

            return _build_subquery_for_identifier(
                key,
                log_event_alias,
                alias=f"select_{key}",
                log_event_ids=event_ids,
                session=session,
                is_derived=is_derived,
            )
        elif filter_dict["type"] == "type_literal":
            return literal(filter_dict["value"])
        elif filter_dict["type"] in ("int", "float", "bool", "string", "other"):
            return literal(filter_dict["value"])
    operand = filter_dict.get("operand")

    # Handle logical operators (and, or, not)
    if operand in ("and", "or", "not"):
        return _handle_logical_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
        )

    # Handle arithmetic operators (+, -, *, /, %, **, //)
    elif operand in ("+", "-", "*", "/", "%", "**", "//"):
        return _handle_arithmetic_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
        )

    # Handle comparison operators (==, !=, <, >, <=, >=, is, is not)
    elif operand in ("==", "!=", "<", ">", "<=", ">=", "is", "is not"):
        return _handle_comparison_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
        )

    # Handle membership operators (in, not in)
    elif operand in ("in", "not in"):
        return _handle_membership_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
        )

    # Handle functions (len, str, type, round, round_timestamp, exists, version, isNone, time, date, now)
    elif operand in (
        "len",
        "str",
        "type",
        "round",
        "round_timestamp",
        "exists",
        "version",
        "BASE",
        "isNone",
        "time",
        "date",
        "now",
    ):
        return _handle_functions(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
        )

    # Handle list/dict indexing
    elif operand == "INDEX":
        return _handle_index_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
        )
    # Handle unknown operand
    else:
        raise ValueError(f"Unknown operand or structure: {filter_dict}")


# Reduction #
# ----------#


# noinspection PyBroadException
def _is_timestamp(v: Any):
    try:
        # First normalize the timestamp if it's in a non-standard format
        normalized = normalize_timestamp(v)
        # Then try to parse it
        datetime.fromisoformat(normalized)
        return True
    except:
        return False


def _is_type_for_len(v: Any) -> bool:
    return (
        (isinstance(v, str) and not _is_timestamp(v))
        or isinstance(v, list)
        or isinstance(v, dict)
        or isinstance(v, tuple)
        or isinstance(v, set)
    )


def _is_all_unique(vals):
    """
    Check if all entries in vals are unique. Works even for unhashable types like lists or dicts.
    """
    seen = []
    for val in vals:
        if val in seen:
            return False
        seen.append(val)
    return True


def _preprocess(
    values: List[Union[int, float, bool, str]],
) -> Tuple[List[Union[int, float, bool]], bool]:
    assert all(
        isinstance(x, type(values[0])) for x in values
    ), "Not all elements have the same type"
    if _is_type_for_len(values[0]):
        return [len(v) for v in values], False
    elif _is_timestamp(values[0]):
        return [datetime.fromisoformat(v).timestamp() for v in values], True
    else:
        return values, False


def _count(values: List[Union[int, float, bool]]) -> Union[int, float]:
    values, _ = _preprocess(values)
    return len(values)


def _sum(values: List[Union[int, float, bool]]) -> Union[int, float, str]:
    values, is_timestamp = _preprocess(values)
    ret = sum(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _mean(values: List[Union[int, float, bool]]) -> Union[float, str]:
    values, is_timestamp = _preprocess(values)
    ret = sum(values) / len(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _var(values: List[Union[int, float, bool]]) -> Union[float, str]:
    values, is_timestamp = _preprocess(values)
    num_values = len(values)
    mean = sum(values) / num_values
    diffs_squared = [(v - mean) ** 2 for v in values]
    ret = sum(diffs_squared) / num_values
    return timedelta(seconds=ret).__repr__() if is_timestamp else ret


def _std(values: List[Union[int, float, bool]]) -> Union[float, str]:
    values, is_timestamp = _preprocess(values)
    num_values = len(values)
    mean = sum(values) / num_values
    diffs_squared = [(v - mean) ** 2 for v in values]
    ret = (sum(diffs_squared) / num_values) ** 0.5
    return timedelta(seconds=ret).__repr__() if is_timestamp else ret


def _min(values: List[Union[int, float, bool]]) -> Union[int, float, bool, str]:
    values, is_timestamp = _preprocess(values)
    ret = min(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _max(values: List[Union[int, float, bool]]) -> Union[int, float, bool, str]:
    values, is_timestamp = _preprocess(values)
    ret = max(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _median(values: List[Union[int, float, bool]]) -> Union[int, float, bool, str]:
    values, is_timestamp = _preprocess(values)
    ret = statistics.median(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _mode(values: List[Union[int, float, bool]]) -> Union[int, float, bool, str]:
    values, is_timestamp = _preprocess(values)
    ret = statistics.mode(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


reduction_methods = {
    "count": _count,
    "sum": _sum,
    "mean": _mean,
    "var": _var,
    "std": _std,
    "min": _min,
    "max": _max,
    "median": _median,
    "mode": _mode,
}


def compute_group_aggregate(
    session,
    log_event_ids: List[int],
    group_field: str,
    value_field: str,
    aggregation_metric: str,
    log_event_alias,
) -> Dict[Any, float]:
    """
    Compute aggregated values for a given group using SQLAlchemy.

    Args:
        session: SQLAlchemy session
        log_event_ids: List of log event IDs to process
        group_field: Field name to group by
        value_field: Field name to aggregate
        aggregation_metric: Metric to use for aggregation (e.g., 'mean', 'sum', 'min', etc.)
        log_event_alias: Alias for LogEvent to correlate subqueries

    Returns:
        Dict mapping group values to their aggregated values
    """
    # Build subqueries for group and value fields
    group_subq = _build_subquery_for_identifier(
        group_field,
        log_event_alias,
        log_event_ids=log_event_ids,
        session=session,
    )
    value_subq = _build_subquery_for_identifier(
        value_field,
        log_event_alias,
        log_event_ids=log_event_ids,
        session=session,
    )

    # Get the value columns and their types
    group_val, group_type = _select_value(group_subq, session)
    value_val, value_type = _select_value(value_subq, session)

    # Cast value column to float for aggregation
    value_col = cast(value_val, Float)

    # Define the aggregation function based on the metric
    if aggregation_metric == "mean":
        agg_func = func.avg(value_col)
    elif aggregation_metric == "sum":
        agg_func = func.sum(value_col)
    elif aggregation_metric == "min":
        agg_func = func.min(value_col)
    elif aggregation_metric == "max":
        agg_func = func.max(value_col)
    elif aggregation_metric == "count":
        agg_func = func.count(value_col)
    elif aggregation_metric == "std":
        # Standard deviation using PostgreSQL's stddev function
        agg_func = func.stddev(value_col)
    elif aggregation_metric == "var":
        # Variance using PostgreSQL's var_pop function
        agg_func = func.var_pop(value_col)
    else:
        raise ValueError(f"Unsupported aggregation metric: {aggregation_metric}")


######################
# Formatting functions
######################


def _flatten_fields(
    log_fields: list,
):
    flattened = dict()
    for log_ids, fields in log_fields:
        log_ids = log_ids if isinstance(log_ids, list) else [log_ids]
        fields = fields if isinstance(fields, list) else [fields]
        for log_id in log_ids:
            if log_id not in flattened:
                flattened[log_id] = list()
            for field in fields:
                if field is not None and field not in flattened[log_id]:
                    flattened[log_id].append(field)
    return flattened


def is_image_field(field_name: str, field_types: dict) -> bool:
    """Check if a field is an image type."""
    return field_types.get(field_name) == "image"


def _format_flat_logs(rows, context_len, value_limit, field_order_map):
    """Helper function to format flat logs using raw query data"""
    formatted = {}

    for (
        row_key,
        row_value,
        row_inferred_type,
        row_param_version,
        row_context_version,
        row_source_type,
        row_created_at,
        row_event_id,
    ) in rows:

        if row_event_id not in formatted:
            formatted[row_event_id] = {
                "ts": row_created_at.isoformat() if row_created_at else None,
                "clipped_fields": [],
                "entries": {},
                "versions": {},
                "context_versions": {},
                "derived_entries": {},
            }

        is_derived = row_source_type == "derived"

        # Apply context_len slicing to the key
        key = row_key[context_len:]

        def _limit_value(value: any, inferred_type: str) -> tuple:
            """Limit the size of a value based on its type and the value_limit parameter.
            Returns a tuple of (limited_value, is_clipped)."""
            if value_limit is None:
                return value, False

            # Handle numeric values - return as is
            if inferred_type in ["int", "float", "bool"]:
                return value, False

            if inferred_type == "image":
                return "", True

            if inferred_type in ["list", "dict", "tuple"]:
                str_value = str(value)
                if len(str_value) > value_limit:
                    return str_value[:value_limit] + "...", True
                return str_value, False

            # Handle string values
            if inferred_type == "str":
                if len(str(value)) > value_limit:
                    return str(value)[:value_limit] + "...", True
                return value, False

            # Default case - treat as string
            str_value = str(value)
            if len(str_value) > value_limit:
                return str_value[:value_limit] + "...", True
            return str_value, False

        # Apply value limiting and get clipped status
        limited_val, is_clipped = _limit_value(row_value, row_inferred_type)
        if is_clipped:
            formatted[row_event_id]["clipped_fields"].append(key)

        if is_derived:
            formatted[row_event_id]["derived_entries"][key] = limited_val
        else:
            if row_param_version is not None:
                # param-based version
                if key not in formatted[row_event_id]["versions"]:
                    formatted[row_event_id]["versions"][key] = {}
                formatted[row_event_id]["versions"][key][
                    row_param_version
                ] = limited_val
                formatted[row_event_id]["entries"][key] = str(row_param_version)

            elif row_context_version is not None:
                # context-based version
                if key not in formatted[row_event_id]["context_versions"]:
                    formatted[row_event_id]["context_versions"][key] = {}
                formatted[row_event_id]["context_versions"][key][
                    row_context_version
                ] = limited_val
                if key not in formatted[row_event_id]["entries"]:
                    formatted[row_event_id]["entries"][key] = limited_val

            else:
                # entries
                formatted[row_event_id]["entries"][key] = limited_val

    # Now build final JSON
    logs_out = []
    params_out = {}
    for event_id, data in formatted.items():
        entries = {}
        params = {}
        for k, v in data["entries"].items():
            if k in data["versions"]:
                # It's param-based
                params[k] = v  # v is the str(ver)
                # Also store in params_out if needed
                if k not in params_out:
                    params_out[k] = {}
                # We might have multiple versions for the same param
                for ver_num, ver_val in data["versions"][k].items():
                    params_out[k][ver_num] = ver_val
            else:
                # It's a normal base entry
                entries[k] = v

        # derived_entries
        derived_entries = data["derived_entries"]

        # Sort all dictionaries according to field_type order
        sorted_entries = dict(
            sorted(
                entries.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        sorted_params = dict(
            sorted(
                params.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        sorted_derived = dict(
            sorted(
                derived_entries.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        # sort keys which are strings by descending order
        sorted_context_versions = {
            field: dict(sorted(versions.items(), key=lambda x: x[0], reverse=True))
            for field, versions in data["context_versions"].items()
        }
        logs_out.append(
            {
                "id": event_id,
                "ts": data["ts"],
                "entries": sorted_entries,
                "params": sorted_params,
                "derived_entries": sorted_derived,
                "versions": sorted_context_versions,
                "clipped_fields": data.get("clipped_fields", []),
            },
        )

    return logs_out, params_out


def _get_final_logs(session, filtered_logs_subq, paginated_ids_subq):
    """
    Returns final rows with the JSONLog value (if available) restored.
    """
    # Outer join JSONLog and JSONLogHistory based on source_type
    final_logs_query = (
        session.query(
            filtered_logs_subq.c.id,
            filtered_logs_subq.c.log_event_id,
            filtered_logs_subq.c.key,
            # Use coalesce to select the appropriate JSON value based on source_type
            func.coalesce(
                case(
                    (
                        filtered_logs_subq.c.source_type == "history",
                        JSONLogHistory.value,
                    ),
                    else_=JSONLog.value,
                ),
                cast(filtered_logs_subq.c.value, JSON),
            ).label("value"),
            filtered_logs_subq.c.inferred_type,
            filtered_logs_subq.c.param_version,
            filtered_logs_subq.c.context_version,
            filtered_logs_subq.c.created_at,
            filtered_logs_subq.c.source_type,
        )
        .outerjoin(
            JSONLog,
            and_(
                JSONLog.log_event_id == filtered_logs_subq.c.log_event_id,
                JSONLog.key == filtered_logs_subq.c.key,
                filtered_logs_subq.c.source_type != "history",
            ),
        )
        .outerjoin(
            JSONLogHistory,
            and_(
                JSONLogHistory.log_event_id == filtered_logs_subq.c.log_event_id,
                JSONLogHistory.key == filtered_logs_subq.c.key,
                JSONLogHistory.version == filtered_logs_subq.c.context_version,
                filtered_logs_subq.c.source_type == "history",
            ),
        )
        .join(
            paginated_ids_subq,
            paginated_ids_subq.c.log_event_id == filtered_logs_subq.c.log_event_id,
        )
        .order_by(paginated_ids_subq.c.row_num, filtered_logs_subq.c.created_at)
    )
    return final_logs_query.all()

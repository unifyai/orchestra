import ast
import copy
import json
import random
import re
from datetime import datetime, timezone

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
    lateral,
    literal,
    literal_column,
    not_,
    or_,
    select,
    true,
    union_all,
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
    try:
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

    If both subqueries have a __comp_idx__ column (used in comprehensions),
    the join condition will also include matching on __comp_idx__ to prevent
    duplicate rows, and the output will preserve the __comp_idx__ column.
    """
    # Get the value columns for both sides
    lhs_val, lhs_type = _select_value(lhs_subq, session)
    rhs_val, rhs_type = _select_value(rhs_subq, session)

    # Check if both sides have __comp_idx__ (used in comprehensions)
    has_idx_lhs = hasattr(lhs_subq.c, "__comp_idx__")
    has_idx_rhs = hasattr(rhs_subq.c, "__comp_idx__")

    # Build the join condition
    join_cond = lhs_subq.c.log_event_id == rhs_subq.c.log_event_id
    if has_idx_lhs and has_idx_rhs:
        join_cond = and_(join_cond, lhs_subq.c.__comp_idx__ == rhs_subq.c.__comp_idx__)

    # Build the select columns
    select_cols = [
        func.coalesce(lhs_subq.c.log_event_id, rhs_subq.c.log_event_id).label(
            "log_event_id",
        ),
    ]

    # Include __comp_idx__ in the output if it exists
    if has_idx_lhs and has_idx_rhs:
        select_cols.append(
            func.coalesce(lhs_subq.c.__comp_idx__, rhs_subq.c.__comp_idx__).label(
                "__comp_idx__",
            ),
        )
    elif has_idx_lhs:
        select_cols.append(lhs_subq.c.__comp_idx__.label("__comp_idx__"))
    elif has_idx_rhs:
        select_cols.append(rhs_subq.c.__comp_idx__.label("__comp_idx__"))

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

    j = (
        select(*select_cols)
        .select_from(lhs_subq)
        .outerjoin(rhs_subq, join_cond)
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
    local_scope=None,
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
            local_scope=local_scope,
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
        local_scope=local_scope,
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
    local_scope=None,
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
        local_scope=local_scope,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
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
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        select_cols.extend(
            [expr.label("value"), literal(result_type).label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()
    elif rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        select_cols.extend(
            [expr.label("value"), literal(result_type).label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()
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
    local_scope=None,
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
        local_scope=local_scope,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
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
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()
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
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()
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
    local_scope=None,
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
        local_scope=local_scope,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
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
                select_cols = [lhs.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in lhs.c.keys():
                    select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [expr.label("value"), literal("bool").label("inferred_type")],
                )
                return select(*select_cols).select_from(lhs).subquery()

        # Fall back to standard handling for non-array types
        rhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("rhs"), rhs)

        if rhs_list and isinstance(rhs_list, list):
            expr = lval.in_(rhs_list) if is_in else ~lval.in_(rhs_list)
        else:
            substring_cond = _substring_expr(lval, rhs)
            expr = substring_cond if is_in else ~substring_cond

        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()

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
                select_cols = [rhs.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in rhs.c.keys():
                    select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [cond.label("value"), literal("bool").label("inferred_type")],
                )
                return select(*select_cols).select_from(rhs).subquery()

        lhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("lhs"), lhs)

        if lhs_list is not None and isinstance(lhs_list, list):
            cond = rval.in_(lhs_list) if is_in else ~rval.in_(lhs_list)
        else:
            # Substring check. We'll check: "lhs in str(rval)" => substring.
            substring_cond = _substring_expr(lhs, rval)
            cond = substring_cond if is_in else ~substring_cond

        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        select_cols.extend(
            [cond.label("value"), literal("bool").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()

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
    local_scope=None,
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
                local_scope=local_scope,
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
            local_scope=local_scope,
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
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            select_cols.extend(
                [expr.label("value"), literal("int").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
        else:
            return len(rhs_expr)

    elif operand == "str":
        if isinstance(rhs_expr, Subquery):
            val, val_type = _select_value(rhs_expr, session)
            expr = func.cast(val, String)
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            select_cols.extend(
                [expr.label("value"), literal("str").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
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
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        func.round(cast(val_col, Numeric)).label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(val_expr).subquery()
            else:
                # val_expr is a literal or a direct SQL expression
                return func.round(cast(val_expr, Numeric))

        elif len(rhs_expr) == 2:
            # round(val, digits)
            val_expr, digits_expr = rhs_expr
            if isinstance(val_expr, Subquery) and isinstance(digits_expr, Subquery):
                val_col, val_type = _select_value(val_expr, session)
                dig_col = _select_value(digits_expr, session)
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        func.round(cast(val_col, Numeric), dig_col).label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return (
                    select(*select_cols)
                    .select_from(val_expr)
                    .join(
                        digits_expr,
                        val_expr.c.log_event_id == digits_expr.c.log_event_id,
                    )
                    .subquery()
                )
            elif isinstance(val_expr, Subquery):
                val_col, val_type = _select_value(val_expr, session)
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        func.round(cast(val_col, Numeric), digits_expr).label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(val_expr).subquery()
            elif isinstance(digits_expr, Subquery):
                val_col, val_type = _select_value(val_expr, session)
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        func.round(cast(val_col, Numeric), digits_expr).label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(val_expr).subquery()
            elif isinstance(digits_expr, Subquery):
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        func.round(cast(val_col, Numeric), digits_expr).label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(val_expr).subquery()
            elif isinstance(digits_expr, Subquery):
                dig_col, dig_type = _select_value(digits_expr, session)
                # In that case, val_expr might be a literal
                select_cols = [digits_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in digits_expr.c.keys():
                    select_cols.append(digits_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        func.round(cast(val_expr, Numeric), dig_col).label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(digits_expr).subquery()
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

            select_cols = [ts_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in ts_expr.c.keys():
                select_cols.append(ts_expr.c.__comp_idx__.label("__comp_idx__"))
            select_cols.extend(
                [
                    _pg_round_timestamp(ts_col, sec_col).label("value"),
                    literal("timestamp").label("inferred_type"),
                ],
            )
            return (
                select(*select_cols)
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
                select_cols = [ts_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in ts_expr.c.keys():
                    select_cols.append(ts_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        _pg_round_timestamp(ts_col, sec_expr.value).label("value"),
                        literal("timestamp").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(ts_expr).subquery()
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

                select_cols = [sec_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in sec_expr.c.keys():
                    select_cols.append(sec_expr.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [
                        _pg_round_timestamp(ts_literal, sec_expr.value).label("value"),
                        literal("timestamp").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(sec_expr).subquery()
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
                local_scope=local_scope,
            )
        else:
            rhs_expr = [
                build_sql_query(
                    expr,
                    log_event_alias,
                    session,
                    log_event_ids=log_event_ids,
                    is_derived=is_derived,
                    local_scope=local_scope,
                )
                for expr in filter_dict.get("rhs")
            ]

        # If the rhs_expr is a Subquery, select its value and check is_(None)
        if isinstance(rhs_expr, Subquery):
            rval, rval_type = _select_value(rhs_expr, session)
            if rval is None:
                return None
            expr = rval.is_(None)
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            select_cols.extend(
                [expr.label("value"), literal("bool").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
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
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            select_cols.extend(
                [expr.label("value"), literal("time").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
        else:
            if isinstance(rhs_expr, BindParameter):
                val = rhs_expr.value
                if isinstance(val, datetime):
                    return literal(val.time().isoformat(), type_=Time)
                elif isinstance(val, str) and _is_time_string(val):
                    clean_val = val.strip("\"'")
                    try:
                        if " PM" in clean_val or " AM" in clean_val:
                            for fmt in ("%I:%M %p", "%I:%M:%S %p", "%I:%M:%S.%f %p"):
                                try:
                                    dt = datetime.strptime(clean_val, fmt)
                                    return literal(dt.time().isoformat(), type_=Time)
                                except ValueError:
                                    continue
                        for fmt in ("%H:%M:%S", "%H:%M:%S.%f", "%H:%M"):
                            try:
                                dt = datetime.strptime(clean_val, fmt)
                                return literal(dt.time().isoformat(), type_=Time)
                            except ValueError:
                                continue
                        return literal(clean_val, type_=Time)
                    except Exception:
                        return literal(clean_val, type_=Time)
                else:
                    raise ValueError(
                        f"Cannot convert {val} to time. Expected datetime or time string.",
                    )
            else:
                return cast(rhs_expr, Time)

    elif operand == "date":
        return _handle_date_function(rhs_expr, session)
    elif operand == "now":
        if log_event_ids is None or log_event_ids == []:
            return literal(datetime.now(timezone.utc).isoformat(), type_=TIMESTAMP)

        if isinstance(log_event_ids, list):
            ids_subq = select(
                literal(id).label("log_event_id") for id in log_event_ids
            ).subquery()
            now_subq = (
                select(
                    ids_subq.c.log_event_id.label("log_event_id"),
                    func.timezone("UTC", func.now()).label("value"),
                    literal("timestamp").label("inferred_type"),
                )
                .select_from(ids_subq)
                .subquery()
            )
        else:
            ids_subq = log_event_ids
            row_number = (
                func.row_number().over(order_by=ids_subq.c.id).label("log_event_id")
            )
            event_id_col = row_number if is_derived else log_event_ids.c.id
            now_subq = (
                select(
                    event_id_col.label("log_event_id"),
                    func.timezone("UTC", func.now()).label("value"),
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
    local_scope=None,
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
        local_scope=local_scope,
    )
    rhs_expr = build_sql_query(
        rhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
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
            rhs_valcol, rhs_type = _select_value(rhs_expr, session)
            select_cols = [lhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__comp_idx__.label("__comp_idx__"))
            select_cols.extend(
                [
                    func.jsonb_extract_path(
                        lhs_valcol,
                        func.cast(rhs_valcol, String),
                    ).label("value"),
                    literal(rhs_type).label("inferred_type"),
                ],
            )
            return (
                select(*select_cols)
                .select_from(lhs_expr)
                .join(rhs_expr, lhs_expr.c.log_event_id == rhs_expr.c.log_event_id)
                .subquery()
            )
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

            result = session.execute(select(extracted)).first()[0]
            inferred_type = LogDAO.infer_type("", result)
            select_cols = [lhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__comp_idx__.label("__comp_idx__"))
            select_cols.extend(
                [
                    extracted.label("value"),
                    literal(inferred_type).label("inferred_type"),
                ],
            )
            return select(*select_cols).select_from(lhs_expr).subquery()

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
            row_number,
            key_val.label("value"),
            literal(key_type).label("inferred_type"),
        )
        .select_from(key_expr)
        .where(key_expr.c.log_event_id.in_(base_ids))
        .subquery()
    )
    return filtered_subquery


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


def _handle_dict_method(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    method = filter_dict["method"]  # e.g., "keys", "values", "items"
    src = build_sql_query(
        filter_dict["rhs"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )
    if not isinstance(src, Subquery):
        raise HTTPException(
            status_code=400,
            detail="dict.keys/values/items only valid on JSONB column",
        )
    # Extract JSONB column and use lateral join
    val, _ = _select_value(src, session, is_collection=True)

    # Ensure we're working with a JSON object, not an array or scalar
    is_object = func.jsonb_typeof(val) == "object"

    # Use a CASE expression to handle non-object values safely
    safe_val = case((is_object, val), else_=literal("{}", type_=JSONB))

    each = lateral(func.jsonb_each(safe_val).table_valued("key", "value")).alias(
        "each_values",
    )
    base = (
        select(src.c.log_event_id, each.c.key, each.c.value)
        .select_from(src.join(each, true()))
        .subquery(name="base")
    )

    if method == "keys":
        agg = func.coalesce(
            func.jsonb_agg(base.c.key),
            literal("[]", type_=JSONB),
        )
        inf = "list"
    elif method == "values":
        agg = func.coalesce(
            func.jsonb_agg(base.c.value),
            literal("[]", type_=JSONB),
        )
        inf = "list"
    else:  # items
        agg = func.coalesce(
            func.jsonb_agg(
                func.jsonb_build_array(
                    base.c.key,
                    base.c.value,
                ),
            ),
            literal("[]", type_=JSONB),
        )
        inf = "list"

    final = (
        select(
            base.c.log_event_id,
            func.coalesce(agg, literal("[]", type_=JSONB)).label("value"),
            literal(inf).label("inferred_type"),
        )
        .group_by(base.c.log_event_id)
        .subquery(name=f"dict_{method}_subquery")
    )
    return final


def _handle_if_expr(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    """
    Handle conditional expressions (ternary if-else) in filter queries.

    This function processes expressions like 'x if condition else y' by evaluating
    the condition and then selecting either the 'then' or 'else' branch accordingly.
    """

    def _inflate_scalar_or_subquery(
        value,
        inferred_type,
        ids_subq,
        has_comp_idx=False,
        local_scope=None,
    ):
        """
        Given a scalar (possibly from a Python literal or BindParameter),
        or an identifier subquery, produce a subquery of the form:

            SELECT
                ids_subq.log_event_id,
                [ids_subq.__comp_idx__],
                :value AS value,
                :type  AS inferred_type
            FROM ids_subq

        so we can join on (log_event_id, __comp_idx__) if needed.
        """
        if isinstance(value, Subquery):
            cols = [value.c.log_event_id]
            if hasattr(ids_subq.c, "__comp_idx__"):
                cols.append(ids_subq.c.__comp_idx__)
            elif local_scope and "__comp_idx__" in local_scope:
                # fallback to local_scope column if we want to replicate indexing
                idx_col = local_scope["__comp_idx__"][0]
                cols.append(idx_col.label("__comp_idx__"))

            val, inf = _select_value(value, session)
            cols.append(val.label("value"))
            cols.append(literal(inf).label("inferred_type"))
            return (
                select(*cols)
                .select_from(value)
                .subquery(
                    name=f"__inflate_select_subq_{value.name}",
                )
            )

        # 1) Unwrap if it's a BindParameter
        if isinstance(value, BindParameter):
            value = value.value

        # 2) Start building a list of columns for select(...)
        cols = [ids_subq.c.log_event_id]

        # 3) If we are in a comprehension, attach ordinality if available
        if has_comp_idx:
            if hasattr(ids_subq.c, "__comp_idx__"):
                cols.append(ids_subq.c.__comp_idx__)
            elif local_scope and "__comp_idx__" in local_scope:
                # fallback to local_scope column if we want to replicate indexing
                idx_col = local_scope["__comp_idx__"][0]
                cols.append(idx_col.label("__comp_idx__"))
            # else do nothing / or handle as an error if you must

        # 4) Add the scalar columns
        cols.append(literal(value).label("value"))
        cols.append(literal(inferred_type).label("inferred_type"))

        # 5) Make a SELECT statement from those columns
        return (
            select(*cols)
            .select_from(ids_subq)
            .subquery(name=f"__inflate_scalar_subq_{value}")
        )

    # Check if we're in a comprehension context (local_scope has __comp_idx__)
    in_comprehension = local_scope is not None and "__comp_idx__" in local_scope

    # Build SQL queries for test, body, and else expressions
    raw_test = build_sql_query(
        filter_dict["test"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    raw_body = build_sql_query(
        filter_dict["body"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    raw_else = build_sql_query(
        filter_dict["orelse"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )

    # Collect all log_event_ids from subqueries
    id_selects = []
    for part in (raw_test, raw_body, raw_else):
        if isinstance(part, Subquery):
            # Include __comp_idx__ in the selection if it exists
            if in_comprehension and hasattr(part.c, "__comp_idx__"):
                id_selects.append(select(part.c.log_event_id, part.c.__comp_idx__))
            else:
                id_selects.append(select(part.c.log_event_id))

    # Create a subquery with all unique log_event_ids (and __comp_idx__ if in comprehension)
    if id_selects:
        if in_comprehension and any(len(s.selected_columns) > 1 for s in id_selects):
            # If any select has __comp_idx__, we need to handle it specially
            # First, standardize all selects to have both log_event_id and __comp_idx__
            standardized_selects = []
            for s in id_selects:
                if len(s.selected_columns) == 1:  # Only has log_event_id
                    # Add a NULL __comp_idx__ column
                    standardized_selects.append(
                        select(
                            s.selected_columns[0],
                            literal(None).label("__comp_idx__"),
                        ),
                    )
                else:  # Has both log_event_id and __comp_idx__
                    standardized_selects.append(s)
            # Union all standardized selects and get distinct rows
            ids_subq = (
                union_all(*standardized_selects)
                .subquery(name="union_all_standardized_selects")
                .select()
                .distinct()
                .subquery(name="ids_subq")
            )
        else:
            # Simple case: just union all log_event_ids
            ids_subq = (
                union_all(*id_selects)
                .subquery(name="union_all_id_selects")
                .select()
                .distinct()
                .subquery(name="ids_subq")
            )
    else:
        # No subqueries, fall back to the ids the caller gave us
        if isinstance(log_event_ids, Subquery):
            ids_subq = select(log_event_ids.c.id.label("log_event_id")).subquery()
        elif isinstance(log_event_ids, (list, tuple)):
            ids_subq = select(
                literal(id_).label("log_event_id") for id_ in log_event_ids
            ).subquery()
        else:  # None → whole table
            ids_subq = select(log_event_alias.id.label("log_event_id")).subquery()

        # If we're in a comprehension, add the __comp_idx__ column from local_scope
        if in_comprehension:
            comp_idx_col = local_scope["__comp_idx__"][0]
            ids_subq = (
                select(ids_subq.c.log_event_id, comp_idx_col.label("__comp_idx__"))
                .select_from(ids_subq)
                .subquery()
            )

    # Convert non-subquery expressions to subqueries
    if not isinstance(raw_test, Subquery) or (
        isinstance(raw_test, Subquery) and "value" not in raw_test.columns
    ):
        raw_test = _inflate_scalar_or_subquery(
            raw_test,
            "bool"
            if not isinstance(raw_test, BindParameter)
            else LogDAO.infer_type("", raw_test.value),
            ids_subq,
            in_comprehension,
        )

    if not isinstance(raw_body, Subquery) or (
        isinstance(raw_body, Subquery) and "value" not in raw_body.columns
    ):
        raw_body = _inflate_scalar_or_subquery(
            raw_body,
            LogDAO.infer_type(
                "",
                raw_body if not isinstance(raw_body, BindParameter) else raw_body.value,
            ),
            ids_subq,
            in_comprehension,
        )

    if not isinstance(raw_else, Subquery) or (
        isinstance(raw_else, Subquery) and "value" not in raw_else.columns
    ):
        raw_else = _inflate_scalar_or_subquery(
            raw_else,
            LogDAO.infer_type(
                "",
                raw_else if not isinstance(raw_else, BindParameter) else raw_else.value,
            ),
            ids_subq,
            in_comprehension,
        )

    # Get the inferred types for body and else expressions
    body_type = session.execute(select(raw_body.c.inferred_type)).scalar()
    else_type = session.execute(select(raw_else.c.inferred_type)).scalar()
    res_type = unify_inferred_types(body_type, else_type)

    # Cast values to the unified type
    body_val = cast_expr(raw_body.c.value, body_type, res_type)
    else_val = cast_expr(raw_else.c.value, else_type, res_type)
    test_val = cast_expr(raw_test.c.value, "bool", "bool")

    # Create the CASE expression for the if-else logic
    case_expr = case((test_val, body_val), else_=else_val)

    # Build the join conditions
    join_conditions = []

    # Always join on log_event_id
    test_join_cond = ids_subq.c.log_event_id == raw_test.c.log_event_id
    body_join_cond = ids_subq.c.log_event_id == raw_body.c.log_event_id
    else_join_cond = ids_subq.c.log_event_id == raw_else.c.log_event_id

    # If in comprehension context, also join on __comp_idx__
    if in_comprehension:
        if hasattr(ids_subq.c, "__comp_idx__") and hasattr(raw_test.c, "__comp_idx__"):
            test_join_cond = and_(
                test_join_cond,
                ids_subq.c.__comp_idx__ == raw_test.c.__comp_idx__,
            )

        if hasattr(ids_subq.c, "__comp_idx__") and hasattr(raw_body.c, "__comp_idx__"):
            body_join_cond = and_(
                body_join_cond,
                ids_subq.c.__comp_idx__ == raw_body.c.__comp_idx__,
            )

        if hasattr(ids_subq.c, "__comp_idx__") and hasattr(raw_else.c, "__comp_idx__"):
            else_join_cond = and_(
                else_join_cond,
                ids_subq.c.__comp_idx__ == raw_else.c.__comp_idx__,
            )

    # Create the final subquery
    select_cols = [ids_subq.c.log_event_id]
    if in_comprehension and hasattr(ids_subq.c, "__comp_idx__"):
        select_cols.append(ids_subq.c.__comp_idx__)

    select_cols.extend(
        [case_expr.label("value"), literal(res_type).label("inferred_type")],
    )

    final_subq = (
        select(*select_cols)
        .select_from(
            ids_subq.join(raw_test, test_join_cond)
            .outerjoin(raw_body, body_join_cond)
            .outerjoin(raw_else, else_join_cond),
        )
        .subquery(name="final_subq")
    )

    return final_subq


def _handle_list_comp(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    """
    Handle list comprehension expressions in filter queries.

    This function processes expressions like [x*2 for x in some_list if x > 0]
    by exploding the source list into rows, then applying the transformation and
    filter to each element, and finally aggregating back into a list.
    """
    iter_subq = build_sql_query(
        filter_dict["iter"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    if not isinstance(iter_subq, Subquery):
        raise HTTPException(
            status_code=400,
            detail="list comprehension source must be a JSONB collection",
        )

    val, _ = _select_value(iter_subq, session, is_collection=True)

    # Determine if we're iterating over an array or object
    is_array = session.execute(select(func.jsonb_typeof(val))).scalar() == "array"

    # Use appropriate function based on the JSON type
    if is_array:
        # For arrays, use jsonb_array_elements with ordinality
        elem_tbl = (
            func.jsonb_array_elements(val)
            .table_valued("value", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )
    else:
        # For objects, use jsonb_each with ordinality
        elem_tbl = (
            func.jsonb_each(val)
            .table_valued("k", "v", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )

    # Create the base subquery with the exploded elements
    parent_idx_col = (
        iter_subq.c.__comp_idx__ if "__comp_idx__" in iter_subq.c.keys() else None
    )
    base_cols = [
        iter_subq.c.log_event_id,
        (elem_tbl.c.value if is_array else elem_tbl.c.v).label("__comp_var__"),
        elem_tbl.c.ordinality,
    ]
    if parent_idx_col is not None:
        base_cols.append(parent_idx_col.label("__parent_idx__"))

    base = (
        select(*base_cols)
        .select_from(iter_subq.join(elem_tbl, literal(True)))
        .subquery("base")
    )

    unpacking = isinstance(filter_dict["target"], list)
    if unpacking:
        local_scope = {
            "__comp_idx__": (base.c.ordinality, "int"),
            "__comp_base__": base,
        }
        for i, ident in enumerate(filter_dict["target"]):
            comp_col = func.coalesce(base.c.__comp_var__.op("->")(i), "null")
            comp_type = LogDAO.infer_type(
                "",
                session.execute(select(comp_col)).scalar(),
            )
            local_scope[ident["value"]] = (comp_col, comp_type)
    else:
        comp_type = LogDAO.infer_type(
            "",
            session.execute(select(base.c.__comp_var__)).scalar(),
        )
        local_scope = {
            filter_dict["target"]["value"]: (base.c.__comp_var__, comp_type),
            "__comp_idx__": (base.c.ordinality, "int"),
            "__comp_base__": base,
        }

    # Use _replace_identifier on the element expression (elt) and any if conditions
    elt_expr = build_sql_query(
        filter_dict["elt"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )

    def _value_column(expr):
        """
        If *expr* is a sub‑query produced by build_sql_query return its
        `.c.value` column and make sure the caller knows it has to JOIN it.
        Otherwise just return *expr* unchanged.
        """
        if isinstance(expr, Subquery):
            # Check if the subquery has the __comp_idx__ column (from local scope)
            has_idx = hasattr(expr.c, "__comp_idx__")
            return (
                expr.c.value,
                expr,
                has_idx,
            )  # (column, subquery to join, has_idx flag)
        return expr, None, False

    # Build the subquery for the element expression
    elt_col, elt_subq, has_idx = _value_column(elt_expr)

    if elt_subq is not None:
        # Create a subquery that preserves both value and ordinality
        elt_with_row = (
            select(
                elt_subq.c.log_event_id,
                # If the subquery has __comp_idx__, use it; otherwise use log_event_id
                (
                    elt_subq.c.__comp_idx__ if has_idx else func.row_number().over()
                ).label("ordinality"),
                elt_subq.c.value,
                elt_subq.c.inferred_type,
            )
            .select_from(elt_subq)
            .subquery(name="elt_with_row")
        )

        # Join on both log_event_id and ordinality
        columns = [
            base.c.log_event_id.label("log_event_id"),
            *(
                [base.c.__parent_idx__.label("__parent_idx__")]
                if parent_idx_col is not None
                else []
            ),
            base.c.ordinality.label("ordinality"),
            elt_with_row.c.value.label("value"),
            elt_with_row.c.inferred_type.label("inferred_type"),
        ]
        from_clause = (
            select(*columns)
            .select_from(
                base.join(
                    elt_with_row,
                    and_(
                        base.c.log_event_id == elt_with_row.c.log_event_id,
                        base.c.ordinality == elt_with_row.c.ordinality,
                    ),
                ),
            )
            .order_by(base.c.log_event_id, base.c.ordinality, elt_with_row.c.ordinality)
            .subquery(name="from_clause")
        )

        # Use the value from the joined subquery
        elt_col = from_clause.c.value
    else:
        from_clause = base

    where_clause = literal(True)
    for cond_ast in filter_dict.get("ifs", []):
        cond_expr = build_sql_query(
            cond_ast,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
        if isinstance(cond_expr, Subquery):
            # Create a correlated scalar subquery
            condition = (
                select(cond_expr.c.value)
                .where(
                    cond_expr.c.log_event_id == from_clause.c.log_event_id,
                    cond_expr.c.__comp_idx__ == from_clause.c.ordinality,
                )
                .scalar_subquery()
            )
        else:
            condition = cond_expr
        where_clause = and_(where_clause, condition)

    # Build the final subquery for the list comprehension
    if parent_idx_col is not None:
        # nested comprehension
        select_cols = [
            from_clause.c.log_event_id,
            from_clause.c.__parent_idx__.label("__comp_idx__"),
            func.coalesce(
                func.jsonb_agg(aggregate_order_by(elt_col, from_clause.c.ordinality)),
                literal("[]", type_=JSONB),
            ).label("value"),
            literal("list").label("inferred_type"),
        ]
        group_by_cols = [
            from_clause.c.log_event_id,
            from_clause.c.__parent_idx__,
        ]
    else:
        # top-level comprehension
        select_cols = [
            from_clause.c.log_event_id,
            func.coalesce(
                func.jsonb_agg(aggregate_order_by(elt_col, from_clause.c.ordinality)),
                literal("[]", type_=JSONB),
            ).label("value"),
            literal("list").label("inferred_type"),
        ]
        group_by_cols = [
            from_clause.c.log_event_id,
        ]
    final = (
        select(*select_cols)
        .select_from(from_clause)
        .where(where_clause)
        .group_by(*group_by_cols)
        .subquery(name="final")
    )
    return final


def _handle_dict_comp(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    """
    Handle dictionary comprehension expressions in filter queries.

    This function processes expressions like {k: v*2 for k, v in some_dict.items() if v > 0}
    by exploding the source dictionary into rows, then applying the transformations and
    filter to each element, and finally aggregating back into a dictionary.
    """

    iter_subq = build_sql_query(
        filter_dict["iter"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    if not isinstance(iter_subq, Subquery):
        raise HTTPException(
            status_code=400,
            detail="dict comprehension source must be JSONB list/dict",
        )

    val, _ = _select_value(iter_subq, session, is_collection=True)

    # Determine if we're iterating over an array or object
    is_array = session.execute(select(func.jsonb_typeof(val))).scalar() == "array"

    # Use appropriate function based on the JSON type
    if is_array:
        # For arrays, use jsonb_array_elements with ordinality
        elem_tbl = (
            func.jsonb_array_elements(val)
            .table_valued("value", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )
    else:
        # For objects, use jsonb_each with ordinality
        elem_tbl = (
            func.jsonb_each(val)
            .table_valued("key", "value", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )

    # Create the base subquery with the exploded elements
    base = (
        select(
            iter_subq.c.log_event_id,
            (
                elem_tbl.c.value.op("->>")(literal(0)) if is_array else elem_tbl.c.key
            ).label("__comp_key__"),
            (
                elem_tbl.c.value.op("->")(literal(1)) if is_array else elem_tbl.c.value
            ).label("__comp_val__"),
            elem_tbl.c.ordinality,
        )
        .select_from(iter_subq.join(elem_tbl, literal(True)))
        .subquery(name="base")
    )

    # Replace occurrences of the comprehension target with a fake identifier
    fake_ident_key = {"type": "identifier", "value": "__comp_key__"}
    fake_ident_val = {"type": "identifier", "value": "__comp_val__"}

    # Create a local scope with the comprehension variable and its ordinality
    comp_key_type = LogDAO.infer_type(
        "",
        session.execute(select(base.c.__comp_key__)).scalar(),
    )
    comp_val_type = LogDAO.infer_type(
        "",
        session.execute(select(base.c.__comp_val__)).scalar(),
    )
    local_scope = {
        "__comp_key__": (base.c.__comp_key__, comp_key_type),
        "__comp_val__": (base.c.__comp_val__, comp_val_type),
        "__comp_idx__": (base.c.ordinality, "int"),
        "__comp_base__": base,
    }

    def _value_column(expr):
        """
        If *expr* is a sub‑query produced by build_sql_query return its
        `.c.value` column and make sure the caller knows it has to JOIN it.
        Otherwise just return *expr* unchanged.
        """
        if isinstance(expr, Subquery):
            # Check if the subquery has the __comp_idx__ column (from local scope)
            has_idx = hasattr(expr.c, "__comp_idx__")
            return (
                expr.c.value,
                expr,
                has_idx,
            )  # (column, subquery to join, has_idx flag)
        return expr, None, False

    # Build the subqueries for key and value expressions
    key_expr = build_sql_query(
        _replace_identifier(
            filter_dict["key_elt"],
            filter_dict["target"],
            fake_ident_key,
        ),
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )

    val_expr = build_sql_query(
        _replace_identifier(
            filter_dict["val_elt"],
            filter_dict["target"],
            fake_ident_val,
        ),
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )

    # Build the subqueries for the key and value expressions
    key_col, key_subq, key_has_idx = _value_column(key_expr)
    val_col, val_subq, val_has_idx = _value_column(val_expr)

    # Start with the base table
    from_clause = base

    # Join with key_subq if needed
    if key_subq is not None:
        # Create a subquery that preserves both value and ordinality for key
        key_with_row = (
            select(
                key_subq.c.log_event_id,
                # If the subquery has __comp_idx__, use it; otherwise use row_number
                (
                    key_subq.c.__comp_idx__ if key_has_idx else func.row_number().over()
                ).label("ordinality"),
                key_subq.c.value,
                key_subq.c.inferred_type,
            )
            .select_from(key_subq)
            .subquery(name="key_with_row")
        )

        # Join on both log_event_id and ordinality
        from_clause_with_key = (
            select(
                from_clause.c.log_event_id,
                from_clause.c.ordinality,
                from_clause.c.__comp_key__,
                key_with_row.c.value.label("key_value"),
                key_with_row.c.inferred_type.label("key_type"),
            )
            .select_from(
                from_clause.join(
                    key_with_row,
                    and_(
                        from_clause.c.log_event_id == key_with_row.c.log_event_id,
                        from_clause.c.ordinality == key_with_row.c.ordinality,
                    ),
                ),
            )
            .subquery(name="from_clause_with_key")
        )

    # Join with val_subq if needed
    if val_subq is not None:
        # Create a subquery that preserves both value and ordinality for value
        val_with_row = (
            select(
                val_subq.c.log_event_id,
                # If the subquery has __comp_idx__, use it; otherwise use row_number
                (
                    val_subq.c.__comp_idx__ if val_has_idx else func.row_number().over()
                ).label("ordinality"),
                val_subq.c.value,
                val_subq.c.inferred_type,
            )
            .select_from(val_subq)
            .subquery(name="val_with_row")
        )

        # Join on both log_event_id and ordinality
        from_clause_with_val = (
            select(
                from_clause.c.log_event_id,
                from_clause.c.ordinality,
                from_clause.c.__comp_val__,
                val_with_row.c.value.label("val_value"),
                val_with_row.c.inferred_type.label("val_type"),
            )
            .select_from(
                from_clause.join(
                    val_with_row,
                    and_(
                        from_clause.c.log_event_id == val_with_row.c.log_event_id,
                        from_clause.c.ordinality == val_with_row.c.ordinality,
                    ),
                ),
            )
            .subquery(name="from_clause_with_val")
        )

    # --- Build the joined_clause ---
    final_key_col = None
    final_val_col = None

    if from_clause_with_key is not None and from_clause_with_val is not None:
        # Scenario 4: Both key and value subqueries exist. Join the two intermediate results.
        joined_clause = (
            select(
                from_clause_with_key.c.log_event_id,
                from_clause_with_key.c.ordinality,
                from_clause_with_key.c.key_value,
                from_clause_with_val.c.val_value,
            )
            .select_from(
                from_clause_with_key.join(
                    from_clause_with_val,
                    and_(
                        from_clause_with_key.c.log_event_id
                        == from_clause_with_val.c.log_event_id,
                        from_clause_with_key.c.ordinality
                        == from_clause_with_val.c.ordinality,
                    ),
                ),
            )
            .subquery(name="joined_clause")
        )
        final_key_col = joined_clause.c.key_value
        final_val_col = joined_clause.c.val_value

    elif from_clause_with_key is not None:
        # Scenario 2: Only key subquery exists. Use from_clause_with_key.
        joined_clause = from_clause_with_key
        final_key_col = joined_clause.c.key_value
        final_val_col = joined_clause.c.__comp_val__

    elif from_clause_with_val is not None:
        # Scenario 3: Only value subquery exists. Use from_clause_with_val.
        joined_clause = from_clause_with_val
        final_key_col = joined_clause.c.__comp_key__
        final_val_col = joined_clause.c.val_value

    else:
        # Scenario 1: Neither subquery exists. Use base directly.
        joined_clause = (
            select(
                base.c.log_event_id,
                base.c.ordinality,
                base.c.__comp_key__.label("key_value"),
                base.c.__comp_val__.label("val_value"),
            )
            .select_from(base)
            .subquery(name="joined_clause")
        )
        final_key_col = joined_clause.c.key_value
        final_val_col = joined_clause.c.val_value

    # Process filter conditions
    where_clause = literal(True)
    for cond_ast in filter_dict.get("ifs", []):
        cond_expr = build_sql_query(
            _replace_identifier(cond_ast, filter_dict["target"], fake_ident_val),
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
        if isinstance(cond_expr, Subquery):
            # Create a correlated scalar subquery
            condition = (
                select(cond_expr.c.value)
                .where(
                    cond_expr.c.log_event_id == joined_clause.c.log_event_id,
                    cond_expr.c.__comp_idx__ == joined_clause.c.ordinality,
                )
                .scalar_subquery()
            )
        else:
            condition = cond_expr
        where_clause = and_(where_clause, condition)

    # Create the final object aggregation subquery
    final = (
        select(
            joined_clause.c.log_event_id,
            func.coalesce(
                func.jsonb_object_agg(final_key_col, final_val_col),
                literal("{}", type_=JSONB),
            ).label("value"),
            literal("dict").label("inferred_type"),
        )
        .select_from(joined_clause)
        .where(where_clause)
        .group_by(joined_clause.c.log_event_id)
        .subquery(name="final")
    )
    return final


def _handle_zip(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    args = [
        build_sql_query(
            arg,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
        for arg in filter_dict["rhs"]
    ]
    if not all(isinstance(arg, Subquery) for arg in args):
        raise HTTPException(
            status_code=400,
            detail="zip() expects only JSONB list columns",
        )

    zipped_subqs = []
    for idx, arg in enumerate(args):
        col, _ = _select_value(arg, session, is_collection=True)
        table_valued = (
            func.jsonb_array_elements(col)
            .table_valued("value", with_ordinality="ordinality")
            .alias(f"elem_tbl_{idx}")
        )
        sub = (
            select(
                arg.c.log_event_id.label("log_event_id"),
                table_valued.c.ordinality.label("ordinality"),
                table_valued.c.value.label(f"value_{idx}"),
            )
            .select_from(arg.join(table_valued, literal(True)))
            .subquery(name=f"zip_subq_{idx}")
        )
        zipped_subqs.append(sub)

    base = zipped_subqs[0]
    for i, other in enumerate(zipped_subqs[1:], start=1):
        base = (
            select(
                base.c.log_event_id,
                base.c.ordinality,
                *[base.c[col] for col in base.c.keys() if col.startswith("value")],
                other.c[f"value_{i}"],
            )
            .select_from(
                base.join(
                    other,
                    and_(
                        base.c.log_event_id == other.c.log_event_id,
                        base.c.ordinality == other.c.ordinality,
                    ),
                ),
            )
            .subquery(name=f"zip_join_{i}")
        )

    value_columns = [base.c[col] for col in base.c.keys() if col.startswith("value")]

    zipped = (
        select(
            base.c.log_event_id,
            func.coalesce(
                func.jsonb_agg(func.jsonb_build_array(*value_columns)),
                literal("[]", type_=JSONB),
            ).label("value"),
            literal("list").label("inferred_type"),
        )
        .group_by(base.c.log_event_id)
        .subquery(name="zipped")
    )
    return zipped


def build_sql_query(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    *,
    local_scope=None,
):
    """
    Recursively build SQLAlchemy filter or expression from filter_dict.

    Args:
        filter_dict (dict): The filter dictionary.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.
        log_event_ids: IDs of log events to filter on.
        is_derived: Whether this is for a derived log.
        local_scope: Dictionary mapping local variable names to (column, type) tuples.
        Used for comprehensions to avoid building subqueries for local variables.

    Returns:
        SQLAlchemy condition or expression
    """
    if local_scope is None:
        local_scope = {}

    # Base cases
    if not isinstance(filter_dict, dict):
        return literal(filter_dict)

    if "type" in filter_dict:
        if filter_dict["type"] == "identifier":
            key = filter_dict["value"]

            if key in local_scope:
                col, itype = local_scope[key]

                base_sub = local_scope.get("__comp_base__")
                if base_sub is not None and "__comp_idx__" in local_scope:
                    cols = [
                        base_sub.c.log_event_id.label("log_event_id"),
                        base_sub.c.ordinality.label("__comp_idx__"),
                        col.label("value"),
                        literal(itype).label("inferred_type"),
                    ]
                    if "__parent_idx__" in local_scope and hasattr(
                        base_sub.c, "__parent_idx__",
                    ):
                        cols.append(base_sub.c.__parent_idx__.label("__parent_idx__"))

                    subq = (
                        select(*cols)
                        .select_from(base_sub)
                        .subquery(name=f"__local_{key}_{random.randint(1,100000000)}")
                    )
                    return subq

            # Otherwise, proceed with normal identifier lookup
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
            local_scope=local_scope,
        )

    # Handle arithmetic operators (+, -, *, /, %, **, //)
    elif operand in ("+", "-", "*", "/", "%", "**", "//"):
        return _handle_arithmetic_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
        )

    # Handle comparison operators (==, !=, <, >, <=, >=, is, is not)
    elif operand in ("==", "!=", "<", ">", "<=", ">=", "is", "is not"):
        return _handle_comparison_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
        )

    # Handle membership operators (in, not in)
    elif operand in ("in", "not in"):
        return _handle_membership_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
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
            local_scope=local_scope,
        )

    # Handle list/dict indexing
    elif operand == "INDEX":
        return _handle_index_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
        )
    # Handle dict methods (keys, values, items)
    elif operand == "dict_method":
        return _handle_dict_method(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    # Handle if expressions
    elif operand == "if_expr":
        return _handle_if_expr(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    # Handle list comprehensions
    elif operand == "list_comp":
        return _handle_list_comp(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    # Handle dictionary comprehensions
    elif operand == "dict_comp":
        return _handle_dict_comp(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    # Handle zip
    elif operand == "zip":
        return _handle_zip(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    else:
        if operand is not None:
            raise ValueError(f"Unknown operand or structure: {filter_dict}")
        else:
            return literal(filter_dict)


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

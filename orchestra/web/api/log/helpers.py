import json
import re
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple, Union

from sqlalchemy import (
    BindParameter,
    Boolean,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Subquery, and_, not_, or_
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.models.orchestra_models import Log

STR_TO_SQL_TYPES = {
    "bool": Boolean,
    "int": Integer,
    "float": Float,
    "str": String,
    "timestamp": DateTime,
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
    => "BASE_IN([10],score) - BASE_IN([20],score)" if we are referencing 1 ID each time.

    If you have multiple IDs, we might do "BASE_IN([10,11],score)" etc.
    Because we want membership logic (log_event_id in [10,11]).
    """
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


def _compute_expression(filter_dict, log_event_alias, session):
    """
    Use build_sql_query -> subquery or expression -> .execute() -> return single result.
    If multiple rows, pick the first or do an aggregator as needed.
    """
    expr = build_sql_query(filter_dict, log_event_alias, session)
    if isinstance(expr, Subquery):
        rows = session.execute(select(expr.c.log_event_id, expr.c.value)).fetchall()
        if not rows:
            return None
        # If you want an aggregator, do sum(...) or so. For now, pick the first row's .value
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


def _tokenize(s):
    token_specification = [
        ("NUMBER", r"-?(\d+(\.\d*)?|\.\d+)"),  # Integer or decimal number, +ve or -ve
        (
            "STRING",
            r'"(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\'',
        ),  # String with non-greedy quantifier
        # Operators, note the order to match 'not in' before 'not' and 'in'
        (
            "OP",
            r"==|!=|<=|>=|<|>|(?<!\w)(?:not in|is not|in|not|and|or|is)(?!\w)|\+|\-|\*|/|%",
        ),
        ("ROUND", r"(?<!\w)round(?!\w)"),
        (
            "FUNC",
            r"(?<!\w)(?:len|type|exists|version|str(?=\()|to_str)",
        ),
        (
            "BASEFUNC",
            r"(?<!\w)BASE(?!\w)",  # special function to handle derived log notation
        ),
        (
            "TYPE_LITERAL",
            r"(?<!\w)(?:str|int|float|bool|list|dict|tuple|set|timestamp|datetime)(?!\w)",
        ),  # Type literals
        ("BOOLEAN", r"(?<!\w)(?:True|False)(?!\w)"),  # Booleans
        ("IDENTIFIER", r"[A-Za-z_/][A-Za-z0-9_/]*"),  # Identifiers
        ("LPAREN", r"\("),
        ("RPAREN", r"\)"),
        ("COMMA", r","),
        (
            "BRACKET_OPEN",
            r"[\[\{]",
        ),  # We detect [ or {, then parse_nested to build an OTHER token
        ("SKIP", r"[ \t]+"),  # Skip over spaces and tabs
        ("MISMATCH", r"."),  # Any other character
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
            value = float(value) if "." in value else int(value)
            tokens.append(("NUMBER", value))
        elif kind == "STRING":
            # check if is datetime
            try:
                timestamp = datetime.fromisoformat(value).timestamp()
                tokens.append(("NUMBER", timestamp))
            except:
                # Remove the surrounding quotes and unescape
                value = value[1:-1]
                value = bytes(value, "utf-8").decode("unicode_escape")
                tokens.append(("STRING", value))
        elif kind == "BOOLEAN":
            value = True if value == "True" else False
            tokens.append(("BOOLEAN", value))
        elif kind in (
            "IDENTIFIER",
            "FUNC",
            "BASEFUNC",
            "ROUND",
            "OP",
            "LPAREN",
            "RPAREN",
            "COMMA",
        ):
            tokens.append((kind, value))
        elif kind == "TYPE_LITERAL":
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


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.current_token = tokens[0]

    def peek_back(self, n=1):
        """Look back n tokens without moving position"""
        if self.pos - n >= 0:
            return self.tokens[self.pos - n]
        return None

    def in_type_check_context(self):
        """Check if we're inside a type() function call like `type(x) is int`."""
        prev_token = self.peek_back(1)
        return (
            prev_token and prev_token[0] == "OP" and prev_token[1] in ("is", "is not")
        )

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
        while self.current_token[0] == "OP" and self.current_token[1] in (
            "+",
            "-",
            "*",
            "/",
            "%",
        ):
            op = self.current_token[1]
            self.advance()
            right = self.mul_div_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def mul_div_expr(self):
        node = self.primary()
        return node

    def primary(self):
        # --- 1) handle function calls like len(a), to_str(b), etc. ---
        if self.current_token[0] == "FUNC":
            fn = self.current_token[1]
            self.advance()
            if self.current_token[0] == "LPAREN":
                self.advance()
                expr = self.expr()
                if self.current_token[0] == "RPAREN":
                    self.advance()
                else:
                    raise RuntimeError('Expected ")" after function call')
                return {"operand": fn, "rhs": expr}
            else:
                raise RuntimeError('Expected "(" after function call')

        # --- 2) handle round(...) with 1 or 2 arguments ---
        elif self.current_token[0] == "ROUND":
            fn = "round"
            self.advance()  # consume the 'round' token
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
                    raise RuntimeError("Expected ')' after round(...) arguments")
                self.advance()  # consume RPAREN

                return {"operand": fn, "rhs": args}
            else:
                raise RuntimeError("Expected '(' after round")

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
                return {"operand": fn, "rhs": [first_arg, second_arg]}
            else:
                raise RuntimeError(f"Expected '(' after {fn}")

        # --- 4) handle type literals like int, float, etc. ---
        elif self.current_token[0] == "TYPE_LITERAL":
            if self.in_type_check_context():
                node = {"type": "type_literal", "value": self.current_token[1]}
            else:
                node = {"type": "identifier", "value": self.current_token[1]}
            self.advance()
            return node

        # --- 5) parentheses grouping ---
        elif self.current_token[0] == "LPAREN":
            self.advance()
            node = self.expr()
            if self.current_token[0] == "RPAREN":
                self.advance()
            else:
                raise RuntimeError('Expected ")"')
            return node

        # --- 6) booleans ---
        elif self.current_token[0] == "BOOLEAN":
            node = self.current_token[1]
            self.advance()
            return node

        # --- 7) identifiers (including subsequent indexing) ---
        elif self.current_token[0] == "IDENTIFIER":
            node = {"type": "identifier", "value": self.current_token[1]}
            self.advance()

            # Now handle any subsequent indexing: x['key'], x[0], x['key'][something_else] ...
            while self.current_token[0] == "OTHER":
                bracket_str = self.current_token[1]
                # If it's something like "[...]" or "{...}" we can parse inside as an expression:
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
                    # if for some reason it's "OTHER" not starting with bracket, break or error
                    break

            return node

        # --- 8) numbers ---
        elif self.current_token[0] == "NUMBER":
            node = self.current_token[1]
            self.advance()
            return node

        # --- 9) strings ---
        elif self.current_token[0] == "STRING":
            node = {"type": "string", "value": self.current_token[1]}
            self.advance()
            return node

        # --- 10) "OTHER" ---
        elif self.current_token[0] == "OTHER":
            node = {"type": "other", "value": self.current_token[1]}
            self.advance()
            return node

        else:
            raise RuntimeError(f"Unexpected token {self.current_token}")


# Filtering #
# ----------#


def str_filter_exp_to_dict(s):
    tokens = _tokenize(s)
    parser = _Parser(tokens)
    result = parser.parse()
    return result


def _select_value(subq, session):
    """
    Helper function to select the appropriate value column from a subquery.
    Prioritizes 'value' if it exists, otherwise selects based on inferred types.
    """
    if hasattr(subq.c, "value"):
        return subq.c.value
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
            "list": subq.c.jsonb_value,
            "dict": subq.c.jsonb_value,
        }
        return d[dt]
    except:
        return None


def _build_subquery_for_identifier(key, log_event_alias, alias=None):
    """
    Build a subselect that retrieves columns for a given log key.
    The returned subselect columns typically include:
      - id (to allow joining)
      - several casted columns (str_value, int_value, float_value, bool_value, jsonb_value)
    """
    log_alias = aliased(Log)
    subq = (
        select(
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
                (log_alias.inferred_type == "str", cast(log_alias.value, String)),
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
        )
        .where(
            log_alias.log_event_id == log_event_alias.id,
            log_alias.key == key,
        )
        .subquery(name=alias)
    )
    return subq


def _join_subqueries(lhs_subq, rhs_subq, expr):
    """
    Given two subqueries lhs_subq and rhs_subq and an expression expr that combines
    their respective columns, produce a new subquery that merges them (by log_event_id),
    with 'expr' as the 'value' column.

    This is useful for arithmetic operations and comparisons. The resulting
    subquery can be used in further operations.
    """
    j = (
        select(
            lhs_subq.c.log_event_id.label("log_event_id"),
            expr.label("value"),
        )
        .select_from(lhs_subq)
        .join(rhs_subq, lhs_subq.c.log_event_id == rhs_subq.c.log_event_id)
        .subquery()
    )
    return j


# Helper function for logical operators (and, or, not)
def _handle_logical_operator(filter_dict, log_event_alias, session):
    """
    Handles logical operators ('and', 'or', 'not') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the logical operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the logical operator.
    """
    operand = filter_dict.get("operand")
    lhs = (
        build_sql_query(filter_dict.get("lhs"), log_event_alias, session)
        if operand != "not"
        else None
    )
    rhs = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

    # Check if lhs and rhs are subqueries
    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if operand in ("and", "or"):
        if lhs_is_sub and rhs_is_sub:
            lval = _select_value(lhs, session)
            rval = _select_value(rhs, session)

            if operand == "and":
                combined_expr = and_(lval, rval)
            else:
                combined_expr = or_(lval, rval)

            return _join_subqueries(lhs, rhs, combined_expr)

        elif lhs_is_sub:
            lval = _select_value(lhs, session)
            if operand == "and":
                combined_expr = and_(lval, rhs)
            else:
                combined_expr = or_(lval, rhs)
            return (
                select(
                    lhs.c.log_event_id.label("log_event_id"),
                    combined_expr.label("value"),
                )
                .select_from(lhs)
                .subquery()
            )

        elif rhs_is_sub:
            rval = _select_value(rhs, session)
            if operand == "and":
                combined_expr = and_(lhs, rval)
            else:
                combined_expr = or_(lhs, rval)
            return (
                select(
                    rhs.c.log_event_id.label("log_event_id"),
                    combined_expr.label("value"),
                )
                .select_from(rhs)
                .subquery()
            )

        else:
            if operand == "and":
                return and_(lhs, rhs)
            else:
                return or_(lhs, rhs)

    elif operand == "not":
        if rhs_is_sub:
            rval = _select_value(rhs, session)
            not_expr = not_(rval)
            return (
                select(
                    rhs.c.log_event_id.label("log_event_id"),
                    not_expr.label("value"),
                )
                .select_from(rhs)
                .subquery()
            )
        else:
            return not_(rhs)


# Helper function for arithmetic operators (+, -, *, /, %)
def _handle_arithmetic_operator(filter_dict, log_event_alias, session):
    """
    Handles arithmetic operators ('+', '-', '*', '/', '%') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the arithmetic operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the arithmetic operator.
    """
    operand = filter_dict.get("operand")
    lhs = build_sql_query(filter_dict.get("lhs"), log_event_alias, session)
    rhs = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval = _select_value(lhs, session)
        rval = _select_value(rhs, session)
        if operand == "+":
            expr = lval + rval
        elif operand == "-":
            expr = lval - rval
        elif operand == "*":
            expr = lval * rval
        elif operand == "/":
            expr = lval / rval
        elif operand == "%":
            expr = lval % rval
        return _join_subqueries(lhs, rhs, expr)
    elif lhs_is_sub:
        lval = _select_value(lhs, session)
        if operand == "+":
            expr = lval + rhs
        elif operand == "-":
            expr = lval - rhs
        elif operand == "*":
            expr = lval * rhs
        elif operand == "/":
            expr = lval / rhs
        elif operand == "%":
            expr = lval % rhs
        return (
            select(
                lhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
            )
            .select_from(lhs)
            .subquery()
        )
    elif rhs_is_sub:
        rval = _select_value(rhs, session)
        if operand == "+":
            expr = lhs + rval
        elif operand == "-":
            expr = lhs - rval
        elif operand == "*":
            expr = lhs * rval
        elif operand == "/":
            expr = lhs / rval
        elif operand == "%":
            expr = lhs % rval
        return (
            select(
                rhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
            )
            .select_from(rhs)
            .subquery()
        )
    else:
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


# Helper function for comparison operators (==, !=, <, >, <=, >=, is, is not)
def _handle_comparison_operator(filter_dict, log_event_alias, session):
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
    lhs = build_sql_query(filter_dict.get("lhs"), log_event_alias, session)
    rhs = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval = _select_value(lhs, session)
        rval = _select_value(rhs, session)
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
        return _join_subqueries(lhs, rhs, expr)
    elif lhs_is_sub:
        lval = _select_value(lhs, session)
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
            expr = lval.is_(rhs) if rhs is None else lval == rhs
        elif operand == "is not":
            expr = lval.isnot(rhs) if rhs is None else lval != rhs
        return (
            select(
                lhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
            )
            .select_from(lhs)
            .subquery()
        )
    elif rhs_is_sub:
        rval = _select_value(rhs, session)
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
            expr = lhs.is_(rval) if rval is None else lhs == rval
        elif operand == "is not":
            expr = lhs.isnot(rval) if rval is None else lhs != rval
        return (
            select(
                rhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
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
def _handle_membership_operator(filter_dict, log_event_alias, session):
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

    lhs = build_sql_query(filter_dict.get("lhs"), log_event_alias, session)
    rhs = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    # Both sides are subqueries
    if lhs_is_sub and rhs_is_sub:
        lval = _select_value(lhs, session)
        rval = _select_value(rhs, session)
        condition = _substring_expr(lval, rval)
        if not is_in:
            condition = ~condition

        expr = exists().where(
            and_(
                lhs.c.log_event_id == rhs.c.log_event_id,
                condition,
            ),
        )
        return _join_subqueries(lhs, rhs, expr)

    # Only LHS is a subquery
    elif lhs_is_sub and not rhs_is_sub:
        lval = _select_value(lhs, session)
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
            )
            .select_from(lhs)
            .subquery()
        )

    # Only RHS is a subquery
    elif rhs_is_sub and not lhs_is_sub:
        rval = _select_value(rhs, session)
        lhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("lhs"), lhs)

        if lhs_list is not None and isinstance(lhs_list, list):
            cond = rval.in_(lhs_list) if is_in else ~rval.in_(lhs_list)

        else:
            # Substring check. We'll check: "lhs in to_str(rval)" => substring.
            substring_cond = _substring_expr(lhs, rval)
            cond = substring_cond if is_in else ~substring_cond

        return (
            select(
                rhs.c.log_event_id.label("log_event_id"),
                cond.label("value"),
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
    if not rhs_dict:
        return None

    possible_str = rhs_dict.get("value")
    if isinstance(possible_str, str) and possible_str.strip():
        try:
            parsed = json.loads(possible_str)
            if isinstance(parsed, (list, dict)):
                return parsed
        except Exception:
            pass

    if isinstance(rhs_val, BindParameter):
        val = rhs_val.value
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, (list, dict)):
                    return parsed
            except Exception:
                pass

    if isinstance(rhs_val, (list, dict)):
        return rhs_val

    return None


# Helper function for functions (len, to_str, type, round, exists, version)
def _handle_functions(filter_dict, log_event_alias, session):
    """
    Handles function-based operations ('len', 'to_str', 'type', 'round', 'exists', 'version') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the function and its arguments.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the provided function.
    """
    operand = filter_dict.get("operand")
    if isinstance(filter_dict.get("rhs"), dict):
        rhs_expr = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)
    else:
        rhs_expr = [
            build_sql_query(expr, log_event_alias, session)
            for expr in filter_dict.get("rhs")
        ]
    if operand == "len":
        rval = _select_value(rhs_expr, session)
        if isinstance(rhs_expr, Subquery):
            subq = (
                select(
                    Log.log_event_id.label("log_event_id"),
                    case(
                        (
                            Log.inferred_type == "list",
                            func.jsonb_array_length(
                                cast(rval, JSONB),
                            ).cast(Float),
                        ),
                        (
                            Log.inferred_type == "dict",
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
                            Log.inferred_type == "str",
                            func.length(
                                cast(rval, String),
                            ).cast(Float),
                        ),
                        else_=0,
                    ).label("value"),
                )
                .select_from(Log)
                .join(log_event_alias, Log.log_event_id == log_event_alias.id)
                .join(rhs_expr, Log.log_event_id == rhs_expr.c.log_event_id)
                .where(
                    Log.key == filter_dict["rhs"]["value"],
                )
                .subquery()
            )
            return subq
        else:
            subq = (
                select(
                    Log.log_event_id.label("log_event_id"),
                    case(
                        (
                            Log.inferred_type == "list",
                            func.jsonb_array_length(
                                cast(Log.value, JSONB),
                            ).cast(Float),
                        ),
                        (
                            Log.inferred_type == "dict",
                            select(func.count())
                            .select_from(
                                func.jsonb_object_keys(
                                    cast(Log.value, JSONB),
                                ),
                            )
                            .scalar_subquery()
                            .cast(Float),
                        ),
                        (
                            Log.inferred_type == "str",
                            func.length(
                                cast(Log.value, String),
                            ).cast(Float),
                        ),
                        else_=0,
                    ).label("value"),
                )
                .select_from(Log)
                .join(log_event_alias, Log.log_event_id == log_event_alias.id)
                .where(
                    Log.key == filter_dict["rhs"]["value"],
                )
                .subquery()
            )
            return subq

    elif operand == "to_str":
        if isinstance(rhs_expr, Subquery):
            expr = func.cast(_select_value(rhs_expr, session), String)
            return (
                select(
                    rhs_expr.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                )
                .select_from(rhs_expr)
                .subquery()
            )
        else:
            return str(rhs_expr)

    elif operand == "round":
        # 1) Normalize the "rhs_expr" into a list of length 1 or 2
        if not isinstance(rhs_expr, list):
            rhs_expr = [rhs_expr]
        if len(rhs_expr) == 1:
            # round(val)
            val_expr = rhs_expr[0]
            if isinstance(val_expr, Subquery):
                # subquery => we retrieve the numeric column
                val_col = _select_value(val_expr, session)
                # produce a new subquery
                subq = (
                    select(
                        val_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_col, Numeric)).label("value"),
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
            # If digits_expr is not an integer-literal, we might need to cast.
            # For Postgres, `round(double precision, integer)` is typical.
            # If digits_expr is a subquery, we’d similarly do _select_value(digits_expr, session).
            if isinstance(val_expr, Subquery) and isinstance(digits_expr, Subquery):
                val_col = _select_value(val_expr, session)
                dig_col = _select_value(digits_expr, session)
                subq = (
                    select(
                        val_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_col, Numeric), dig_col).label("value"),
                    )
                    .select_from(val_expr)
                    .join(
                        digits_expr,
                        val_expr.c.log_event_id == digits_expr.c.log_event_id,
                    )
                    .subquery()
                )
                return subq
            elif isinstance(val_expr, Subquery):
                val_col = _select_value(val_expr, session)
                # If digits_expr is literal or bind param, we can pass it directly:
                subq = (
                    select(
                        val_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_col, Numeric), digits_expr).label("value"),
                    )
                    .select_from(val_expr)
                    .subquery()
                )
                return subq
            elif isinstance(digits_expr, Subquery):
                dig_col = _select_value(digits_expr, session)
                # In that case, val_expr might be a literal
                subq = (
                    select(
                        digits_expr.c.log_event_id.label("log_event_id"),
                        func.round(cast(val_expr, Numeric), dig_col).label("value"),
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
    elif operand == "type":
        if isinstance(rhs_expr, Subquery):
            expr = rhs_expr.c.inferred_type
            return (
                select(
                    rhs_expr.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                )
                .select_from(rhs_expr)
                .subquery()
            )
        else:
            return type(rhs_expr).__name__

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
        identifier = filter_dict.get("rhs", {}).get("value")
        if identifier:
            version_subq = (
                select(
                    Log.log_event_id.label("log_event_id"),
                    Log.version.label("value"),
                )
                .select_from(Log)
                .join(log_event_alias, Log.log_event_id == log_event_alias.id)
                .where(
                    Log.key == identifier,
                )
                .subquery()
            )
            return version_subq
    elif operand == "BASE":
        # The parse node might have: { "operand":"BASE", "rhs":[ <log_event_id_expr>, <key_expr> ] }
        # We want to produce a subquery that fetches the (log_event_id, typed_value) from Log,
        # specifically where log_event_id = X, key = Y.
        # We'll interpret the first arg as an int literal, the second as a string key or an expression.

        if len(rhs_expr) != 2:
            raise ValueError("BASE(...) requires exactly 2 arguments: (event_id, key)")

        event_id_expr = rhs_expr[0]
        key_expr = rhs_expr[1]
        return _build_subquery_for_base_call(event_id_expr, key_expr, session)
    else:
        raise ValueError(f"Unknown function operand: {operand}")


def _handle_index_operator(filter_dict, log_event_alias, session):
    """
    For a parse node like:
      {"operand": "INDEX", "lhs": <some node>, "rhs": <some node>}
    we interpret LHS as a JSON object/array, and RHS as either a string key or an integer index.
    We'll produce a subquery that extracts that sub-value from LHS.

    Return shape: Subquery with (log_event_id, value).
    """
    lhs_node = filter_dict.get("lhs")
    rhs_node = filter_dict.get("rhs")

    lhs_expr = build_sql_query(lhs_node, log_event_alias, session)
    rhs_expr = build_sql_query(rhs_node, log_event_alias, session)

    # If LHS is a subquery => we pull out its .c.log_event_id plus the "value" column
    # If RHS is a subquery => that implies the index key is dynamic; in practice, you may or may not want to handle that
    # For simplicity, let's assume RHS is literal or a direct bind param.
    if isinstance(lhs_expr, Subquery):
        lhs_valcol = _select_value(
            lhs_expr,
            session,
        )  # JSONB column with the parent object/array
        if isinstance(rhs_expr, Subquery):
            # Potentially advanced scenario: the user wrote x[y], where y is a subquery.
            # We'll pick the .value from y, interpret it as a string or integer, and then do -> or ->> extraction.
            rhs_valcol = _select_value(rhs_expr, session)
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
                )
                .select_from(lhs_expr)
                .join(rhs_expr, lhs_expr.c.log_event_id == rhs_expr.c.log_event_id)
                .subquery()
            )
            return subq
        else:
            # RHS is a literal or direct expression. Could be an int or string:
            # If it's an int, we do valcol->'<idx>'. If string, we do valcol->'some_key'.
            if isinstance(rhs_expr, int):
                # For PG JSONB, array index is valcol -> idx as text
                idx_str = str(rhs_expr)
                extracted = lhs_valcol[idx_str]  # Postgres expression
            elif isinstance(rhs_expr, str):
                extracted = lhs_valcol[rhs_expr]
            else:
                # Possibly a BindParam. You can do .value
                if isinstance(rhs_expr, BindParameter):
                    # get the actual python value
                    key_or_idx = rhs_expr.value
                    extracted = lhs_valcol[json.loads(key_or_idx)]
                else:
                    # fallback
                    extracted = lhs_valcol[rhs_expr]

            # Build the subquery
            subq = (
                select(
                    lhs_expr.c.log_event_id.label("log_event_id"),
                    extracted.label("value"),
                )
                .select_from(lhs_expr)
                .subquery()
            )
            return subq

    else:
        # If LHS is not a subquery => e.g. LHS is a python dict or list literal
        # Then we can do python-level extraction. Or if LHS is a direct SQL expression (rare), do something else.
        # For simplicity, treat LHS as python literal:
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


def _build_subquery_for_base_call(list_of_ids_expr, key_expr, session):
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
    filtered_subquery = (
        select(
            key_expr.c.log_event_id.label("log_event_id"),
            _select_value(key_expr, session).label("value"),
        )
        .select_from(key_expr)
        .where(key_expr.c.log_event_id.in_(base_ids))
        .subquery()
    )

    return filtered_subquery


def build_sql_query(filter_dict, log_event_alias, session):
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
            return _build_subquery_for_identifier(
                key,
                log_event_alias,
                alias=f"select_{key}",
            )
        elif filter_dict["type"] == "type_literal":
            return literal(filter_dict["value"])
        elif filter_dict["type"] in ("int", "float", "bool", "other"):
            return literal(filter_dict["value"])
        elif filter_dict["type"] == "string":
            return literal(json.dumps(filter_dict["value"]))  # convert to json string

    operand = filter_dict.get("operand")

    # Handle logical operators (and, or, not)
    if operand in ("and", "or", "not"):
        return _handle_logical_operator(filter_dict, log_event_alias, session)

    # Handle arithmetic operators (+, -, *, /, %)
    elif operand in ("+", "-", "*", "/", "%"):
        return _handle_arithmetic_operator(filter_dict, log_event_alias, session)

    # Handle comparison operators (==, !=, <, >, <=, >=, is, is not)
    elif operand in ("==", "!=", "<", ">", "<=", ">=", "is", "is not"):
        return _handle_comparison_operator(filter_dict, log_event_alias, session)

    # Handle membership operators (in, not in)
    elif operand in ("in", "not in"):
        return _handle_membership_operator(filter_dict, log_event_alias, session)

    # Handle functions (len, to_str, type, round, exists, version)
    elif operand in ("len", "to_str", "type", "round", "exists", "version", "BASE"):
        return _handle_functions(filter_dict, log_event_alias, session)

    # Handle list/dict indexing
    elif operand == "INDEX":
        return _handle_index_operator(filter_dict, log_event_alias, session)
    # Handle unknown operand
    else:
        raise ValueError(f"Unknown operand or structure: {filter_dict}")


# Reduction #
# ----------#


# noinspection PyBroadException
def _is_timestamp(v: Any):
    try:
        datetime.fromisoformat(v)
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


def format_logs(all_logs, context_len=0):
    formatted_entries = dict()
    for log_data in all_logs:
        log = log_data[0]
        ts = log_data[1]
        key = log.key[context_len:]
        log_event_id = log.log_event_id
        if log_event_id not in formatted_entries:
            formatted_entries[log_event_id] = {"entries": {}, "versions": {}}
        assert (
            key not in formatted_entries[log_event_id]
        ), f"found duplicates for key {key} with log_id {log_event_id}"
        formatted_entries[log_event_id]["ts"] = ts.isoformat()

        # noinspection PyBroadException
        def _try_decode(str_in):
            try:
                return json.loads(str_in)
            except:
                return str_in

        value = _try_decode(log.value) if isinstance(log.value, str) else log.value
        formatted_entries[log_event_id]["entries"][key] = value

        formatted_entries[log_event_id]["versions"][key] = log.version
    return formatted_entries


def _flatten_fields(
    log_fields: List[Tuple[Union[int, List[int]], Union[str, List[str]]]],
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

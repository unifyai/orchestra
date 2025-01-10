import json
import re
import statistics
from datetime import datetime, timedelta
from typing import Any, List, Tuple, Union

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
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


def parse_nested(s, pos):
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
            # Skip over string literals
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
        # Updated STRING regex to handle nested quotation marks and escaped quotes correctly
        (
            "STRING",
            r'"(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\'',
        ),  # String with non-greedy quantifier
        # Operators, note the order to match 'not in' before 'not' and 'in'
        (
            "OP",
            r"==|!=|<=|>=|<|>|(?<!\w)(?:not in|is not|in|not|and|or|is)(?!\w)|\+|\-|\*|/|%",
        ),
        (
            "FUNC",
            r"(?<!\w)(?:round|len|type|exists|version|str(?=\()|to_str)",
        ),  # Functions
        (
            "TYPE_LITERAL",
            r"(?<!\w)(?:str|int|float|bool|list|dict|tuple|set|timestamp|datetime)(?!\w)",
        ),  # Type literals
        ("BOOLEAN", r"(?<!\w)(?:True|False)(?!\w)"),  # Booleans
        ("IDENTIFIER", r"[A-Za-z_/][A-Za-z0-9_/]*"),  # Identifiers
        ("LPAREN", r"\("),
        ("RPAREN", r"\)"),
        ("BRACKET_OPEN", r"[\[\{]"),
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
            "LEN",
            "STR",
            "TYPE_CHECK",
            "EXISTS",
            "VERSION",
            "OP",
            "LPAREN",
            "RPAREN",
            "FUNC",
        ):
            tokens.append((kind, value))
        elif kind == "TYPE_LITERAL":
            tokens.append((kind, value))
        elif kind == "BRACKET_OPEN":
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
        """Check if we're inside a type() function call"""
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
        node = self.or_expr()
        return node

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
            node = {"operand": op, "rhs": rhs}
            return node
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
        while self.current_token[0] == "OP" and self.current_token[1] in (
            "*",
            "/",
            "%",
        ):
            op = self.current_token[1]
            self.advance()
            right = self.primary()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def primary(self):
        if self.current_token[0] == "FUNC":
            fn = self.current_token[1]
            self.advance()
            if self.current_token[0] == "LPAREN":
                self.advance()
                expr = self.expr()
                if self.current_token[0] == "RPAREN":
                    self.advance()
                else:
                    raise RuntimeError(
                        'Expected ")" after function call',
                    )
                return {"operand": fn, "rhs": expr}
            else:
                raise RuntimeError(
                    'Expected "(" after function call',
                )
        elif self.current_token[0] == "TYPE_LITERAL":
            if self.in_type_check_context():
                node = {"type": "type_literal", "value": self.current_token[1]}
            else:
                node = {"type": "identifier", "value": self.current_token[1]}
            self.advance()
            return node
        elif self.current_token[0] == "LPAREN":
            self.advance()
            node = self.expr()
            if self.current_token[0] == "RPAREN":
                self.advance()
            else:
                raise RuntimeError('Expected ")"')
            return node
        elif self.current_token[0] == "BOOLEAN":
            node = self.current_token[1]
            self.advance()
            return node
        elif self.current_token[0] == "IDENTIFIER":
            node = {"type": "identifier", "value": self.current_token[1]}
            self.advance()
            return node
        elif self.current_token[0] == "NUMBER":
            node = self.current_token[1]
            self.advance()
            return node
        elif self.current_token[0] == "STRING":
            node = {"type": "string", "value": self.current_token[1]}
            self.advance()
            return node
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
    lhs = build_sql_query(filter_dict.get("lhs"), log_event_alias, session)
    rhs = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval = _select_value(lhs, session)
        rval = _select_value(rhs, session)
        if operand == "in":
            expr = exists().where(
                and_(
                    lhs.c.log_event_id == rhs.c.log_event_id,
                    func.replace(cast(rval, String), '"', "").like(
                        "%" + func.replace(cast(lval, String), '"', "") + "%",
                    ),
                ),
            )
        else:
            expr = exists().where(
                and_(
                    lhs.c.log_event_id == rhs.c.log_event_id,
                    func.replace(cast(rval, String), '"', "").like(
                        "%" + func.replace(cast(lval, String), '"', "") + "%",
                    ),
                ),
            )
        return _join_subqueries(lhs, rhs, expr)
    elif lhs_is_sub:
        rval = _select_value(rhs, session)
        lval = _select_value(lhs, session)
        if "lhs" in filter_dict and isinstance(filter_dict["lhs"], dict):
            if filter_dict["lhs"].get("type") == "identifier":
                key = filter_dict["lhs"]["value"]
                comparison_key = key
            elif filter_dict["lhs"].get("operand") is not None:
                comparison_key = None
            else:
                comparison_key = None
        else:
            comparison_key = filter_dict.get("lhs")
        if operand == "in":
            if comparison_key is not None:
                expr = exists().where(
                    and_(
                        Log.log_event_id == lhs.c.log_event_id,
                        Log.key == comparison_key,
                        func.replace(cast(Log.value, String), '"', "").like(
                            "%" + func.replace(cast(rval, String), '"', "") + "%",
                        ),
                    ),
                )
            else:
                expr = exists().where(
                    and_(
                        Log.log_event_id == lhs.c.log_event_id,
                        func.replace(cast(rval, String), '"', "").like(
                            "%" + func.replace(cast(rval, String), '"', "") + "%",
                        ),
                    ),
                )
        else:
            if comparison_key is not None:
                expr = ~exists().where(
                    and_(
                        Log.log_event_id == lhs.c.log_event_id,
                        Log.key == comparison_key,
                        func.replace(cast(Log.value, String), '"', "").like(
                            "%" + func.replace(cast(lhs, String), '"', "") + "%",
                        ),
                    ),
                )
            else:
                expr = ~exists().where(
                    and_(
                        Log.log_event_id == lhs.c.log_event_id,
                        func.replace(cast(rval, String), '"', "").like(
                            "%" + func.replace(cast(lhs, String), '"', "") + "%",
                        ),
                    ),
                )
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
        if "rhs" in filter_dict and isinstance(filter_dict["rhs"], dict):
            if filter_dict["rhs"].get("type") == "identifier":
                key = filter_dict["rhs"]["value"]
                comparison_key = key
            elif filter_dict["rhs"].get("operand") is not None:
                comparison_key = None
            else:
                comparison_key = None
        else:
            comparison_key = filter_dict.get("rhs")
        if operand == "in":
            if comparison_key is not None:
                expr = exists().where(
                    and_(
                        Log.log_event_id == rhs.c.log_event_id,
                        Log.key == comparison_key,
                        func.replace(cast(Log.value, String), '"', "").like(
                            "%" + func.replace(cast(lhs, String), '"', "") + "%",
                        ),
                    ),
                )
            else:
                expr = exists().where(
                    and_(
                        Log.log_event_id == rhs.c.log_event_id,
                        func.replace(cast(rval, String), '"', "").like(
                            "%" + func.replace(cast(lhs, String), '"', "") + "%",
                        ),
                    ),
                )
        else:
            if comparison_key is not None:
                expr = ~exists().where(
                    and_(
                        Log.log_event_id == rhs.c.log_event_id,
                        Log.key == comparison_key,
                        func.replace(cast(Log.value, String), '"', "").like(
                            "%" + func.replace(cast(lhs, String), '"', "") + "%",
                        ),
                    ),
                )
            else:
                expr = ~exists().where(
                    and_(
                        Log.log_event_id == rhs.c.log_event_id,
                        func.replace(cast(rval, String), '"', "").like(
                            "%" + func.replace(cast(lhs, String), '"', "") + "%",
                        ),
                    ),
                )
        return (
            select(
                rhs.c.log_event_id.label("log_event_id"),
                expr.label("value"),
            )
            .select_from(rhs)
            .subquery()
        )
    else:
        if operand == "in":
            return lhs.in_(rhs)
        else:
            return ~lhs.in_(rhs)


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
    rhs_expr = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

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
        if isinstance(rhs_expr, Subquery):
            expr = func.round(_select_value(rhs_expr, session))
            return (
                select(
                    rhs_expr.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                )
                .select_from(rhs_expr)
                .subquery()
            )
        else:
            return round(rhs_expr)

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

    else:
        raise ValueError(f"Unknown function operand: {operand}")


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
    elif operand in ("len", "to_str", "type", "round", "exists", "version"):
        return _handle_functions(filter_dict, log_event_alias, session)

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
        formatted_entries[log_event_id]["entries"][key] = log.value

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

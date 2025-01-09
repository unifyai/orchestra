import json
import re
import statistics
from datetime import datetime
from typing import Any, List, Tuple, Union

from sqlalchemy import (
    Boolean,
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
        ("FUNC", r"round|len|str|type|exists|version"),  # Functions
        # ("TYPE_CHECK", r"type"),  # Type check expression
        # ("LEN", r"len"),  # length
        # ("STR", r"str"),  # str function
        # ("EXISTS", r"exists"),  # exists
        # ("VERSION", r"version"),  # version
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
    # NOTE: this is an inefficiency. Ideally, we should be able to determine the type of the subquery
    # without an additional read operation from the database.

    dt = session.execute(select(subq)).first()[
        -1
    ]  # execute the subquery to determine the type.
    d = {
        "int": subq.c.int_value,
        "float": subq.c.float_value,
        "bool": subq.c.bool_value,
        "str": subq.c.str_value,
        "list": subq.c.jsonb_value,
        "dict": subq.c.jsonb_value,
    }
    return d[dt]

    # this does not work unfortuantely because sqlalchemy
    # does not support column selection based on dynamic types.
    # return case(
    #     *[
    #         (subq.inferred_type == "float", subq.float_value),
    #         (subq.inferred_type == "int", csubq.int_value),
    #         (subq.inferred_type == "str", subq.str_value),
    #         (subq.inferred_type == "bool", subq.bool_value),
    #         (subq.inferred_type == "list", subq.list_value),
    #         (subq.inferred_type == "dict", subq.dict_value)
    #     ],
    #     else_=None
    # )


def _build_subquery_for_identifier(key, log_event_alias):
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
        .subquery()
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
    log_alias = aliased(Log)

    # Base cases
    if not isinstance(filter_dict, dict):
        return literal(filter_dict)

    if "type" in filter_dict:
        if filter_dict["type"] == "identifier":
            key = filter_dict["value"]
            return _build_subquery_for_identifier(key, log_event_alias)
        elif filter_dict["type"] in ("string", "int", "float", "bool"):
            return literal(filter_dict["value"])

    operand = filter_dict.get("operand")

    # Handle logical operators (and, or, not)
    if operand in ("and", "or", "not"):
        lhs = build_sql_query(filter_dict.get("lhs"), log_event_alias, session) if operand != "not" else None
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
                return select(
                    lhs.c.log_event_id.label("log_event_id"),
                    combined_expr.label("value"),
                ).select_from(lhs).subquery()
                

            elif rhs_is_sub:
                # Only rhs is a subquery
                rval = _select_value(rhs, session)
                if operand == "and":
                    combined_expr = and_(lhs, rval)
                else:
                    combined_expr = or_(lhs, rval)
                return select(
                    rhs.c.log_event_id.label("log_event_id"),
                    combined_expr.label("value"),
                ).select_from(rhs).subquery()

            else:
                # Neither lhs nor rhs are subqueries
                if operand == "and":
                    return and_(lhs, rhs)
                else:
                    return or_(lhs, rhs)

        elif operand == "not":
            if rhs_is_sub:
                rval = _select_value(rhs, session)
                not_expr = not_(rval)
                return select(
                    rhs.c.log_event_id.label("log_event_id"),
                    not_expr.label("value"),
                ).select_from(rhs).subquery()
            else:
                return not_(rhs)

    # Handle arithmetic operators (+, -, *, /, %)
    elif operand in ("+", "-", "*", "/", "%"):
        lhs = build_sql_query(filter_dict.get("lhs"), log_event_alias, session)
        rhs = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

        # Check if lhs and rhs are subqueries
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
            return expr
            # return select(
            #     lhs.c.log_event_id.label("log_event_id"),
            #     expr.label("value"),
            # ).select_from(lhs).subquery()
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
            return expr
            # return select(
            #     rhs.c.log_event_id.label("log_event_id"),
            #     expr.label("value"),
            # ).select_from(rhs).subquery()
        else:
            # Both are literals
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

    # Handle comparison operators (==, !=, <, >, <=, >=, is, is not)
    elif operand in ("==", "!=", "<", ">", "<=", ">=", "is", "is not"):
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
                expr = lval.is_(rhs)
            elif operand == "is not":
                expr = lval.isnot(rhs)
            return expr
            # return select(
            #     lhs.c.log_event_id.label("log_event_id"),
            #     expr.label("value"),
            # ).select_from(lhs).subquery()
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
                expr = lhs.is_(rval)
            elif operand == "is not":
                expr = lhs.isnot(rval)
            return expr
            # return select(
            #     rhs.c.log_event_id.label("log_event_id"),
            #     expr.label("value"),
            # ).select_from(rhs).subquery()
        else:
            # Both are literals
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

    # Handle membership operators (in, not in)
    elif operand in ("in", "not in"):
        lhs = build_sql_query(filter_dict.get("lhs"), log_event_alias, session)
        rhs = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

        lhs_is_sub = isinstance(lhs, Subquery)
        rhs_is_sub = isinstance(rhs, Subquery)

        if lhs_is_sub and rhs_is_sub:
            lval = _select_value(lhs)
            rval = _select_value(rhs)
            if operand == "in":
                expr = lval.in_(select(rval))
            else:
                expr = ~lval.in_(select(rval))
            return _join_subqueries(lhs, rhs, expr)
        elif lhs_is_sub:
            lval = _select_value(lhs)
            if operand == "in":
                expr = lval.in_(rhs)
            else:
                expr = ~lval.in_(rhs)
            return (
                select(
                    lhs.c.log_event_id.label("log_event_id"),
                    expr.label("value"),
                )
                .select_from(lhs)
                .subquery()
            )
        elif rhs_is_sub:
            rval = _select_value(rhs)
            if operand == "in":
                expr = lhs.in_(rval)
            else:
                expr = ~lhs.in_(rval)
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

    # Handle functions (len, str, type, round, exists, version)
    elif operand in ("len", "str", "type", "round", "exists", "version"):
        # Functions typically have only 'rhs'
        rhs_expr = build_sql_query(filter_dict.get("rhs"), log_event_alias, session)

        if operand == "len":
            if isinstance(rhs_expr, Subquery):
                # Determine if it's a string or JSON array
                # For simplicity, assume string here; extend as needed
                expr = func.length(_select_value(rhs_expr))
            else:
                # Literal value, handle appropriately
                if isinstance(rhs_expr, str):
                    expr = len(rhs_expr)
                else:
                    raise ValueError(
                        "Cannot apply len() to non-string literal without subquery logic.",
                    )
            return expr

        elif operand == "str":
            if isinstance(rhs_expr, Subquery):
                expr = func.cast(_select_value(rhs_expr), String)
            else:
                expr = str(rhs_expr)
            return expr

        elif operand == "round":
            if isinstance(rhs_expr, Subquery):
                expr = func.round(_select_value(rhs_expr))
            else:
                expr = round(rhs_expr)
            return expr

        elif operand == "type":
            # 'type' usually paired with 'is' or 'is not', handled in comparisons
            # Here, just return the inferred_type
            if isinstance(rhs_expr, Subquery):
                expr = rhs_expr.c.inferred_type
            else:
                expr = type(rhs_expr).__name__
            return expr

        elif operand == "exists":
            if (
                isinstance(filter_dict.get("rhs"), dict)
                and filter_dict["rhs"].get("type") == "identifier"
            ):
                identifier = filter_dict["rhs"]["value"]
                exists_subq = select(log_alias.id).filter(
                    log_alias.log_event_id == log_event_alias.id,
                    log_alias.key == identifier,
                )
                return exists(exists_subq)
            else:
                raise ValueError("Invalid argument for 'exists' function.")

        elif operand == "version":
            # Handle version comparison
            # Expecting filter_dict to have:
            # lhs: {'operand': 'version', 'rhs': {'type': 'identifier', 'value': 'some_key'}}
            # rhs: {'type': 'string', 'value': '1.0'}
            if (
                isinstance(filter_dict.get("lhs"), dict)
                and filter_dict["lhs"].get("operand") == "version"
            ):
                version = filter_dict["rhs"]["value"]
                identifier = filter_dict["lhs"].get("rhs", {}).get("value")
                if identifier:
                    version_subq = (
                        session.query(log_alias.id)
                        .filter(
                            log_alias.log_event_id == log_event_alias.id,
                            log_alias.key == identifier,
                        )
                        .with_entities(log_alias.version)
                        .subquery()
                    )
                    return _select_value(version_subq) == version
                else:
                    raise ValueError("Invalid identifier for 'version' comparison.")
            else:
                raise ValueError("Invalid structure for 'version' function.")

    # Handle unknown operand
    else:
        raise ValueError(f"Unknown operand or structure: {operand}")


# Reduction #
# ----------#


def _is_type_for_len(v: Any) -> bool:
    return (
        isinstance(v, str)
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
) -> List[Union[int, float, bool]]:
    return [len(v) if _is_type_for_len(v) else v for v in values if v is not None]


def _count(values: List[Union[int, float, bool]]) -> Union[int, float]:
    return len(_preprocess(values))


def _sum(values: List[Union[int, float, bool]]) -> Union[int, float]:
    return sum(_preprocess(values))


def _mean(values: List[Union[int, float, bool]]) -> float:
    values = _preprocess(values)
    return sum(values) / len(values)


def _var(values: List[Union[int, float, bool]]) -> float:
    values = _preprocess(values)
    num_values = len(values)
    mean = sum(values) / num_values
    diffs_squared = [(v - mean) ** 2 for v in values]
    return sum(diffs_squared) / num_values


def _std(values: List[Union[int, float, bool]]) -> float:
    return _var(values) ** 0.5


def _min(values: List[Union[int, float, bool]]) -> Union[int, float, bool]:
    return min(_preprocess(values))


def _max(values: List[Union[int, float, bool]]) -> Union[int, float, bool]:
    return max(_preprocess(values))


def _median(values: List[Union[int, float, bool]]) -> Union[int, float, bool]:
    values = _preprocess(values)
    return statistics.median(values)


def _mode(values: List[Union[int, float, bool]]) -> Union[int, float, bool]:
    values = _preprocess(values)
    return statistics.mode(values)


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
        formatted_entries[log_event_id]["ts"] = ts.strftime("%Y-%m-%d %H:%M:%S")
        formatted_entries[log_event_id]["entries"][key] = json.loads(
            log.value,
        )
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
                if field not in flattened[log_id]:
                    flattened[log_id].append(field)
    return flattened

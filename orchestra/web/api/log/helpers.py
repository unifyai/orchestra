import json
import re
import statistics
from datetime import datetime
from typing import Any, List, Union, Tuple

from sqlalchemy import JSON, Boolean, Float, String, case, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql import and_, not_, or_

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
        ("OP", r"==|<=|>=|<|>|(?<!\w)(?:not in|in|not|and|or|is)(?!\w)"),
        ("LEN", r"len"),  # length
        ("EXISTS", r"exists"),  # exists
        ("VERSION", r"version"),  # version
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
        elif kind == "IDENTIFIER":
            tokens.append(("IDENTIFIER", value))
        elif kind == "LEN":
            tokens.append(("LEN", value))
        elif kind == "EXISTS":
            tokens.append(("EXISTS", value))
        elif kind == "VERSION":
            tokens.append(("VERSION", value))
        elif kind == "OP":
            tokens.append(("OP", value))
        elif kind == "LPAREN":
            tokens.append(("LPAREN", value))
        elif kind == "RPAREN":
            tokens.append(("RPAREN", value))
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
        node = self.primary()
        while self.current_token[0] == "OP" and self.current_token[1] in (
            "==",
            "<",
            ">",
            "<=",
            ">=",
            "in",
            "not in",
            "is",
        ):
            op = self.current_token[1]
            self.advance()
            right = self.primary()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def primary(self):
        if self.current_token[0] in ("LEN", "EXISTS", "VERSION") and self.current_token[
            1
        ] in (
            "len",
            "exists",
            "version",
        ):
            fn = self.current_token[1]
            self.advance()
            if self.current_token[0] == "LPAREN":
                self.advance()
                expr = self.expr()
                if self.current_token[0] == "RPAREN":
                    self.advance()
                else:
                    raise RuntimeError(
                        'Expected ")" after len, exists or version function',
                    )
                return {"operand": fn, "rhs": expr}
            else:
                raise RuntimeError(
                    'Expected "(" after len, exists, or version function',
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


def get_sqlalchemy_type(py_object):
    """Maps Python object types to SQLAlchemy column types."""
    py_type = type(py_object)

    if py_type is bool:
        return Boolean
    elif py_type in (int, float):
        return Float
    elif py_type is str:
        return String
    elif py_type is dict:
        return JSON
    else:
        raise TypeError(f"Unsupported type: {py_type}")


def build_filter(filter_dict, log_event_alias, session):
    """
    Recursively build SQLAlchemy filter from filter_dict.

    Args:
        filter_dict (dict): The filter dictionary.
        log_event_alias: Alias for LogEvent to correlate subqueries.

    Returns:
        SQLAlchemy condition
    """
    if filter_dict == {}:
        return None
    if not isinstance(filter_dict, dict):
        # Base case: direct value
        return filter_dict

    operand = filter_dict.get("operand")

    if operand in ("and", "or"):
        lhs = build_filter(filter_dict["lhs"], log_event_alias, session)
        rhs = build_filter(filter_dict["rhs"], log_event_alias, session)
        if operand == "and":
            return and_(lhs, rhs)
        else:
            return or_(lhs, rhs)

    elif operand == "not":
        rhs = build_filter(filter_dict["rhs"], log_event_alias, session)
        return not_(rhs)

    elif operand in ("==", "!=", "<", ">", "<=", ">=", "in", "not in", "is"):
        lhs = filter_dict["lhs"]
        rhs = filter_dict["rhs"]

        if isinstance(lhs, dict) and lhs.get("type") == "identifier":
            key = lhs["value"]
            log_alias = aliased(Log)
            subq = select(log_alias.id).filter(
                log_alias.log_event_id == log_event_alias.id,
                log_alias.key == key,
            )

            if operand in ("==", "!=", "is", "<", ">", "<=", ">="):
                try:
                    compare_value = rhs
                    if isinstance(compare_value, dict):
                        compare_value = compare_value["value"]
                        if rhs["type"] == "string":
                            compare_value = json.dumps(compare_value)
                        condition = log_alias.value
                    elif isinstance(compare_value, bool):
                        compare_value = bool(compare_value)
                        condition = cast(log_alias.value, Boolean)
                    else:
                        compare_value = float(compare_value)
                        condition = cast(log_alias.value, Float)
                    if operand == "==" or operand == "is":
                        subq = subq.filter(condition == compare_value)
                    elif operand == "!=":
                        subq = subq.filter(condition != compare_value)
                    elif operand == "<":
                        subq = subq.filter(condition < compare_value)
                    elif operand == ">":
                        subq = subq.filter(condition > compare_value)
                    elif operand == "<=":
                        subq = subq.filter(condition <= compare_value)
                    elif operand == ">=":
                        subq = subq.filter(condition >= compare_value)
                except ValueError:
                    raise ValueError(
                        f"Cannot cast value '{rhs}' to float for comparison.",
                    )
            elif operand == "in":
                subq = subq.filter(log_alias.value.contains(rhs))
            elif operand == "not in":
                subq = subq.filter(~log_alias.value.contains(rhs))

            return subq.exists()

        if isinstance(lhs, dict) and lhs.get("operand") == "len":
            length = rhs
            identifier = lhs.get("rhs", {}).get("value")
            if identifier:
                log_alias = aliased(Log)
                subq = (
                    session.query(log_alias.id)
                    .filter(
                        log_alias.log_event_id == log_event_alias.id,
                        log_alias.key == identifier,
                    )
                    .with_entities(
                        case(
                            (
                                log_alias.inferred_type == "list",
                                func.jsonb_array_length(
                                    cast(log_alias.value, JSONB),
                                ).cast(Float),
                            ),
                            (
                                log_alias.inferred_type == "dict",
                                select(func.count())
                                .select_from(
                                    func.jsonb_object_keys(
                                        cast(log_alias.value, JSONB),
                                    ),
                                )
                                .scalar_subquery()
                                .cast(Float),
                            ),
                            (
                                log_alias.inferred_type == "str",
                                func.length(
                                    cast(log_alias.value, JSONB)[0].astext,
                                ).cast(Float),
                            ),
                            else_=0,
                        ),
                    )
                )
                if operand == "<":
                    return subq.as_scalar() < length
                elif operand == ">":
                    return subq.as_scalar() > length
                elif operand == "<=":
                    return subq.as_scalar() <= length
                elif operand == ">=":
                    return subq.as_scalar() >= length
                elif operand == "==":
                    return subq.as_scalar() == length
                elif operand == "!=":
                    return subq.as_scalar() != length

        if isinstance(lhs, dict) and lhs.get("operand") == "version":
            version = rhs["value"]
            identifier = lhs.get("rhs", {}).get("value")
            if identifier:
                log_alias = aliased(Log)
                subq = (
                    session.query(log_alias.id)
                    .filter(
                        log_alias.log_event_id == log_event_alias.id,
                        log_alias.key == identifier,
                    )
                    .with_entities(log_alias.version)
                )
                if operand == "==":
                    return subq.as_scalar() == version

        if operand in ["in", "not in"]:
            if isinstance(rhs, dict) and rhs.get("type") == "identifier":
                key = rhs["value"]
                lhs_dict = isinstance(lhs, dict)
                lhs_value = lhs["value"] if lhs_dict else lhs
                log_alias = aliased(Log)
                subq = select(log_alias.id).filter(
                    log_alias.log_event_id == log_event_alias.id,
                    log_alias.key == key,
                )
                if operand == "in":
                    subq = subq.filter(log_alias.value.contains(lhs_value))
                elif operand == "not in":
                    subq = subq.filter(~log_alias.value.contains(lhs_value))

                return subq.exists()

    elif operand == "exists":
        rhs = filter_dict["rhs"]

        if isinstance(rhs, dict) and rhs.get("type") == "identifier":
            identifier = rhs["value"]

            log_alias = aliased(Log)
            subq = select(log_alias.id).filter(
                log_alias.log_event_id == log_event_alias.id,
                log_alias.key == identifier,
            )
            return subq.exists()
    else:
        # Handle literals or unexpected structures
        if "type" in filter_dict:
            if filter_dict["type"] == "boolean":
                return filter_dict["value"]
            elif filter_dict["type"] == "string":
                return filter_dict["value"]
            elif filter_dict["type"] == "number":
                return filter_dict["value"]

    raise ValueError(f"Unsupported filter structure: {filter_dict}")


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
    count = 0
    for log_data in all_logs:
        log = log_data[0]
        ts = log_data[1]
        if len(log_data) == 3:
            count = log_data[2]
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
    return formatted_entries, count


def _flatten_fields(
    log_fields: List[Tuple[Union[int, List[int]], Union[str, List[str]]]]
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

import json
import re
from typing import Any, List, Union


class KeyNotFound(Exception):
    pass


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
        ("NUMBER", r"\d+(\.\d*)?|\.\d+"),  # Integer or decimal number
        (
            "STRING",
            r"'([^'\\]*(?:\\.[^'\\]*)*)'|\"([^\"\\]*(?:\\.[^\"\\]*)*)\"",
        ),  # String
        # Operators, note the order to match 'not in' before 'not' and 'in'
        ("OP", r"==|<=|>=|<|>|(?<!\w)(?:not in|in|not|and|or|is)(?!\w)"),
        ("LEN", r"len"),  # length
        ("EXISTS", r"exists"),  # length
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
    mo = get_token(line)
    while mo is not None:
        kind = mo.lastgroup
        value = mo.group()
        if kind == "NUMBER":
            value = float(value) if "." in value else int(value)
            tokens.append(("NUMBER", value))
        elif kind == "STRING":
            if value[0] in ("'", '"'):
                value = value[1:-1].encode("utf-8").decode("unicode_escape")
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
        if self.current_token[0] in ("LEN", "EXISTS") and self.current_token[1] in (
            "len",
            "exists",
        ):
            fn = self.current_token[1]
            self.advance()
            if self.current_token[0] == "LPAREN":
                self.advance()
                expr = self.expr()
                if self.current_token[0] == "RPAREN":
                    self.advance()
                else:
                    raise RuntimeError('Expected ")" after len or exists function')
                return {"operand": fn, "rhs": expr}
            else:
                raise RuntimeError('Expected "(" after len or exists function')
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


def evaluate_filter_expression(expr, **variables):
    if isinstance(expr, dict):
        if "operand" in expr:
            operand = expr["operand"]
            if operand == "and":
                lhs = evaluate_filter_expression(expr["lhs"], **variables)
                if not lhs:
                    return False
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                return lhs and rhs
            elif operand == "or":
                lhs = evaluate_filter_expression(expr["lhs"], **variables)
                if lhs:
                    return True
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                return lhs or rhs
            elif operand == "not":
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                return not rhs
            elif operand in ("==", "<", ">", "<=", ">="):
                lhs = evaluate_filter_expression(expr["lhs"], **variables)
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                if operand == "==":
                    return lhs == rhs
                elif operand == "<":
                    return lhs < rhs
                elif operand == ">":
                    return lhs > rhs
                elif operand == "<=":
                    return lhs <= rhs
                elif operand == ">=":
                    return lhs >= rhs
            elif operand == "len":
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                return len(rhs)
            elif operand == "exists":
                try:
                    evaluate_filter_expression(expr["rhs"], **variables)
                    return True
                except KeyNotFound:
                    return False
            elif operand == "in":
                lhs = evaluate_filter_expression(expr["lhs"], **variables)
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                return lhs in rhs
            elif operand == "not in":
                lhs = evaluate_filter_expression(expr["lhs"], **variables)
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                return lhs not in rhs
            elif operand == "is":
                lhs = evaluate_filter_expression(expr["lhs"], **variables)
                rhs = evaluate_filter_expression(expr["rhs"], **variables)
                return lhs is rhs
            else:
                raise ValueError(f"Unknown operand: {operand}")
        elif "type" in expr:
            if expr["type"] == "identifier":
                var_name = expr["value"]
                if var_name in variables:
                    return variables[var_name]
                else:
                    raise KeyNotFound(f"Variable '{var_name}' not provided")
            elif expr["type"] == "string":
                return expr["value"]
            elif expr["type"] == "other":
                return json.loads(expr["value"].replace("'", '"'))
            else:
                raise ValueError(f"Unknown leaf node type: {expr['type']}")
        else:
            raise ValueError(f"Malformed expression node: {expr}")
    else:
        if isinstance(expr, (int, float, bool)):
            return expr
        else:
            raise TypeError(f"Unexpected expression type: {expr}")


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
    num_values = len(values)
    return sorted(values)[int(num_values / 2)]


def _mode(values: List[Union[int, float, bool]]) -> Union[int, float, bool]:
    values = _preprocess(values)
    return max(set(values), key=values.count)


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


def format_logs(all_logs):
    formatted_entries = dict()
    for log in all_logs:
        log_event_id = log[0].log_event_id
        if log_event_id not in formatted_entries:
            formatted_entries[log_event_id] = {"entries": {}}
        key = log[0].key + (f"/{log[0].version}" if log[0].version is not None else "")
        assert (
            key not in formatted_entries[log_event_id]
        ), f"found duplicates for key {key} with log_id {log_event_id}"
        formatted_entries[log_event_id]["ts"] = log[1].strftime("%Y-%m-%d %H:%M:%S")
        formatted_entries[log_event_id]["entries"][key] = json.loads(log[0].value)
    return formatted_entries

import re
from typing import List, Union


def _tokenize(s):
    token_specification = [
        ("NUMBER", r"\d+(\.\d*)?|\.\d+"),  # Integer or decimal number
        ("STRING", r"'([^'\\]*(?:\\.[^'\\]*)*)'|\"([^\"\\]*(?:\\.[^\"\\]*)*)\""),
        # String
        # Operators, note the order to match 'not in' before 'not' and 'in'
        ("OP", r"==|<=|>=|<|>|(?<!\w)(?:not in|in|not|and|or|is)(?!\w)"),
        ("FUNCTION", r"len"),  # Functions
        ("BOOLEAN", r"(?<!\w)(?:True|False)(?!\w)"),  # Booleans
        ("IDENTIFIER", r"[A-Za-z_][A-Za-z0-9_]*"),  # Identifiers
        ("LPAREN", r"\("),
        ("RPAREN", r"\)"),
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
            if value[0] == "'" or value[0] == '"':
                value = value[1:-1].encode("utf-8").decode("unicode_escape")
            tokens.append(("STRING", value))
        elif kind == "BOOLEAN":
            value = True if value == "True" else False
            tokens.append(("BOOLEAN", value))
        elif kind == "IDENTIFIER":
            tokens.append(("IDENTIFIER", value))
        elif kind == "FUNCTION":
            tokens.append(("FUNCTION", value))
        elif kind == "OP":
            tokens.append(("OP", value))
        elif kind == "LPAREN":
            tokens.append(("LPAREN", value))
        elif kind == "RPAREN":
            tokens.append(("RPAREN", value))
        elif kind == "SKIP":
            pass
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
        node = self.and_expr()
        while self.current_token[0] == "OP" and self.current_token[1] == "or":
            op = self.current_token[1]
            self.advance()
            right = self.and_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def and_expr(self):
        node = self.comp_expr()
        while self.current_token[0] == "OP" and self.current_token[1] == "and":
            op = self.current_token[1]
            self.advance()
            right = self.comp_expr()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

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
            "not",
        ):
            op = self.current_token[1]
            self.advance()
            # Handle 'not in' operator
            if (
                op == "not"
                and self.current_token[0] == "OP"
                and self.current_token[1] == "in"
            ):
                op = "not in"
                self.advance()
            right = self.primary()
            node = {"lhs": node, "operand": op, "rhs": right}
        return node

    def primary(self):
        if self.current_token[0] == "FUNCTION" and self.current_token[1] == "len":
            self.advance()
            if self.current_token[0] == "LPAREN":
                self.advance()
                expr = self.expr()
                if self.current_token[0] == "RPAREN":
                    self.advance()
                else:
                    raise RuntimeError('Expected ")" after len function')
                return {"operand": "len", "rhs": expr}
            else:
                raise RuntimeError('Expected "(" after len')
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
                    raise ValueError(f"Variable '{var_name}' not provided")
            elif expr["type"] == "string":
                return expr["value"]
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


def _reduce_mean(values: List[Union[int, float, bool]]) -> float:
    values = [v for v in values if v is not None]
    return sum(values) / len(values)


reduction_methods = {
    "mean": _reduce_mean,
}

"""Restricted decimal arithmetic for the public API."""

from decimal import Decimal, DecimalException, localcontext
import re

TOKEN = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+|[()+*/-]")


class Expression:
    def __init__(self, tokens):
        self.tokens = iter(tokens)
        self.current = next(self.tokens, None)

    def take(self):
        token = self.current
        self.current = next(self.tokens, None)
        return token

    def sum(self):
        left = self.product()
        while self.current in ("+", "-"):
            op = self.take()
            right = self.product()
            left = left + right if op == "+" else left - right
        return left

    def product(self):
        left = self.atom()
        while self.current in ("*", "/"):
            op = self.take()
            right = self.atom()
            left = left * right if op == "*" else left / right
        return left

    def atom(self):
        token = self.take()
        if token == "(":
            value = self.sum()
            if self.take() != ")":
                raise ValueError("Unclosed parentheses")
            return value
        if token in ("+", "-"):
            value = self.atom()
            return value if token == "+" else -value
        if token is None or token in ("*", "/", ")"):
            raise ValueError("Expected number")
        return Decimal(token)


def calculate(math):
    if not isinstance(math, str) or not 0 < len(math) <= 256:
        raise ValueError("Invalid expression length")
    tokens = []
    for match in re.finditer(r"\s+|" + TOKEN.pattern + r"|.", math):
        token = match.group()
        if token.isspace():
            continue
        if not TOKEN.fullmatch(token):
            raise ValueError("Invalid token")
        tokens.append(token)
    # Bound recursive depth and output size independently of the input bytes.
    if len(tokens) > 64:
        raise ValueError("Too many tokens")
    parser = Expression(tokens)
    with localcontext() as context:
        context.prec = 40
        context.Emax = 128
        context.Emin = -128
        try:
            value = parser.sum()
        except DecimalException as error:
            raise ValueError("Invalid arithmetic") from error
        if parser.current is not None:
            raise ValueError("Unexpected token")
        return format(value, "f")

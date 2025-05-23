"""Application-level exceptions shared across the code-base."""


class OutOfCreditError(RuntimeError):
    """Raised when a suspended / over-drawn account attempts a paid action."""

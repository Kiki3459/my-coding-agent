"""A deliberately small project used by the two-minute agent demo."""


def add(a: float, b: float) -> float:
    return a + b


def divide(a: float, b: float) -> float:
    """Return a divided by b."""
    if b == 0:
        raise ValueError("cannot divide by zero")
    return a / b


def percentage(part: float, whole: float) -> float:
    if whole == 0:
        raise ValueError("whole cannot be zero")
    return part / whole * 100


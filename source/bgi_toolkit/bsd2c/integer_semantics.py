"""Integer division used by the scenario VM (C truncation toward zero)."""


def trunc_div(left: int, right: int) -> int:
    quotient = abs(left) // abs(right)
    return -quotient if (left < 0) != (right < 0) else quotient


def trunc_mod(left: int, right: int) -> int:
    return left - trunc_div(left, right) * right

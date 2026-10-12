import re

_DIGITS = re.compile(r"([0-9]+)")


def natural_key(name: str) -> tuple:
    """Ключ сортировки, при котором числа в имени сравниваются как числа: 2 < 10."""
    # split по группе всегда даёт [текст, число, текст, ...], поэтому
    # на одинаковых позициях сравниваются значения одного типа.
    parts = _DIGITS.split(name.casefold())
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts)), name

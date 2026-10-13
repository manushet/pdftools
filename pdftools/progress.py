from collections.abc import Callable

# progress(done, total, label): сделано done из total; label — готовая подпись для пользователя.
# total == 0 означает «прогресс неизвестен» (бегущая полоса).
Progress = Callable[[int, int, str], None]


def no_progress(done: int, total: int, label: str) -> None:
    pass

from datetime import datetime

import pytest

from hoard_link.since import SINCE_HELP, resolve_since

NOW = datetime(2026, 9, 25, 14, 30).timestamp()  # a Friday


@pytest.mark.parametrize("text,seconds", [
    ("1h", 3600), ("30m", 1800), ("2 horas", 7200), ("hace 2 horas", 7200), ("2 hours ago", 7200),
    ("3d", 3 * 86400), ("hace 3 días", 3 * 86400), ("1w", 604800), ("2 weeks ago", 2 * 604800),
    ("1mo", 2592000), ("última hora", 3600), ("the last hour", 3600), ("1,5h", 5400),
    ("la semana pasada", 7 * 86400), ("last month", 30 * 86400),
])
def test_ages(text, seconds):
    assert resolve_since(text, NOW) == pytest.approx(NOW - seconds)


def test_words():
    at = lambda w: datetime.fromtimestamp(resolve_since(w, NOW))  # noqa: E731
    assert at("hoy") == at("today") == datetime(2026, 9, 25)
    assert at("ayer") == datetime(2026, 9, 24)
    assert at("esta mañana") == at("Esta Manana") == datetime(2026, 9, 25, 6)
    assert at("esta semana") == datetime(2026, 9, 21)
    assert at("este mes") == datetime(2026, 9, 1)


def test_epoch_iso_and_empty():
    assert resolve_since(1788000000, NOW) == 1788000000.0
    assert resolve_since("1788000000", NOW) == 1788000000.0
    assert datetime.fromtimestamp(resolve_since("2026-09-20T10:30", NOW)) == datetime(2026, 9, 20, 10, 30)
    assert datetime.fromtimestamp(resolve_since("2026-09-20", NOW)) == datetime(2026, 9, 20)
    assert resolve_since(None, NOW) is None and resolve_since("  ", NOW) is None


def test_unreadable_says_what_is_accepted():
    with pytest.raises(ValueError) as err:
        resolve_since("cuando sea", NOW)
    assert str(err.value) == SINCE_HELP

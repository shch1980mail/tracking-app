"""Проверяет, что скрипт импорта корректно парсит даты и не падает на null."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.import_json import _parse_date


def test_parse_date_ok():
    assert str(_parse_date("2026-10-20")) == "2026-10-20"


def test_parse_date_none():
    assert _parse_date(None) is None
    assert _parse_date("") is None

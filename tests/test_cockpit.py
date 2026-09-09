"""Тесты пульта наблюдателя.

Пульт — приватная страница, но правила те же, что у публичных: чужой
текст (артефакты и рекомендации родом с чужого сайта, имена из чужой
конфигурации) не имеет права ломать разметку, а отказ любого источника
не имеет права ронять сборку целиком.
"""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import balance

from tools.cockpit import render, ring_svg

WHEN = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def make_data(**overrides) -> dict:
    data = {
        "built_at": WHEN,
        "heartbeat": {"last_run": "2026-08-01T10:00:00Z", "runs_total": 4,
                      "last_model_probe": "2026-08-01T10:00:00Z",
                      "consecutive_source_failures": 0,
                      "consecutive_model_failures": 0},
        "coverage": {
            "covered": 19, "total": 45,
            "per_category": {"hooks": [2, 13], "skills": [9, 12]},
            "missing": [
                {"artifact": "/terminal-setup", "recommendation": "Включить Shift+Enter."},
            ],
        },
        "archive": [
            {"name": "a.html", "verdict": "no", "unconfirmed": ["одно"]},
            {"name": "b.html", "verdict": "maybe", "unconfirmed": []},
        ],
        "reactions": [],
        "reactions_offset": 128437435,
    }
    data.update(overrides)
    return data


def test_cockpit_renders_whole_and_balanced():
    page = render(make_data())
    assert balance(page).stack == []
    assert "19 из 45" in page
    assert "/terminal-setup" in page
    assert "2 разборов" in page


def test_ring_carries_the_share():
    svg = ring_svg(19, 45)
    assert "42%" in svg
    assert "19 из 45" in svg
    assert ring_svg(0, 0).count("0%") == 1, "пустой эталон не делит на ноль"


def test_hostile_text_cannot_break_the_markup():
    """Артефакты и рекомендации писала модель по чужому сайту."""
    data = make_data(coverage={
        "covered": 1, "total": 2,
        "per_category": {"hooks": [1, 2]},
        "missing": [{
            "artifact": '</div><script>alert(1)</script>',
            "recommendation": "до\x1b[31mпосле <b>жирный",
        }],
    })
    page = render(data)
    assert "<script>alert(1)" not in page
    assert "\x1b" not in page
    assert balance(page).stack == []


def test_every_source_degrades_independently():
    """Нет сети, нет эталона, пустой heartbeat — страница всё равно есть."""
    page = render(make_data(coverage=None, archive=None, heartbeat={},
                            reactions=[], reactions_offset=None))
    assert balance(page).stack == []
    assert "архив недоступен" in page
    assert "не собралось" in page
    assert "очередь ещё не читалась" in page


def test_attached_reactions_are_shown():
    page = render(make_data(reactions=[
        {"part": 16, "reactions": {"46": ["👍", "🔥"]}},
    ]))
    assert "👍 🔥" in page
    assert balance(page).stack == []

"""Тесты выемки настроек из советов (`tools/audit.py`).

Модель здесь не вызывается. Проверяется то, что стоит между её ответом и
эталоном: сверка цитат с исходником — единственная защита от выдуманной
рекомендации — и строгая схема ответа.
"""

from __future__ import annotations

from tools.audit import Audit, AuditItem, quote_matches
from watcher.analyze import strict_schema


def test_exact_quote_matches():
    assert quote_matches("use hooks to gate edits", "Always use hooks to gate edits.")


def test_whitespace_differences_do_not_break_the_match():
    assert quote_matches("use  hooks\nto gate", "…use hooks to gate…")


def test_typographic_quotes_match_straight_ones():
    assert quote_matches('set "shell" to powershell', "set “shell” to powershell")


def test_backticks_around_identifiers_are_ignored():
    assert quote_matches("add `PostToolUse` hook", "add PostToolUse hook")


def test_paraphrase_is_not_a_match():
    """Пересказ вместо цитаты — ровно то, что сверка обязана отвергнуть."""
    assert not quote_matches("configure the hook", "set up a PostToolUse gate")


def test_audit_schema_survives_strict_mode():
    """Строгий режим не принимает ряд ключевых слов JSON Schema.

    Тот же контракт, что у схемы разбора: у всех объектов
    additionalProperties=false и все поля required.
    """
    schema = strict_schema(Audit)
    forbidden = ("minLength", "maxLength", "pattern", "format", "minimum", "maximum")
    text = str(schema)
    for keyword in forbidden:
        assert keyword not in text, keyword

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


def test_audit_item_rejects_unknown_category():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AuditItem(
            part=1, bid="b-1", category="vibes",
            artifact="x", recommendation="y", quote="z",
        )

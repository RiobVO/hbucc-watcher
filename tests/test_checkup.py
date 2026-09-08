"""Тесты сверки конфигурации читателя с эталоном аудита.

Модель здесь не участвует: сверка — чистый код. Два принципа под тестами:

  * БЕЛЫЙ СПИСОК ЧТЕНИЯ. Рядом с конфигурацией лежат `.credentials.json`
    и история сессий — инструмент не имеет права их читать. Поверхность
    собирается по перечисленным именам, а не «всё подряд минус секреты».

  * ЧЕСТНОСТЬ ОТВЕТА. «Найдено» всегда называет файл, где нашлось, —
    подстрока в прозе CLAUDE.md и ключ в settings.json весят по-разному,
    и взвешивает это читатель, а не мы.
"""

from __future__ import annotations

import json

from tools.checkup import collect_surface, find_artifact, normalize_artifact


def test_artifact_normalization_drops_the_slash_and_case():
    assert normalize_artifact("/statusline") == "statusline"
    assert normalize_artifact("PostToolUse") == "posttooluse"
    assert normalize_artifact(" CLAUDE.md ") == "claude.md"


def test_artifact_is_found_as_a_json_key():
    surface = {"settings.json": json.dumps({"statusLine": {"type": "command"}})}
    assert find_artifact("/statusline", surface) == "settings.json"


def test_artifact_is_found_as_a_nested_json_key():
    surface = {"settings.json": json.dumps({"hooks": {"PostToolUse": []}})}
    assert find_artifact("PostToolUse", surface) == "settings.json"


def test_artifact_is_found_in_plain_text():
    surface = {"hooks/gate.mjs": "// PostToolUse gate for edits"}
    assert find_artifact("PostToolUse", surface) == "hooks/gate.mjs"


def test_artifact_is_found_by_file_name():
    surface = {"CLAUDE.md": "# правила"}
    assert find_artifact("CLAUDE.md", surface) == "CLAUDE.md"


def test_missing_artifact_reports_nothing():
    surface = {"settings.json": "{}", "CLAUDE.md": "текст"}
    assert find_artifact("PermissionRequest", surface) is None


def test_surface_reads_only_the_allowlist(tmp_path):
    """`.credentials.json` лежит рядом — и не читается никогда."""
    (tmp_path / "CLAUDE.md").write_text("правила", encoding="utf-8")
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".credentials.json").write_text("SECRET-VALUE", encoding="utf-8")
    (tmp_path / "history.jsonl").write_text("SECRET-TOO", encoding="utf-8")
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "gate.mjs").write_text("// PostToolUse", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "my-skill").mkdir()

    surface = collect_surface(tmp_path)

    joined = "\n".join([*surface.keys(), *surface.values()])
    assert "SECRET-VALUE" not in joined
    assert "SECRET-TOO" not in joined
    assert ".credentials.json" not in joined
    assert "history.jsonl" not in joined
    assert "CLAUDE.md" in surface
    assert "hooks/gate.mjs" in surface


def test_skills_and_commands_contribute_names_not_contents(tmp_path):
    """Факт наличия скилла — имя каталога; его текст сверке не нужен."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "pre-commit-check").mkdir()
    (tmp_path / "skills" / "pre-commit-check" / "SKILL.md").write_text(
        "секретов тут нет, но и читать незачем", encoding="utf-8"
    )
    (tmp_path / "commands").mkdir()
    (tmp_path / "commands" / "checkup.md").write_text("тело команды", encoding="utf-8")

    surface = collect_surface(tmp_path)

    assert find_artifact("pre-commit-check", surface) == "skills/"
    assert find_artifact("checkup", surface) == "commands/"
    assert "читать незачем" not in "\n".join(surface.values())
    assert "тело команды" not in "\n".join(surface.values())


def test_empty_home_is_an_empty_surface(tmp_path):
    assert collect_surface(tmp_path) == {}

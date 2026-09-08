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


# ------------------------------------------------- находки Codex-ревью


def test_symlink_in_place_of_config_is_not_followed(tmp_path):
    """Находка ревью: `settings.json -> .credentials.json` читал секреты.

    Белый список имён ничего не стоит, если имя — ссылка на чужой файл.
    """
    import os

    (tmp_path / ".credentials.json").write_text("SECRET-VALUE", encoding="utf-8")
    try:
        os.symlink(tmp_path / ".credentials.json", tmp_path / "settings.json")
    except OSError:
        import pytest
        pytest.skip("создание символических ссылок требует прав")
    surface = collect_surface(tmp_path)
    assert "SECRET-VALUE" not in "\n".join(surface.values())
    assert "settings.json" not in surface


def test_symlinked_hooks_dir_is_not_followed(tmp_path):
    """`hooks -> ~/.claude` заставил бы iterdir() прочитать всё подряд."""
    import os

    (tmp_path / ".credentials.json").write_text("SECRET-VALUE", encoding="utf-8")
    try:
        os.symlink(tmp_path, tmp_path / "hooks", target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("создание символических ссылок требует прав")
    surface = collect_surface(tmp_path)
    assert "SECRET-VALUE" not in "\n".join(surface.values())


def test_unreadable_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    """Залоченный файл на Windows — быт, а не повод для трейсбека."""
    from pathlib import Path

    (tmp_path / "CLAUDE.md").write_text("правила", encoding="utf-8")
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    original = Path.read_text

    def locked(self, *args, **kwargs):
        if self.name == "settings.json":
            raise PermissionError("файл занят другим процессом")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    surface = collect_surface(tmp_path)
    assert "CLAUDE.md" in surface
    assert "settings.json" not in surface


def test_corrupt_audit_json_is_a_clean_error_not_a_traceback(tmp_path):
    from tools.checkup import run

    (tmp_path / "CLAUDE.md").write_text("правила", encoding="utf-8")
    broken = tmp_path / "audit.json"
    broken.write_text("{обрезано", encoding="utf-8")
    assert run(tmp_path, audit_path=broken) == 1


def test_audit_entry_without_bid_does_not_crash_the_report(tmp_path):
    import json as jsonlib

    from tools.checkup import run

    (tmp_path / "CLAUDE.md").write_text("правила", encoding="utf-8")
    audit = tmp_path / "audit.json"
    audit.write_text(jsonlib.dumps({"items": [{}]}), encoding="utf-8")
    assert run(tmp_path, audit_path=audit) == 0


def test_control_characters_never_reach_the_report(tmp_path, caplog):
    """recommendation пишет модель по чужому сайту — терминал надо беречь."""
    import json as jsonlib
    import logging

    from tools.checkup import run

    (tmp_path / "CLAUDE.md").write_text("правила", encoding="utf-8")
    audit = tmp_path / "audit.json"
    audit.write_text(
        jsonlib.dumps(
            {"items": [{
                "part": 1, "bid": "b-1", "category": "other",
                "artifact": "нет-такого",
                "recommendation": "до\x1b[31mпосле\nвторая строка",
                "quote": "q",
            }]}
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO, logger="checkup"):
        assert run(tmp_path, audit_path=audit) == 0
    report = "\n".join(record.getMessage() for record in caplog.records)
    # ESC вырезан — остаток "[31m" без него просто текст, не команда
    # терминалу. Перевод строки стал пробелом: одна запись — одна строка.
    assert "\x1b" not in report
    assert "вторая строка" in report
    assert "до [31mпосле вторая строка" in report

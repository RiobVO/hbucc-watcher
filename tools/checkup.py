"""Сверка конфигурации читателя с эталоном аудита.

Вторая половина пункта 6: эталон (`state/audit.json`) знает, что сайт
рекомендует настроить, — этот инструмент смотрит, что из этого настроено
у читателя, и отвечает «у тебя не настроено вот это, это и это».

Модель не участвует: сверка — чистый код по файлам, на которые указали.

Два правила важнее полноты:

  * БЕЛЫЙ СПИСОК ЧТЕНИЯ. Рядом с конфигурацией лежат `.credentials.json`
    и история сессий. Поверхность собирается по перечисленным именам —
    «всё подряд минус секреты» здесь запрещено по построению.

  * НИЧЕГО НЕ ПИСАТЬ. Чужая конфигурация не попадает ни в git, ни в
    файлы проекта: отчёт уходит в stdout и больше никуда.

Запуск:  python tools/checkup.py                  по ~/.claude
         python tools/checkup.py --home <путь>    по указанному каталогу
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.config import STATE_DIR

log = logging.getLogger("checkup")

# Файлы конфигурации, чьё СОДЕРЖИМОЕ участвует в сверке.
_CONTENT_FILES = (
    "CLAUDE.md",
    "settings.json",
    "settings.local.json",
    "keybindings.json",
    ".mcp.json",
)
# Каталоги, где след — само СОДЕРЖИМОЕ файлов (хуки — это код с именами
# событий внутри).
_CONTENT_DIRS = ("hooks",)
# Каталоги, где след — только ИМЯ: текст скилла или команды сверке не
# нужен, и читать его незачем.
_NAME_DIRS = ("skills", "commands", "agents")


def normalize_artifact(artifact: str) -> str:
    """Артефакт к виду для поиска: без ведущего слэша, в нижнем регистре."""
    return artifact.strip().lstrip("/").lower()


def _json_keys(node: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key).lower())
            keys.update(_json_keys(value))
    elif isinstance(node, list):
        for value in node:
            keys.update(_json_keys(value))
    return keys


def find_artifact(artifact: str, surface: dict[str, str]) -> str | None:
    """Где в поверхности есть след артефакта. Нет следа — None.

    JSON ищется по ключам, а не подстрокой: ключ `statusLine` — настройка,
    а то же слово в прозе — просто слово. Для не-JSON — подстрока, и
    ответ всегда называет файл: взвешивает находку читатель.
    """
    needle = normalize_artifact(artifact)
    if not needle:
        return None
    for path, content in surface.items():
        if needle in Path(path).name.lower():
            return path
        if path.endswith(".json"):
            try:
                if needle in _json_keys(json.loads(content)):
                    return path
                continue
            except json.JSONDecodeError:
                log.debug("не разбирается как JSON, ищу подстрокой: %s", path)
        if needle in content.lower():
            return path
    return None


def _is_link(path: Path) -> bool:
    """Симлинк или junction. Находка ревью: белый список имён ничего не
    стоит, если имя — ссылка на чужой файл (`settings.json ->
    .credentials.json` читал бы секреты, а `hooks -> ~/.claude` заставил
    бы перечисление прочитать всё подряд). Junction на Windows создаётся
    без прав администратора, поэтому проверяются оба вида."""
    return path.is_symlink() or path.is_junction()


def _safe_read(path: Path) -> str | None:
    """Содержимое файла, если его можно и допустимо читать.

    Залоченный файл на Windows — быт, а не повод ронять сверку: он
    пропускается со строкой в логе. Ссылки не читаются вовсе. Даже
    `is_file()` внутри try: stat() под deny-ACL пропагирует
    PermissionError, а не возвращает False.
    """
    try:
        if _is_link(path) or not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("не читается, пропускаю: %s (%s)", _clean(path.name), _clean(exc))
        return None


def _usable_dir(folder: Path) -> bool:
    """Каталог, в который допустимо заглядывать: не ссылка и stat жив."""
    try:
        return folder.is_dir() and not _is_link(folder)
    except OSError as exc:
        log.warning("каталог недоступен, пропускаю: %s (%s)", _clean(folder.name), _clean(exc))
        return False


def collect_surface(home: Path) -> dict[str, str]:
    """Поверхность конфигурации: путь → содержимое (или пусто для имён).

    Читается ТОЛЬКО перечисленное. Имена из _NAME_DIRS попадают в ключ
    вида "skills/", а их содержимое — никогда.
    """
    surface: dict[str, str] = {}
    for name in _CONTENT_FILES:
        content = _safe_read(home / name)
        if content is not None:
            surface[name] = content
    for dirname in _CONTENT_DIRS:
        folder = home / dirname
        if not _usable_dir(folder):
            continue
        try:
            children = sorted(folder.iterdir())
        except OSError as exc:
            log.warning("каталог не перечисляется, пропускаю: %s (%s)", dirname, _clean(exc))
            continue
        for path in children:
            content = _safe_read(path)
            if content is not None:
                surface[f"{dirname}/{path.name}"] = content
    for dirname in _NAME_DIRS:
        folder = home / dirname
        if not _usable_dir(folder):
            continue
        try:
            names = sorted(item.name for item in folder.iterdir())
        except OSError as exc:
            log.warning("каталог не перечисляется, пропускаю: %s (%s)", dirname, _clean(exc))
            continue
        if names:
            surface[f"{dirname}/"] = "\n".join(names)
    return surface


# В отчёт попадает текст, который модель писала по чужому сайту: терминал
# надо беречь от управляющих последовательностей так же, как страницу — от
# угловых скобок. Печатаемые символы и пробел; переводы строк — в пробел.
def _clean(value: object) -> str:
    return "".join(
        ch if ch.isprintable() else " " for ch in str(value)
    ).strip()


def run(home: Path, audit_path: Path | None = None) -> int:
    audit_path = audit_path or STATE_DIR / "audit.json"
    if not audit_path.is_file():
        log.error("эталона нет: %s — сначала python tools/audit.py --run", audit_path)
        return 1
    # Эталон мог быть перезаписан руками: битый JSON — внятная строка и
    # код 1, не трейсбек. Тот же контракт, что у состояния наблюдателя.
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("эталон не читается: %s — %s", audit_path, exc)
        return 1
    raw_items = audit.get("items") if isinstance(audit, dict) else None
    if not isinstance(raw_items, list):
        log.error("эталон неожиданной формы: %s — нет списка items", audit_path)
        return 1
    items = [item for item in raw_items if isinstance(item, dict)]
    surface = collect_surface(home)
    if not surface:
        log.error("в %s не нашлось ни одного файла конфигурации", home)
        return 1
    log.info("конфигурация: %s — файлов в поверхности %d", home, len(surface))
    log.info("эталон: %d записей от %s\n", len(items), _clean(audit.get("generated_at", "?")))

    found: list[tuple[dict, str]] = []
    missing: list[dict] = []
    for item in items:
        where = find_artifact(str(item.get("artifact", "")), surface)
        if where:
            found.append((item, where))
        else:
            missing.append(item)

    covered_bids = {item.get("bid") for item, _ in found}
    all_bids = {item.get("bid") for item in items}
    log.info(
        "НАСТРОЕНО: %d записей из %d (советов покрыто %d из %d)",
        len(found), len(items), len(covered_bids), len(all_bids),
    )
    for item, where in found:
        log.info("  + %-28s -> %s", _clean(item.get("artifact", "")), _clean(where))

    if missing:
        log.info("\nНЕ НАСТРОЕНО: %d записей", len(missing))
        for item in missing:
            log.info(
                "  - Part %-3s %-28s %s",
                _clean(item.get("part", "?")), _clean(item.get("artifact", "")),
                _clean(item.get("recommendation", "")),
            )
    else:
        log.info("\nНЕ НАСТРОЕНО: ничего — эталон покрыт целиком")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        default=os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".claude"),
        help="каталог конфигурации Claude Code (по умолчанию ~/.claude)",
    )
    sys.exit(run(Path(args := parser.parse_args().home)))

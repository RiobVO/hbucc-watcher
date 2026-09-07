"""Аудит настройки: что сайт вообще рекомендует настроить.

Один проход по всем советам снапшота на дешёвой модели даёт эталон:
хуки, CLAUDE.md, settings.json, режимы разрешений и прочее, что оставляет
след в конфигурации. Дальше против эталона можно сверять и собственные
файлы, и каждый новый совет.

Выемка — работа механическая, и её ошибка проверяема: каждая запись несёт
дословную цитату, которую этот же скрипт сверяет с исходником. Несовпавшая
цитата не выбрасывается молча, а откладывается в `unverified` — глазам.

Запуск:  python tools/audit.py            оценка входа, без модели
         python tools/audit.py --run      платный вызов, ~$0.02-0.03

Результат: state/audit.json. Сырой ответ модели сохраняется на диск ДО
валидации: упавший разбор ответа не имеет права терять платный вызов.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field, ValidationError

from watcher.analyze import fence, strict_schema
from watcher.config import PROMPTS_DIR, ROOT, STATE_DIR
from watcher.state import Snapshot

log = logging.getLogger("audit")

AUDIT_PATH = STATE_DIR / "audit.json"
RAW_PATH = STATE_DIR / "audit_raw.txt"

# Выемка, а не рассуждение: reasoning выше medium здесь покупает токены,
# но не точность — точность делает сверка цитат ниже.
EFFORT = "medium"
MAX_OUTPUT_TOKENS = 16000

Category = Literal[
    "hooks",
    "claude_md",
    "settings_json",
    "permission_modes",
    "skills",
    "slash_commands",
    "mcp",
    "env",
    "other",
]


class AuditItem(BaseModel):
    part: int = Field(description="Номер части, как указан в материале.")
    bid: str = Field(description="Идентификатор совета, как указан в материале.")
    category: Category
    artifact: str = Field(description="Конкретное имя: ключ, файл, команда, хук — как в совете.")
    recommendation: str = Field(description="Что именно совет предлагает настроить, одна строка по-русски.")
    quote: str = Field(description="Дословная подстрока из текста совета, 5–15 слов.")


class Audit(BaseModel):
    items: list[AuditItem]


def _canon(text: str) -> str:
    """Нормализация для сверки цитат: пробелы, кавычки, обратные кавычки.

    Модель ставит прямые кавычки вместо типографских и оборачивает
    идентификаторы в обратные — это не делает цитату пересказом.
    """
    for src, dst in ("`", ""), ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"):
        text = text.replace(src, dst)
    return " ".join(text.split())


def quote_matches(quote: str, advice_text: str) -> bool:
    """Дословная ли цитата. Пересказ — нет."""
    return _canon(quote) in _canon(advice_text)


def _advice_corpus(snap: Snapshot) -> tuple[str, dict[str, str]]:
    """Все советы одним недоверенным блоком плюс индекс bid → текст."""
    lines: list[str] = ['<untrusted_source note="советы сайта, данные для выемки">']
    texts: dict[str, str] = {}
    for part in sorted(snap.parts, key=lambda p: p.number):
        for block in part.tips:
            texts[block.bid] = f"{block.heading} {block.text}"
            lines.append(fence(f"--- Part {part.number} · {block.bid} · {block.heading} ---"))
            lines.append(fence(block.text))
            lines.append("")
    lines.append("</untrusted_source>")
    return "\n".join(lines), texts


def run(*, live: bool) -> int:
    snap = Snapshot.from_dict(
        json.loads((STATE_DIR / "snapshot.json").read_text(encoding="utf-8"))
    )
    corpus, texts = _advice_corpus(snap)
    system_prompt = (PROMPTS_DIR / "audit.md").read_text(encoding="utf-8")

    est_input = (len(corpus) + len(system_prompt)) // 3
    log.info("советов: %d, вход ~%d токенов, поисков 0", len(texts), est_input)
    if not live:
        log.info("сухой прогон: модель не вызывалась. Запуск: python tools/audit.py --run")
        return 0

    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log.error("OPENAI_API_KEY не задан — локально он лежит в .env")
        return 1

    client = OpenAI(api_key=api_key, max_retries=2)
    response = client.responses.create(
        model="gpt-5.6-luna",
        instructions=system_prompt,
        input=corpus,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        reasoning={"effort": EFFORT},
        text={
            "format": {
                "type": "json_schema",
                "name": "audit",
                "strict": True,
                "schema": strict_schema(Audit),
            }
        },
    )

    # Диск раньше валидации: платный ответ не теряется из-за упавшего разбора.
    RAW_PATH.write_text(response.output_text or "", encoding="utf-8")
    usage_in = getattr(response.usage, "input_tokens", 0) or 0
    usage_out = getattr(response.usage, "output_tokens", 0) or 0
    log.info("ответ получен: вход=%d выход=%d, сырой текст в %s", usage_in, usage_out, RAW_PATH)

    if response.status != "completed":
        log.error("ответ не завершён: %s", getattr(response, "incomplete_details", None))
        return 1

    try:
        audit = Audit.model_validate_json(response.output_text)
    except ValidationError as exc:
        log.error("ответ не прошёл схему, сырой текст сохранён: %s", exc)
        return 1

    verified: list[dict] = []
    unverified: list[dict] = []
    for item in audit.items:
        row = item.model_dump()
        source = texts.get(item.bid)
        if source is not None and quote_matches(item.quote, source):
            verified.append(row)
        else:
            row["reason"] = "bid не найден" if source is None else "цитата не сошлась"
            unverified.append(row)

    AUDIT_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": "gpt-5.6-luna",
                "advice_total": len(texts),
                "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
                "items": verified,
                "unverified": unverified,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    by_category: dict[str, int] = {}
    for row in verified:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
    log.info(
        "эталон записан: %s — записей %d (сверено), отложено %d",
        AUDIT_PATH, len(verified), len(unverified),
    )
    for category, count in sorted(by_category.items(), key=lambda kv: -kv[1]):
        log.info("  %-16s %d", category, count)
    for row in unverified:
        log.info("  не сверено: %s %s — %s", row["bid"], row["artifact"], row["reason"])
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="платный вызов модели")
    args = parser.parse_args()
    if args.run and not os.environ.get("OPENAI_API_KEY"):
        # Локальный запуск: ключ из .env, кодом и без печати значения.
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
    sys.exit(run(live=args.run))

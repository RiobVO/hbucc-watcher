"""Пульт наблюдателя: весь организм системы на одном локальном экране.

Одна команда — одна страница: здоровье прогонов из heartbeat, покрытие
твоей конфигурации против эталона сайта (кольцо и полоски по категориям),
чеклист «что докрутить», сводка публичного архива и реакции из журнала.

Всё собирается из уже существующих данных: state/, ~/.claude и один GET
публичного архива. Модель не вызывается, счёт не растёт.

Страница ПРИВАТНАЯ: на ней факты о твоей конфигурации. Пишется в корень
проекта (cockpit.html, стоит в .gitignore) и никуда не публикуется.
Каждый блок деградирует сам: нет сети — архивный блок честно говорит об
этом, остальные живут.

Запуск:  python tools/cockpit.py           собрать страницу
         python tools/cockpit.py --open    собрать и открыть в браузере
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from tools.checkup import _clean, collect_surface, find_artifact
from watcher.config import ROOT, STATE_DIR
from watcher.publish import index_entries

log = logging.getLogger("cockpit")

OUT_PATH = ROOT / "cockpit.html"
ARCHIVE_URL = "https://riobvo.github.io/hbucc-reports/index.html"
CSS_PATH = ROOT / "templates" / "report.css"

# Категории эталона — русские подписи и порядок показа.
CATEGORY_RU = (
    ("hooks", "хуки"),
    ("settings_json", "settings.json"),
    ("skills", "скиллы"),
    ("slash_commands", "команды"),
    ("permission_modes", "разрешения"),
    ("claude_md", "CLAUDE.md"),
    ("mcp", "MCP"),
    ("env", "окружение"),
    ("other", "прочее"),
)

_EXTRA_CSS = """
/* --- пульт: добавка к вёрстке проекта --- */
.cockpit-grid { display: grid; grid-template-columns: 180px 1fr; gap: 24px; align-items: center;
  background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; }
.ring-wrap { text-align: center; }
.ring-label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: .05em; margin-top: 6px; }
.ring-value { font-size: 26px; font-weight: 700; fill: #0f172a; }
.ring-sub { font-size: 11px; fill: #64748b; }
.checklist { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 4px 16px; margin-bottom: 24px; }
.check-item { display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid #f1f5f9; align-items: baseline; }
.check-item:last-child { border-bottom: none; }
.check-artifact { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #1e40af;
  background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 4px; padding: 1px 7px; flex: none; }
.check-why { font-size: 13px; color: #475569; }
.check-more { font-size: 12px; color: #94a3b8; padding: 10px 0; }
.health { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 24px; }
.health .card { margin-bottom: 0; }
.health .card p { font-size: 14px; font-weight: 600; color: #0f172a; }
@media (max-width: 640px) { .cockpit-grid { grid-template-columns: 1fr; } }
"""


def ring_svg(covered: int, total: int) -> str:
    """Кольцо покрытия. Дуга — доля закрытых советов, цифра — в центре."""
    pct = 0 if not total else round(covered / total * 100)
    radius, circumference = 62, 389.6  # 2πr при r=62
    filled = circumference * pct / 100
    return f"""<svg width="150" height="150" viewBox="0 0 150 150" role="img"
  aria-label="покрыто {covered} из {total}">
  <circle cx="75" cy="75" r="{radius}" fill="none" stroke="#f1f5f9" stroke-width="12"/>
  <circle cx="75" cy="75" r="{radius}" fill="none" stroke="#1e40af" stroke-width="12"
    stroke-linecap="round" stroke-dasharray="{filled:.1f} {circumference}"
    transform="rotate(-90 75 75)"/>
  <text x="75" y="72" text-anchor="middle" class="ring-value">{pct}%</text>
  <text x="75" y="92" text-anchor="middle" class="ring-sub">{covered} из {total}</text>
</svg>"""


def esc(value: object) -> str:
    """Всё чужое — и текст модели, и имена из конфигурации — через одно горло."""
    return html.escape(_clean(value), quote=False)


def _bar_rows(rows: list[tuple[str, int, int]]) -> str:
    """Полоски «закрыто/всего» по категориям. Пустые категории не рисуются."""
    peak = max((total for _, _, total in rows), default=0)
    out = []
    for label, covered, total in rows:
        if not total or not peak:
            continue
        tone = "good" if covered == total else ("warn" if covered else "muted")
        out.append(
            f'<div class="bar-row"><span class="bar-label">{esc(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill {tone}" '
            f'style="width:{round(total / peak * 100)}%"></span></span>'
            f'<span class="bar-value">{covered}/{total}</span></div>'
        )
    return "\n".join(out)


def _ago(stamp: str) -> str:
    """«3 ч назад» из ISO-метки. Не разобралось — метка как есть."""
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp or "—"
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    if hours < 1:
        return "меньше часа назад"
    if hours < 48:
        return f"{round(hours)} ч назад"
    return f"{round(hours / 24)} дн назад"


def gather() -> dict:
    """Собрать данные пульта. Каждый источник падает независимо."""
    data: dict = {"built_at": datetime.now().astimezone()}

    heartbeat_path = STATE_DIR / "heartbeat.json"
    try:
        data["heartbeat"] = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("heartbeat не читается: %s", exc)
        data["heartbeat"] = {}

    try:
        ledger = json.loads((STATE_DIR / "delivered.json").read_text(encoding="utf-8"))
        entries = ledger.get("delivered") or []
        data["reactions"] = [e for e in entries if isinstance(e, dict) and e.get("reactions")]
        data["reactions_offset"] = ledger.get("reactions_offset")
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("журнал не читается: %s", exc)
        data["reactions"], data["reactions_offset"] = [], None

    # Живая сверка конфигурации: тот же код, что в tools/checkup.py.
    data["coverage"] = None
    try:
        audit = json.loads((STATE_DIR / "audit.json").read_text(encoding="utf-8"))
        items = [i for i in (audit.get("items") or []) if isinstance(i, dict)]
        home = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".claude"
        surface = collect_surface(home)
        if items and surface:
            per_cat: dict[str, list[int]] = {}
            missing: list[dict] = []
            found_bids, all_bids = set(), set()
            for item in items:
                all_bids.add(item.get("bid"))
                cat = per_cat.setdefault(str(item.get("category", "other")), [0, 0])
                cat[1] += 1
                if find_artifact(str(item.get("artifact", "")), surface):
                    cat[0] += 1
                    found_bids.add(item.get("bid"))
                else:
                    missing.append(item)
            data["coverage"] = {
                "covered": len(found_bids), "total": len(all_bids),
                "per_category": per_cat, "missing": missing,
            }
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("сверка не собралась: %s", exc)

    # Публичный архив: один GET, отказ сети — не отказ пульта.
    data["archive"] = None
    try:
        response = httpx.get(ARCHIVE_URL, timeout=5.0, follow_redirects=True)
        if response.status_code == 200:
            data["archive"] = index_entries(response.text)
    except httpx.HTTPError as exc:
        log.warning("архив недоступен: %s", exc)

    return data


def render(data: dict) -> str:
    """Страница пульта целиком, в вёрстке проекта."""
    heartbeat = data.get("heartbeat") or {}
    parts: list[str] = []

    # --- здоровье ---
    probe = heartbeat.get("last_model_probe", "")
    health_cards = (
        ("последний прогон", _ago(heartbeat.get("last_run", ""))),
        ("прогонов всего", str(heartbeat.get("runs_total", "—"))),
        ("проба модели", _ago(probe) if probe else "ещё не было"),
        ("отказов подряд", str(
            (heartbeat.get("consecutive_source_failures") or 0)
            + (heartbeat.get("consecutive_model_failures") or 0)
        )),
    )
    parts.append('<div class="health">' + "".join(
        f'<div class="card"><div class="card-head">{esc(label)}</div><p>{esc(value)}</p></div>'
        for label, value in health_cards
    ) + "</div>")

    # --- покрытие конфигурации ---
    coverage = data.get("coverage")
    if coverage:
        rows = [
            (ru, *(coverage["per_category"].get(key) or (0, 0)))
            for key, ru in CATEGORY_RU
        ]
        parts.append("<h2>Твоя конфигурация против сайта</h2>")
        parts.append(
            '<div class="cockpit-grid"><div class="ring-wrap">'
            + ring_svg(coverage["covered"], coverage["total"])
            + '<div class="ring-label">советов покрыто</div></div>'
            + "<div>" + _bar_rows(rows) + "</div></div>"
        )
        missing = coverage["missing"]
        if missing:
            top = "".join(
                '<div class="check-item">'
                f'<span class="check-artifact">{esc(item.get("artifact", ""))}</span>'
                f'<span class="check-why">{esc(item.get("recommendation", ""))}</span>'
                "</div>"
                for item in missing[:6]
            )
            more = (
                f'<div class="check-more">…и ещё {len(missing) - 6} — '
                "полный список: <code>python tools/checkup.py</code></div>"
                if len(missing) > 6 else ""
            )
            parts.append("<h2>Докрутить в первую очередь</h2>")
            parts.append(f'<div class="checklist">{top}{more}</div>')
    else:
        parts.append(
            '<div class="card"><div class="card-head">покрытие конфигурации</div>'
            "<p>не собралось: нужен эталон (python tools/audit.py --run) "
            "и каталог ~/.claude</p></div>"
        )

    # --- архив ---
    archive = data.get("archive")
    parts.append("<h2>Публичный архив</h2>")
    if archive is not None:
        claims = sum(len(e.get("unconfirmed") or ()) for e in archive)
        verdict_rows: dict[str, int] = {}
        for entry in archive:
            verdict_rows[str(entry.get("verdict", "?"))] = (
                verdict_rows.get(str(entry.get("verdict", "?")), 0) + 1
            )
        labels = {"yes": "стоит времени", "maybe": "смотря по чему", "no": "пропущено осознанно"}
        breakdown = " · ".join(
            f"{labels.get(key, key)}: {count}" for key, count in sorted(verdict_rows.items())
        )
        parts.append(
            f'<div class="card"><div class="card-head">'
            f'<a href="{ARCHIVE_URL}">{len(archive)} разборов</a> · '
            f"расхождений с документацией: {claims}</div>"
            f"<p>{esc(breakdown)}</p></div>"
        )
    else:
        parts.append(
            '<div class="card"><div class="card-head">архив недоступен</div>'
            "<p>сеть не ответила — блок вернётся при следующей сборке</p></div>"
        )

    # --- реакции ---
    parts.append("<h2>Реакции читателя</h2>")
    reactions = data.get("reactions") or []
    if reactions:
        rows = "".join(
            '<div class="check-item">'
            f'<span class="check-artifact">Part {esc(entry.get("part", "?"))}</span>'
            f'<span class="check-why">{esc(" ".join(sum(entry["reactions"].values(), [])))}'
            "</span></div>"
            for entry in reactions
        )
        parts.append(f'<div class="checklist">{rows}</div>')
    else:
        offset = data.get("reactions_offset")
        state = f"курсор очереди живой: {offset}" if offset else "очередь ещё не читалась"
        parts.append(
            f'<div class="card"><p>привязанных реакций пока нет — они появятся '
            f"с первым событием, доставленным конвейером. {esc(state)}</p></div>"
        )

    when = data["built_at"]
    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Пульт наблюдателя</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
{CSS_PATH.read_text(encoding="utf-8")}
{_EXTRA_CSS}</style>
</head>
<body>
<div class="container">
  <div class="eyebrow"><span>пульт наблюдателя · приватная страница</span></div>
  <h1>Наблюдатель за howborisusesclaudecode.com</h1>
  <p class="subtitle">Собрано {when:%d.%m.%Y в %H:%M} · данные: state/, ~/.claude, публичный архив</p>

{body}

  <footer>
    <span>пересборка: <code>python tools/cockpit.py --open</code></span>
    <span>страница локальная, в git не попадает</span>
  </footer>
</div>
</body>
</html>"""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true", help="открыть в браузере")
    args = parser.parse_args()
    OUT_PATH.write_text(render(gather()), encoding="utf-8")
    log.info("пульт собран: %s", OUT_PATH)
    if args.open:
        os.startfile(OUT_PATH)  # noqa: S606 - локальный файл по явной просьбе

"""Рендер разбора в текст и доставка в Telegram.

Здесь заканчивается путь недоверенных данных: в Telegram уходят только
поля валидированной схемы Analysis, отрендеренные этим модулем. Сырой HTML
сайта сюда не попадает физически — его нет в объекте разбора.

Два неочевидных ограничения формата, оба вынужденные:

  1. parse_mode=HTML, а не MarkdownV2. Экранировать надо три символа
     вместо четырнадцати. Текст пишет модель по материалам чужого сайта —
     чем меньше символов способны сломать сообщение, тем лучше.

  2. HTML-теги допускаются ТОЛЬКО на коротких строках-заголовках, тело
     разбора идёт без разметки. Это позволяет резать сообщение по границам
     строк и гарантированно не разорвать тег пополам. Разорванный тег
     Telegram отвергает целиком — то есть разбор просто не доходит.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Iterable

import httpx

from watcher.analyze import Analysis
from watcher.detect import Event

log = logging.getLogger(__name__)

TELEGRAM_HARD_LIMIT = 4096

_WINDOWS_LABEL = {
    "works": "работает",
    "macos_only": "только macOS",
    "needs_adaptation": "работает с оговорками",
    "unconfirmed": "не удалось выяснить",
}

_VERDICT_LABEL = {
    "yes": "да, стоит",
    "no": "нет, пропускай",
    "maybe": "смотря по обстоятельствам",
}


class DeliveryFailed(RuntimeError):
    """Сообщение не доставлено. Событие не попадает в журнал."""


def esc(text: str) -> str:
    """Экранировать текст для parse_mode=HTML."""
    return html.escape(text, quote=False)


def render(analysis: Analysis, event: Event) -> str:
    """Собрать текст разбора.

    Порядок секций и подписи — здесь. Менять тон и объём можно правкой
    промтов в prompts/, а порядок и оформление — правкой этой функции.
    Ни то, ни другое не требует трогать обнаружение, состояние или доставку.
    """
    lines: list[str] = []

    lines.append(f"<b>{esc(analysis.headline)}</b>")
    lines.append(esc(event.headline))
    lines.append("")

    lines.append("<b>ЧТО ЭТО</b>")
    lines.append(esc(analysis.what_it_is))
    lines.append("")

    lines.append("<b>КАК РАБОТАЕТ ПО СУТИ</b>")
    lines.append(esc(analysis.how_it_works))
    lines.append("")

    lines.append("<b>СЛОИ ДОСТОВЕРНОСТИ</b>")
    lines.append("Автор оригинала:")
    lines.append(esc(analysis.layers.original_author))
    lines.append("")
    lines.append("Автор фан-сайта дописал:")
    lines.append(esc(analysis.layers.site_author))
    lines.append("")
    lines.append("Официальная документация:")
    lines.append(esc(analysis.layers.official_docs))
    lines.append("")
    lines.append("Мой вывод:")
    lines.append(esc(analysis.layers.my_conclusion))
    lines.append("")

    lines.append(f"<b>WINDOWS: {esc(_WINDOWS_LABEL[analysis.windows.status])}</b>")
    lines.append(esc(analysis.windows.detail))
    lines.append("")

    lines.append(f"<b>СТОИТ ЛИ ТЕБЕ: {esc(_VERDICT_LABEL[analysis.verdict.worth_it])}</b>")
    lines.append(esc(analysis.verdict.why))
    lines.append("")

    if analysis.action.strip():
        lines.append("<b>ЧТО СДЕЛАТЬ</b>")
        lines.append(esc(analysis.action))
        lines.append("")

    if analysis.unconfirmed:
        lines.append("<b>НЕ ПОДТВЕРЖДЕНО ДОКУМЕНТАЦИЕЙ</b>")
        lines.extend(f"- {esc(item)}" for item in analysis.unconfirmed)
        lines.append("")

    if analysis.anomalies:
        # Аномалии идут отдельной секцией и намеренно заметны: это найденные
        # в чужом тексте обращения к агенту. Читатель должен видеть, что
        # материал пытался управлять системой, а не только сам разбор.
        lines.append("<b>АНОМАЛИИ В ИСХОДНОМ ТЕКСТЕ</b>")
        lines.append("В разбираемом материале найдены инструкции, обращённые к агенту.")
        lines.append("Они процитированы как факт и не исполнялись:")
        lines.extend(f"- {esc(item)}" for item in analysis.anomalies)
        lines.append("")

    if analysis.sources:
        lines.append("<b>ИСТОЧНИКИ</b>")
        lines.extend(esc(url) for url in analysis.sources)

    return "\n".join(lines).strip()


def chunk(text: str, limit: int) -> list[str]:
    """Разбить сообщение на части, не разрывая строки и теги.

    Режем строго по границам строк. Поскольку теги в render() живут только
    на коротких строках-заголовках, строка не может содержать незакрытый
    тег, а значит и часть сообщения не может.

    Место под префикс «[2/3] » резервируется заранее: иначе на границе
    лимита префикс вытолкнул бы сообщение за 4096 символов уже после того,
    как разбиение посчитано.
    """
    if limit >= TELEGRAM_HARD_LIMIT:
        raise ValueError(f"limit={limit} не оставляет запаса под лимит Telegram")

    prefix_room = 12
    budget = limit - prefix_room
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in text.split("\n"):
        # Одиночная строка длиннее бюджета — режем по словам. На практике
        # не встречается (модель пишет абзацами), но длинная ссылка или
        # слипшийся текст не должны ронять доставку.
        pieces = [line] if len(line) <= budget else _split_long(line, budget)
        for piece in pieces:
            addition = len(piece) + (1 if current else 0)
            if size + addition > budget and current:
                chunks.append("\n".join(current))
                current, size = [piece], len(piece)
            else:
                current.append(piece)
                size += addition

    if current:
        chunks.append("\n".join(current))
    if not chunks:
        return []
    if len(chunks) == 1:
        return chunks
    return [f"[{i}/{len(chunks)}]\n{c}" for i, c in enumerate(chunks, 1)]


def _split_long(line: str, budget: int) -> list[str]:
    out: list[str] = []
    rest = line
    while len(rest) > budget:
        cut = rest.rfind(" ", 0, budget)
        if cut <= 0:
            cut = budget
        out.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def send_message(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    parse_mode: str = "HTML",
    retries: int = 3,
) -> None:
    """Отправить одно сообщение. Бросает DeliveryFailed."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        # Превью подтянуло бы карточку x.com на пол-экрана под каждым разбором.
        "link_preview_options": {"is_disabled": True},
    }

    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
                response = client.post(url, json=payload)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code == 429:
                # Telegram сам говорит, сколько ждать.
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram: rate limit, пауза %s с", retry_after)
                time.sleep(float(retry_after))
                continue
            if response.status_code < 500:
                # 400 обычно означает сломанную разметку — ретрай не поможет.
                raise DeliveryFailed(f"Telegram отверг сообщение: {last_error}")
        except httpx.HTTPError as exc:
            last_error = str(exc)

        if attempt < retries:
            time.sleep(2 * attempt)

    raise DeliveryFailed(f"не доставлено за {retries} попыток: {last_error}")


def deliver(
    text: str, *, bot_token: str, chat_id: str, chunk_chars: int, parse_mode: str = "HTML"
) -> int:
    """Отправить разбор целиком, разбив на части. Возвращает число сообщений.

    Критерий «разбор длиннее лимита Telegram доходит целиком» выполняется
    здесь. Части отправляются последовательно с паузой: Telegram
    ограничивает частоту, и вторая часть, отправленная мгновенно, может
    прийти раньше первой.
    """
    parts = chunk(text, chunk_chars)
    for i, part in enumerate(parts):
        if i:
            time.sleep(0.5)
        send_message(part, bot_token=bot_token, chat_id=chat_id, parse_mode=parse_mode)
    log.info("доставлено сообщений: %d", len(parts))
    return len(parts)


def send_alert(lines: Iterable[str], *, bot_token: str, chat_id: str) -> None:
    """Сообщить о сбое.

    Отдельная функция и намеренно без parse_mode: в алерт попадают
    технические строки с угловыми скобками, путями и обрывками HTML.
    Разметка тут только мешала бы, а сломанный тег превратил бы сообщение
    о сбое в ещё один сбой.
    """
    body = "\n".join(str(line) for line in lines)
    text = f"СБОЙ НАБЛЮДАТЕЛЯ\n\n{body}"[: TELEGRAM_HARD_LIMIT - 10]
    try:
        send_message(text, bot_token=bot_token, chat_id=chat_id, parse_mode="")
    except DeliveryFailed:
        # Единственный канал недоступен. Внешний watchdog заметит отсутствие
        # пинга — ради этого он и существует.
        log.exception("не удалось доставить алерт")

"""Рендер разбора в текст и доставка в Telegram.

Здесь заканчивается путь недоверенных данных: в Telegram уходят только
поля валидированной схемы Analysis, отрендеренные этим модулем. Сырой HTML
сайта сюда не попадает физически — его нет в объекте разбора.

Три ограничения формата, все вынужденные:

  1. parse_mode=HTML, а не MarkdownV2. Экранировать надо три символа
     вместо четырнадцати. Текст пишет модель по материалам чужого сайта —
     чем меньше символов способны сломать сообщение, тем лучше.

  2. СТРОЧНЫЕ теги (<b>, <i>, <a>, <code>) не пересекают перенос строки.
     На этом держится безопасность разбиения: резать можно по границам
     строк, не разрывая тег. Разорванный тег Telegram отвергает целиком —
     то есть разбор просто не доходит.

  3. БЛОЧНЫЙ тег ровно один: <blockquote expandable>. Он многострочный по
     своей природе, поэтому chunk() знает про него отдельно — закрывает
     перед разрывом и открывает заново в следующей части. Раньше
     многострочные теги были просто запрещены; запрет сняли ради
     сворачиваемых блоков, но заменили его явной обработкой, а не ничем.

Порядок секций подчинён одному: сверху то, ради чего читатель открыл
сообщение — стоит ли это его времени, работает ли на Windows и что
делать. Доказательная часть занимает больше места, но нужна реже, и
поэтому убрана под сворачиваемые блоки.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Iterable
from urllib.parse import urlparse

import httpx

from watcher.analyze import Analysis
from watcher.original import Author
from watcher.detect import Event

log = logging.getLogger(__name__)

TELEGRAM_HARD_LIMIT = 4096

# Место под служебный префикс «[2/3]» и под закрывающий </blockquote>,
# который может добавиться на разрыве. Резервируется заранее: иначе на
# границе лимита то и другое вытолкнет сообщение за 4096 уже после того,
# как разбиение посчитано.
_PREFIX_ROOM = 32

_QUOTE_OPEN = re.compile(r"<blockquote(?: expandable)?>")
_QUOTE_CLOSE = "</blockquote>"

# Модель, работающая с веб-поиском, вставляет цитаты прямо в текст в виде
# ([домен](https://...)). Сырым текстом это мусор на пол-абзаца. Выкусываем
# и собираем адреса отдельно, чтобы ни одна ссылка не потерялась.
_INLINE_CITATION = re.compile(r"\s*\(\[[^\]]+\]\((https?://[^)\s]+)\)\)")

# Команды и пути модель обрамляет обратными кавычками, хотя промт этого не
# просит. Для нас это готовая разметка, а не мусор — см. _code_spans().
#
# Перенос строки внутрь вставки не пускаем намеренно: chunk() режет
# сообщение по границам строк и держится на том, что строка не содержит
# незакрытого тега. Многострочная вставка это правило нарушила бы, а
# разорванный тег Telegram отвергает вместе со всем сообщением.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")

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


def strip_citations(text: str) -> tuple[str, list[str]]:
    """Убрать врезанные в текст markdown-цитаты, вернув их адреса."""
    found: list[str] = []

    def take(match: re.Match) -> str:
        found.append(match.group(1))
        return ""

    return _INLINE_CITATION.sub(take, text).strip(), found


def source_label(url: str) -> str:
    """Короткая подпись вместо простыни URL.

    Шесть строк голых адресов внизу каждого разбора не читаются и не
    нажимаются. Подпись говорит, куда ведёт ссылка, до нажатия.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or url).removeprefix("www.")
    tail = [p for p in parsed.path.split("/") if p]
    if host.endswith("x.com"):
        return f"пост @{tail[0]}" if tail else "пост в X"
    if not tail:
        return host
    return f"{host.split('.')[0]}: {tail[-1].replace('-', ' ')}"


class _Text:
    """Накопитель: готовит текст модели к отправке и копит выкушенные ссылки.

    Три шага в жёстком порядке: выкусить markdown-цитаты, экранировать,
    и только потом добавить свои теги. Экранировать после — значит
    превратить собственную разметку в &lt;code&gt;.
    """

    def __init__(self) -> None:
        self.citations: list[str] = []

    def __call__(self, raw: str) -> str:
        cleaned, found = strip_citations(raw)
        self.citations.extend(found)
        return _code_spans(esc(cleaned))


def post_handle(urls: list[str]) -> str | None:
    """Хендл автора цитируемого поста — из адреса, а не из текста.

    Подписывать слой именем человека честно только тогда, когда имя
    вычислено, а не припомнено. В адресе поста хендл стоит первым сегментом
    пути, ошибиться негде. Поста среди источников нет — подпись безличная.
    """
    for url in urls:
        parsed = urlparse(url)
        if (parsed.hostname or "").removeprefix("www.").endswith("x.com"):
            tail = [p for p in parsed.path.split("/") if p]
            if tail:
                return f"@{tail[0]}"
    return None


def render(
    analysis: Analysis,
    event: Event,
    *,
    site_author: str | None = None,
    author: "Author | None" = None,
) -> str:
    """Собрать текст разбора.

    Порядок секций и подписи — здесь. Менять тон и объём можно правкой
    промтов в prompts/, а порядок и оформление — правкой этой функции.
    Ни то, ни другое не требует трогать обнаружение, состояние или доставку.
    """
    clean = _Text()
    lines: list[str] = [
        f"<b>{esc(analysis.headline)}</b>",
        f"<i>{esc(event.headline)}</i>",
        "",
        f"<b>Стоит ли тебе:</b> {_VERDICT_LABEL[analysis.verdict.worth_it]}",
        clean(analysis.verdict.why),
    ]

    # Windows печатается, только когда меняет то, что читатель будет
    # делать. «Работает» — это и есть ожидание по умолчанию, и строка про
    # него занимала место, не сообщая ничего. Саму проверку это не
    # отменяет: промт по-прежнему требует разобраться с Windows на каждом
    # разборе, и молчание здесь означает «разобрались, всё как есть».
    if analysis.windows.status != "works":
        lines += [
            "",
            f"<b>Windows:</b> {_WINDOWS_LABEL[analysis.windows.status]}",
            clean(analysis.windows.detail),
        ]

    if analysis.action.strip():
        lines += ["", "<b>Что сделать</b>", clean(analysis.action)]

    body = [clean(analysis.what_it_is), clean(analysis.how_it_works)]
    if analysis.example.strip():
        # Метка нужна, чтобы пример находился глазами: он отвечает на другой
        # вопрос, чем механика, и читается отдельно от неё.
        body.append(f"<b>Пример.</b> {clean(analysis.example)}")
    if analysis.how_to_verify.strip():
        body.append(f"<b>Как проверить.</b> {clean(analysis.how_to_verify)}")
    lines += ["", _quote("Что это и как работает", "\n\n".join(body))]

    if analysis.pitfalls:
        # Ловушки не прячем глубоко: на них наступают через минуту после
        # того, как начали делать, а не при перечитывании разбора.
        lines.append(_quote(
            "Где споткнёшься",
            "\n".join(f"— {clean(item)}" for item in analysis.pitfalls),
        ))

    # Слои — своим свёртком, а не внутри общего: это ядро разбора, и до
    # него должно быть одно нажатие, а не нажатие плюс поиск глазами.
    #
    # Подписи называют людей, а не роли: «@trq212 в оригинале» вместо
    # «автор оригинала». Оба имени вычислены — хендл из адреса поста, тот,
    # кто ведёт сайт, из его разметки. Не вычислилось — подпись безличная:
    # чужое имя в подписи хуже её отсутствия.
    who_post = author.credited if author else post_handle(analysis.sources)
    said = f"{esc(who_post)} в оригинале" if who_post else "В оригинальном посте"
    added = f"{esc(site_author)} дописал" if site_author else "Дописано на сайте"
    lines.append(_quote("Слои достоверности", "\n\n".join([
        f"<b>{said}.</b> {clean(analysis.layers.original_author)}",
        f"<b>{added}.</b> {clean(analysis.layers.site_author)}",
        f"<b>Документация.</b> {clean(analysis.layers.official_docs)}",
        f"<b>Мой вывод.</b> {clean(analysis.layers.my_conclusion)}",
    ])))

    if analysis.unconfirmed:
        lines.append(_quote(
            "Не подтверждено документацией",
            "\n".join(f"— {clean(item)}" for item in analysis.unconfirmed),
        ))

    if analysis.anomalies:
        # Аномалии НЕ сворачиваются намеренно: это найденные в чужом тексте
        # обращения к агенту. Читатель должен видеть, что материал пытался
        # управлять системой, без дополнительного нажатия.
        lines += [
            "",
            "<b>Аномалии в исходном тексте</b>",
            "Инструкции, обращённые к агенту. Процитированы, не исполнялись:",
        ]
        lines += [f"— {clean(item)}" for item in analysis.anomalies]

    if analysis.related:
        lines += ["", f"<b>Рядом:</b> {clean(', '.join(analysis.related))}"]

    urls = list(dict.fromkeys([*analysis.sources, *clean.citations]))
    if urls:
        rendered = " · ".join(
            f'<a href="{esc(u)}">{esc(source_label(u))}</a>' for u in urls
        )
        lines += ["", f"<b>Источники:</b> {rendered}"]

    return "\n".join(lines).strip()


def _quote(title: str, body: str) -> str:
    """Сворачиваемый блок. Открывающий тег — в начале строки, закрывающий — в конце."""
    return f"<blockquote expandable><b>{title}</b>\n{body}{_QUOTE_CLOSE}"


# Карточку читают целиком и не разворачивая — в этом весь её смысл.
# Поэтому у каждого поля свой потолок: без него одно многословное поле
# съедает место у остальных, и карточка превращается в ту же простыню,
# от которой уходили.
CARD_WHY_CHARS = 140
CARD_WINDOWS_CHARS = 100
CARD_ACTION_CHARS = 110


def _trim(text: str, limit: int) -> str:
    """Укоротить до потолка, отметив, что текст урезан.

    Многоточие обязательно даже при обрезке по концу предложения: без него
    фраза выглядит законченной, и читатель не узнает, что за ссылкой есть
    продолжение. Точка перед многоточием убирается — «…дробят работу…»
    читается, «…работу. …» нет.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    sentence = text.rfind(".", 0, limit + 1)
    if sentence > limit // 2:
        return _outside_code(text[:sentence]) + "…"
    word = text.rfind(" ", 0, limit)
    return _outside_code(text[: word if word > 0 else limit]) + "…"


def _outside_code(text: str) -> str:
    """Отступить назад, если разрез пришёлся внутрь вставки в кавычках.

    Половина команды («claude --permission-mode…») читателю бесполезна и
    выглядит опечаткой: лучше оборвать фразу до неё.
    """
    if text.count("`") % 2:
        text = text[: text.rfind("`")]
    return text.rstrip(" ,;:-—")


def _code_spans(text: str) -> str:
    """Обратные кавычки модели → <code>, непарная кавычка выбрасывается.

    Модель размечает команды обратными кавычками вопреки запрету в промте.
    В карточке они доезжают буквальными символами и выглядят мусором, а
    <code> Telegram понимает. Обрезка при этом может прийтись на середину
    вставки — оставшуюся одинокую кавычку убираем, потому что незакрытый
    тег Telegram отвергает целиком, и карточка просто не дойдёт.
    """
    return _CODE_SPAN.sub(lambda m: f"<code>{m.group(1)}</code>", text).replace("`", "")


def _short(raw: str, limit: int) -> str:
    """Поле модели для карточки: без цитат, укороченное, экранированное.

    Порядок обязателен. Обрезать уже экранированный текст нельзя: разрез
    придётся на середину сущности вроде &amp;lt; и сломает разметку. Теги
    добавляются последними — по той же причине.
    """
    text, _ = strip_citations(raw)
    return _code_spans(esc(_trim(text, limit)))


def card(analysis: Analysis, event: Event, url: str) -> str:
    """Короткая карточка со ссылкой на страницу разбора.

    Отвечает на четыре вопроса, ради которых сообщение открывают: что
    случилось, стоит ли оно времени, работает ли на Windows и что сделать.
    Доказательная часть — слои, механика, расхождения — живёт на странице:
    в мессенджере её всё равно читают по диагонали, а через неделю не
    находят вовсе.

    Адрес приходит параметром, а не строится здесь: карточка отправляется
    только после того, как страница уже отвечает по этому адресу.
    """
    lines = [
        f"<b>{esc(analysis.headline)}</b>",
        f"<i>{esc(event.headline)}</i>",
        "",
        f"<b>Стоит ли:</b> {_VERDICT_LABEL[analysis.verdict.worth_it]} — "
        f"{_short(analysis.verdict.why, CARD_WHY_CHARS)}",
    ]

    # Та же экономия, что и в полном тексте, но здесь она весомее: строка
    # про Windows занимала четверть карточки, чтобы сообщить ожидаемое.
    if analysis.windows.status != "works":
        lines.append(
            f"<b>Windows:</b> {_WINDOWS_LABEL[analysis.windows.status]} — "
            f"{_short(analysis.windows.detail, CARD_WINDOWS_CHARS)}"
        )

    if analysis.action.strip():
        lines.append(f"<b>Что сделать:</b> {_short(analysis.action, CARD_ACTION_CHARS)}")

    # Числом, а не цитатой: сама находка лежит на странице, а в карточке
    # важно, что она есть. Аномалия при этом не тонет — читатель видит,
    # что материал пытался управлять системой.
    notes = []
    if analysis.unconfirmed:
        notes.append(f"расхождений с документацией: {len(analysis.unconfirmed)}")
    if analysis.anomalies:
        notes.append(f"аномалий в исходном тексте: {len(analysis.anomalies)}")
    if notes:
        lines += ["", "Найдено — " + ", ".join(notes) + "."]

    lines += ["", f'<a href="{esc(url)}">Разбор целиком</a>']
    return "\n".join(lines)


def chunk(text: str, limit: int) -> list[str]:
    """Разбить сообщение на части, не разрывая строки и теги.

    Режем строго по границам строк: строчные теги в render() не пересекают
    перенос, поэтому строка не может содержать незакрытый тег.

    Отдельно обрабатывается <blockquote>: он многострочный, и разрыв внутри
    него превратил бы сообщение в отвергнутое Telegram. Поэтому на разрыве
    блок закрывается, а в следующей части открывается заново тем же тегом —
    вместе с атрибутом expandable, иначе часть 2 перестала бы сворачиваться.
    """
    if limit >= TELEGRAM_HARD_LIMIT:
        raise ValueError(f"limit={limit} не оставляет запаса под лимит Telegram")

    budget = limit - _PREFIX_ROOM
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    open_tag: str | None = None

    def flush() -> None:
        nonlocal current, size
        if not current:
            return
        body = "\n".join(current)
        if open_tag:
            body += _QUOTE_CLOSE
        chunks.append(body)
        current = [open_tag] if open_tag else []
        size = len(open_tag) if open_tag else 0

    for line in text.split("\n"):
        # Одиночная строка длиннее бюджета — режем по словам. На практике
        # не встречается (модель пишет абзацами), но длинная ссылка или
        # слипшийся текст не должны ронять доставку.
        pieces = [line] if len(line) <= budget else _split_long(line, budget)
        for piece in pieces:
            addition = len(piece) + (1 if current else 0)
            if size + addition > budget and current:
                flush()
                addition = len(piece) + (1 if current else 0)
            current.append(piece)
            size += addition
            found = _QUOTE_OPEN.search(piece)
            if found:
                open_tag = found.group(0)
            if _QUOTE_CLOSE in piece:
                open_tag = None

    flush()
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

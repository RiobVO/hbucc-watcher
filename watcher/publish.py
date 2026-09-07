"""Страница разбора и её публикация в репозиторий отчётов.

Зачем страница вообще. Полный разбор — это 5–7 тысяч символов, и в
Telegram он приезжает простынёй на два-три сообщения, где половина текста
спрятана под сворачиваемые блоки. Читать это в мессенджере неудобно, а
перечитывать через неделю невозможно: сообщение утонуло. Поэтому разбор
живёт страницей по своему адресу, а в Telegram уходит карточка со ссылкой.

Два правила, которые здесь важнее красоты:

  1. ПУБЛИКАЦИЯ ОБЯЗАНА ДЕГРАДИРОВАТЬ. Не опубликовалось — читатель
     получает полный текст, как раньше. Поэтому publish_page падает
     явным PublishFailed и никогда не возвращает адрес страницы, которой
     нет: карточка со ссылкой на 404 хуже простыни.

  2. ПУБЛИКАЦИЯ ДО ОТПРАВКИ. Сначала страница живёт по адресу, потом
     читатель получает ссылку. Порядок держит main.py, здесь — только
     то, что делает его возможным.

Текст на странице пишет модель по материалам чужого сайта, то есть в HTML
попадает недоверенный вход. Экранируется всё без исключения — и ровно в
одном месте, в `_Prose`. Обратные кавычки, которые модель ставит вокруг
команд вопреки запрету в промте, здесь становятся <code>: на одобренных
страницах эти куски оформлены именно так.

Вёрстка лежит в templates/report.css и встраивается в страницу. Внешний
файл добавил бы второй запрос и кеш к странице в чужом репозитории —
страница обязана открываться одна.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime
from typing import Iterable
from urllib.parse import urlparse

import httpx

from watcher.analyze import Analysis, Usage
from watcher.config import ROOT
# Подпись ссылки и выкусывание markdown-цитат — те же самые, что в
# Telegram: это чистка одного и того же артефакта модели и один и тот же
# ярлык для одного и того же адреса. Вторая копия однажды разошлась бы с
# первой, и разошлась бы молча.
from watcher.deliver import post_handle, source_label, strip_citations
from watcher.detect import (
    BLOCK_ADDED,
    BLOCK_DELETED,
    BLOCK_EDITED,
    PART_ADDED,
    PART_REMOVED,
    Event,
)
from watcher.original import Author

log = logging.getLogger(__name__)

__all__ = [
    "PublishFailed",
    "page_name",
    "page_url",
    "index_entries",
    "index_entry",
    "merge_entry",
    "publish_page",
    "render_divergences",
    "render_index",
    "render_page",
    "update_index",
    "wait_for_page",
]

INDEX_NAME = "index.html"
# Страница расхождений считается из записей архива и живёт рядом с ним.
DIVERGENCES_NAME = "divergences.html"

# Индекс носит свои данные в себе. Отдельный манифест рядом со страницей
# — это два файла, которые обязаны совпадать, а значит однажды разойдутся;
# к тому же Contents API отдаёт содержимое и sha одним ответом, так что
# самодостаточная страница обходится двумя запросами вместо четырёх.
_INDEX_DATA = re.compile(
    r'<script type="application/json" id="reports">(.*?)</script>', re.S
)

CSS_PATH = ROOT / "templates" / "report.css"

GITHUB_API = "https://api.github.com"

# Ярлыки короткие, не такие, как в Telegram: в ряду метрик стоит одно
# слово под подписью «стоит ли тебе», и «нет, пропускай» там не помещается
# ни по смыслу, ни по ширине.
_VERDICT_SHORT = {"yes": "да", "no": "нет", "maybe": "смотря по чему"}
_WINDOWS_SHORT = {
    "works": "работает",
    "macos_only": "только macOS",
    "needs_adaptation": "с оговоркой",
    "unconfirmed": "неизвестно",
}

# Тон тайла в ряду метрик. «Не стоит» гасится, а не краснеет: красный
# читается как поломка, а это сэкономленное время. «Только macOS» —
# наоборот, красный: у читателя Windows, и это единственная метрика,
# которая говорит «у тебя не заработает».
_VERDICT_TONE = {"yes": "good", "maybe": "warn", "no": "muted"}
_WINDOWS_TONE = {
    "works": "good",
    "needs_adaptation": "warn",
    "unconfirmed": "warn",
    "macos_only": "bad",
}
_KIND_BADGE = {
    PART_ADDED: "новая часть",
    PART_REMOVED: "часть удалена",
    BLOCK_ADDED: "новый совет",
    BLOCK_EDITED: "совет изменён",
    BLOCK_DELETED: "совет удалён",
}

_CODE_SPAN = re.compile(r"`([^`]+)`")

# Кусок кода уезжает в отдельный блок с кнопкой «копировать», только если
# его и правда захочется скопировать целиком: многострочный, конфиг в
# фигурных скобках или просто длинный. Короткая команда остаётся в строке,
# иначе фраза рвётся пополам на ровном месте.
_BLOCK_MIN_CHARS = 60


class PublishFailed(RuntimeError):
    """Страница не опубликована. Читателю уходит полный текст, как раньше."""


# --------------------------------------------------------------------------
# Текст модели → HTML
# --------------------------------------------------------------------------


class _Prose:
    """Единственное место, где текст модели превращается в разметку.

    Делает три вещи и в этом порядке: выкусывает markdown-цитаты вида
    ([домен](url)), запоминая адреса; экранирует всё остальное; возвращает
    обратные кавычки в виде <code>.

    Порядок важен: экранировать надо ДО того, как мы сами добавим теги,
    иначе собственная разметка уедет в &lt;code&gt;.
    """

    def __init__(self) -> None:
        self.citations: list[str] = []

    def clean(self, raw: str) -> str:
        cleaned, found = strip_citations(raw)
        self.citations.extend(found)
        return cleaned

    def __call__(self, raw: str) -> str:
        parts = _CODE_SPAN.split(self.clean(raw))
        # split с одной группой даёт чередование: текст, код, текст, код...
        return "".join(
            html.escape(piece, quote=False)
            if index % 2 == 0
            else f"<code>{html.escape(piece, quote=False)}</code>"
            for index, piece in enumerate(parts)
        )


def _is_block_worthy(code: str) -> bool:
    stripped = code.strip()
    return "\n" in code or stripped.startswith(("{", "[")) or len(stripped) >= _BLOCK_MIN_CHARS


def _example_html(raw: str, prose: _Prose) -> str:
    """Пример: проза абзацами, крупные куски кода — блоком с кнопкой.

    Модель отдаёт пример одной строкой с командами в обратных кавычках.
    Здесь строка разворачивается в то, что на одобренных страницах сделано
    руками: абзац, под ним копируемый блок конфига, снова абзац.
    """
    pieces = _CODE_SPAN.split(prose.clean(raw))
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        text = "".join(paragraph).strip()
        paragraph.clear()
        # Хвост фразы за блоком кода — часто одна точка или закрывающая
        # скобка. Абзац из знака препинания на странице выглядит опечаткой.
        if text and any(char.isalnum() for char in text):
            out.append(f"  <p>{text}</p>")

    for index, piece in enumerate(pieces):
        if index % 2 == 0:
            paragraph.append(html.escape(piece, quote=False))
        elif _is_block_worthy(piece):
            flush()
            out.append(
                '  <div class="code-block">\n'
                '    <button class="copy-btn">копировать</button>\n'
                f"    <pre>{html.escape(piece.strip(), quote=False)}</pre>\n"
                "  </div>"
            )
        else:
            paragraph.append(f"<code>{html.escape(piece, quote=False)}</code>")

    flush()
    return "\n".join(out)


def _first_sentence(text: str) -> str:
    """Первая фраза — в подзаголовок страницы.

    Отдельного поля под подзаголовок в схеме нет и заводить его не за чем:
    вывод наблюдателя начинается ровно с той фразы, которая и должна стоять
    под заголовком.
    """
    match = re.search(r"^.+?[.!?](?=\s|$)", text.strip(), re.S)
    return (match.group(0) if match else text.strip())[:200]


# --------------------------------------------------------------------------
# Адрес страницы
# --------------------------------------------------------------------------


def page_name(event: Event) -> str:
    """Имя файла страницы: часть, блок и идентификатор события.

    Ни транслита заголовка, ни даты — ничего, что зависит от момента
    вызова. Имя обязано быть одинаковым при повторе: прогон, упавший
    после публикации, но до отправки, повторится следующим разом и должен
    перезаписать ту же страницу, а не завести вторую. Часы это ломали —
    повтор после полуночи по UTC давал другое имя, а прогоны идут раз в
    шесть часов, так что четверть отказов приходилась бы ровно на такой
    случай.

    Идентификатор события в имени не для красоты: `bid` переживает правку
    совета, то есть без него разбор правки затёр бы разбор появления, а
    отправленная раньше карточка стала бы вести на чужой текст.
    """
    # Шестнадцать знаков, а не восемь: восемь это 32 бита, и порог дня
    # рождения для них — 65 тысяч событий. При темпе сайта в 72 события в
    # год до такого не дожить, но спорить об этом дороже, чем дописать
    # восемь символов в имя файла.
    short = event.event_id.removeprefix("sha256:")[:16]
    return f"part{event.part_number}-{event.bid.removeprefix('b-')}-{short}.html"


def page_url(base_url: str, name: str) -> str:
    """Публичный адрес страницы. База берётся из конфига, со слэшем или без."""
    return f"{base_url.rstrip('/')}/{name}"


# --------------------------------------------------------------------------
# Рендер страницы
# --------------------------------------------------------------------------


def render_page(
    analysis: Analysis,
    event: Event,
    *,
    author: Author | None = None,
    site_author: str | None = None,
    usage: Usage | None = None,
    price_usd: float | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Собрать страницу разбора целиком, одним самодостаточным файлом."""
    prose = _Prose()
    when = generated_at or datetime.now().astimezone()

    # Порядок рендера не совпадает с порядком на странице, и это важно.
    # Оглавление обязано перечислять ровно те секции, под которыми что-то
    # есть, а «Источники» собираются в том числе из markdown-цитат,
    # выкушенных из текстовых полей: пока не отрендерено последнее поле,
    # список адресов неполон. Поэтому источники считаются в самом конце.
    headline = prose(analysis.headline)
    subtitle = prose(_first_sentence(analysis.layers.my_conclusion))
    glance = _glance(analysis, prose)
    anomalies = _anomalies(analysis.anomalies, prose) if analysis.anomalies else ""
    sections = _sections(analysis, prose, author, site_author)
    sources = _sources(analysis, prose)

    body = [
        _eyebrow(event),
        f"  <h1>{headline}</h1>",
        f'  <p class="subtitle">{subtitle}</p>',
        _stats(analysis),
        glance,
        # Аномалии стоят до оглавления и не сворачиваются: это найденные в
        # чужом тексте обращения к агенту, и читатель обязан увидеть их
        # без прокрутки — как и в Telegram.
        anomalies,
        _author_card(author) if author is not None else "",
        _toc(sections, bool(sources)),
        *(html_block for _, _, html_block in sections),
        sources,
        _footer(when, usage, price_usd),
    ]

    return "\n".join([_head(analysis, event), *[part for part in body if part], _TAIL])


def _summary(analysis: Analysis) -> str:
    """Одна строка про разбор: вердикт, Windows, число расхождений.

    Идёт и в og:description страницы, и в строку архива — это один и тот
    же ответ на один и тот же вопрос «о чём это и стоит ли открывать».
    """
    summary = (
        f"Стоит ли: {_VERDICT_SHORT[analysis.verdict.worth_it]} · "
        f"Windows: {_WINDOWS_SHORT[analysis.windows.status]}"
    )
    if analysis.unconfirmed:
        summary += f" · расхождений с документацией: {len(analysis.unconfirmed)}"
    return summary


def index_entry(analysis: Analysis, event: Event, name: str, when: datetime) -> dict:
    """Строка архива для этого разбора."""
    return {
        "name": name,
        "title": analysis.headline,
        "summary": _summary(analysis),
        "date": f"{when:%Y-%m-%d}",
        "part": event.part_number,
        "kind": _KIND_BADGE.get(event.kind, event.kind),
        # Сами утверждения, а не только их число: из них собирается
        # страница расхождений, и держать её данные отдельно от архива
        # означало бы завести второй файл, обязанный совпадать с первым.
        "unconfirmed": list(analysis.unconfirmed),
        # Машинные ключи, а не русские ярлыки: из них считаются метрики
        # архива, а ярлык — презентация, которая имеет право поменяться.
        "verdict": analysis.verdict.worth_it,
        "windows": analysis.windows.status,
    }


def _head(analysis: Analysis, event: Event) -> str:
    summary = (
        f"{_summary(analysis)}. Part {event.part_number}, "
        f"{_KIND_BADGE.get(event.kind, event.kind)}."
    )
    title = html.escape(analysis.headline, quote=True)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{html.escape(summary, quote=True)}">
<meta property="og:site_name" content="Наблюдатель за howborisusesclaudecode.com">
<meta name="theme-color" content="#f8fafc">
<title>{title} — разбор</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
{CSS_PATH.read_text(encoding="utf-8")}</style>
</head>
<body>
<div class="container">
"""


def _eyebrow(event: Event) -> str:
    return (
        '  <div class="eyebrow">\n'
        f'    <span class="badge">{html.escape(_KIND_BADGE.get(event.kind, event.kind))}</span>\n'
        f"    <span>Part {event.part_number} · {html.escape(event.part_title)}</span>\n"
        "  </div>"
    )


def _stats(analysis: Analysis) -> str:
    """Ряд метрик: четыре величины, по которым решают, читать ли дальше.

    Числа поисков и цены здесь нет намеренно, хотя раньше были. Они уже
    стоят в футере, отвечают на вопрос «во что обошлось мне», а не «стоит
    ли это твоего времени», и занимали два тайла из пяти в самом заметном
    месте страницы — на узком экране вытесняя те, что отвечают.

    Тон вместо одного признака `bad`: цвет обязан стоять там, где решение.
    «Не стоит» гасится, а не краснеет: это не поломка, а сэкономленные
    двадцать минут — читатель имеет право закрыть страницу и уйти.
    """
    cells = (
        (
            _VERDICT_SHORT[analysis.verdict.worth_it],
            "стоит ли тебе",
            _VERDICT_TONE[analysis.verdict.worth_it],
        ),
        (
            _WINDOWS_SHORT[analysis.windows.status],
            "на Windows",
            _WINDOWS_TONE[analysis.windows.status],
        ),
        (str(len(analysis.pitfalls)), "ловушек", "warn" if analysis.pitfalls else ""),
        (
            str(len(analysis.unconfirmed)),
            "расхождения с докой",
            "bad" if analysis.unconfirmed else "",
        ),
    )

    rendered = "\n".join(
        f'    <div><div class="stat-value{" " + tone if tone else ""}">{html.escape(value)}</div>'
        f'<div class="stat-label">{label}</div></div>'
        for value, label, tone in cells
    )
    return f'  <div class="stats-row">\n{rendered}\n  </div>'


def _glance(analysis: Analysis, prose: _Prose) -> str:
    rows = [("Стоит ли", prose(analysis.verdict.why))]
    # Тайл в ряду метрик уже сказал «работает». Повторять это строкой в
    # «Коротко» — занимать место ради ожидаемого; строка нужна там, где
    # Windows меняет то, что читатель будет делать.
    if analysis.windows.status != "works":
        rows.append(("Windows", prose(analysis.windows.detail)))
    if analysis.action.strip():
        rows.append(("Что сделать", f"<b>{prose(analysis.action)}</b>"))

    body = "\n".join(
        '    <div class="glance-row">\n'
        f'      <span class="glance-key">{key}</span>\n'
        f'      <span class="glance-val">{value}</span>\n'
        "    </div>"
        for key, value in rows
    )
    return f'  <div class="at-a-glance">\n    <div class="glance-title">Коротко</div>\n{body}\n  </div>'


def _anomalies(items: list[str], prose: _Prose) -> str:
    quotes = "\n".join(f"    <blockquote>{prose(item)}</blockquote>" for item in items)
    return (
        '  <div class="card card-alert">\n'
        '    <div class="card-head">аномалии в исходном тексте · процитированы, не исполнялись</div>\n'
        f"{quotes}\n"
        "  </div>"
    )


def _author_card(author: Author) -> str:
    name = author.name or author.handle
    initial = html.escape((name.lstrip("@") or "?")[0].upper())
    bio = (
        f'      <div class="who-bio">{html.escape(author.bio, quote=False)}</div>\n'
        if author.bio
        else ""
    )
    return (
        '  <div class="who-wrote">\n'
        f'    <div class="who-avatar">{initial}</div>\n'
        "    <div>\n"
        f'      <div><span class="who-name">{html.escape(name)}</span> '
        f'<span class="who-handle">{html.escape(author.handle)}</span></div>\n'
        f"{bio}"
        "    </div>\n"
        "  </div>"
    )


def _sections(
    analysis: Analysis,
    prose: _Prose,
    author: Author | None,
    site_author: str | None,
) -> list[tuple[str, str, str]]:
    """Секции страницы: якорь, подпись в оглавлении, готовый HTML.

    Список собирается по фактическому наполнению: пустое поле не даёт ни
    пункта в оглавлении, ни заголовка в пустоту.
    """
    sections: list[tuple[str, str, str]] = [
        ("what", "Что это", _section("what", "Что это", f"  <p>{prose(analysis.what_it_is)}</p>")),
        (
            "how",
            "Как работает",
            _section("how", "Как работает", f"  <p>{prose(analysis.how_it_works)}</p>"),
        ),
    ]

    if analysis.example.strip():
        sections.append(
            ("example", "Пример", _section("example", "Пример", _example_html(analysis.example, prose)))
        )

    if analysis.how_to_verify.strip():
        sections.append((
            "verify",
            "Как проверить",
            _section(
                "verify",
                "Как проверить",
                f'  <div class="card card-good"><p>{prose(analysis.how_to_verify)}</p></div>',
            ),
        ))

    if analysis.pitfalls:
        sections.append((
            "pitfalls",
            "Где споткнёшься",
            _section(
                "pitfalls",
                "Где споткнёшься",
                '  <div class="card card-warn">\n' + _list(analysis.pitfalls, prose) + "\n  </div>",
            ),
        ))

    sections.append((
        "layers",
        "Слои достоверности",
        _layers(analysis, prose, author, site_author),
    ))

    if analysis.unconfirmed:
        sections.append((
            "unconfirmed",
            "Расхождения с документацией",
            _section(
                "unconfirmed",
                "Расхождения с документацией",
                '  <div class="card card-warn">\n'
                '    <div class="card-head">сайт обещает то, чего документация не подтверждает</div>\n'
                + _list(analysis.unconfirmed, prose)
                + "\n  </div>",
            ),
        ))

    if analysis.related:
        chips = "\n".join(f"    <span>{prose(item)}</span>" for item in analysis.related)
        sections.append((
            "related",
            "Рядом",
            _section("related", "Рядом", f'  <div class="chips">\n{chips}\n  </div>'),
        ))

    return sections


def _section(anchor: str, title: str, body: str) -> str:
    return f'  <h2 id="{anchor}">{title}</h2>\n{body}'


def _list(items: Iterable[str], prose: _Prose) -> str:
    rows = "\n".join(f"      <li>{prose(item)}</li>" for item in items)
    return f'    <ul class="plain">\n{rows}\n    </ul>'


def _layers(
    analysis: Analysis,
    prose: _Prose,
    author: Author | None,
    site_author: str | None,
) -> str:
    """Четыре слоя карточками, подписанные людьми, а не ролями.

    Хендл автора поста вычисляется из адреса первоисточника, ведущий сайт —
    из разметки сайта. Не вычислилось — подпись безличная: чужое имя в
    подписи хуже её отсутствия. Ровно то же правило, что в Telegram.
    """
    who_post = author.credited if author else post_handle(analysis.sources)
    cards = [
        ("01", who_post or "", "в оригинале", analysis.layers.original_author, ""),
        ("02", site_author or "", "дописано на сайте", analysis.layers.site_author, ""),
        ("03", "code.claude.com", "документация", analysis.layers.official_docs, ""),
        ("04", "", "вывод наблюдателя", analysis.layers.my_conclusion, " card-good"),
    ]

    rendered = []
    for num, who, role, text, extra in cards:
        who_html = f'<span class="who">{html.escape(who)}</span> · ' if who else ""
        rendered.append(
            f'    <div class="card{extra}">\n'
            f'      <div class="card-head"><span class="layer-num">{num}</span> {who_html}{role}</div>\n'
            f"      <p>{prose(text)}</p>\n"
            "    </div>"
        )
    body = '  <div class="layers">\n' + "\n".join(rendered) + "\n  </div>"
    return _section("layers", "Слои достоверности", body)


# Подписи слоёв приходят параметрами, здесь их нет намеренно: имя автора
# вычисляется кодом до вызова, а не подставляется по умолчанию.


def _sources(analysis: Analysis, prose: _Prose) -> str:
    urls = list(dict.fromkeys([*analysis.sources, *prose.citations]))
    if not urls:
        return ""
    links = "\n".join(
        f'    <a href="{html.escape(url, quote=True)}">'
        f'<span class="host">{html.escape(_host(url))}</span>'
        f'<span class="what">{html.escape(source_label(url))}</span></a>'
        if _is_web_url(url)
        # Схему, которой тут быть не должно, показываем текстом. Убирать
        # адрес совсем нельзя: читатель обязан видеть, на что материал
        # ссылался, — просто нажимать на это он не будет.
        else f'    <div class="card"><p>{html.escape(url)} — адрес отброшен: '
        "не http и не https</p></div>"
        for url in urls
    )
    return _section("sources", "Источники", f'  <div class="sources">\n{links}\n  </div>')


def _is_web_url(url: str) -> bool:
    """Только http и https попадают в href.

    `sources` — строки, которые модель составила по чужому материалу;
    схема проверяет их тип, но не содержание. Экранирование кавычек схему
    не обезвреживает: `javascript:` в href остаётся рабочей ссылкой, а
    страница лежит на публичном домене.
    """
    return urlparse(url.strip()).scheme in ("http", "https")


def _host(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


def _toc(sections: list[tuple[str, str, str]], with_sources: bool) -> str:
    links = [
        f'    <a href="#{anchor}">{html.escape(title)}</a>' for anchor, title, _ in sections
    ]
    if with_sources:
        links.append('    <a href="#sources">Источники</a>')
    return '  <nav class="nav-toc">\n' + "\n".join(links) + "\n  </nav>"


def _footer(when: datetime, usage: Usage | None, price_usd: float | None) -> str:
    parts = [
        '    <span><a href="index.html">все разборы</a></span>',
        f"    <span>{when:%d.%m.%Y, %H:%M} UTC</span>",
    ]
    if usage is not None:
        spent = f"{usage.searches} поиска · {usage.input_tokens} вход · {usage.output_tokens} выход"
        if price_usd is not None:
            spent += f" · ${price_usd:.4f}"
        parts.append(f"    <span>{spent}</span>")
    return "  <footer>\n" + "\n".join(parts) + "\n  </footer>"


# Подписи слоёв 1 и 2 подставляются в render_page: имена приходят снаружи.
_WHO_ORIGINAL = ""
_WHO_SITE = ""

_TAIL = """
</div>

<script>
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      await navigator.clipboard.writeText(btn.parentElement.querySelector('pre').textContent);
      const was = btn.textContent;
      btn.textContent = 'скопировано'; btn.classList.add('done');
      setTimeout(() => { btn.textContent = was; btn.classList.remove('done'); }, 1400);
    });
  });
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# Индекс архива
# --------------------------------------------------------------------------


def index_entries(page: str | None) -> list[dict]:
    """Достать список разборов из самой индексной страницы.

    Индекса нет или его переписали руками — считаем, что записей нет.
    Архив соберётся заново со следующей публикации: терять при этом
    нечего, страницы разборов лежат на своих адресах и никуда не делись.
    """
    if not page:
        return []
    found = _INDEX_DATA.search(page)
    if not found:
        log.warning("в индексе нет блока с данными — список собирается заново")
        return []
    try:
        data = json.loads(found.group(1))
    except json.JSONDecodeError:
        log.warning("данные индекса не разбираются, список собирается заново", exc_info=True)
        return []
    return data if isinstance(data, list) else []


def merge_entry(entries: list[dict], entry: dict) -> list[dict]:
    """Добавить разбор в список, заменив прежнюю запись с тем же именем.

    Замена, а не добавление: повторная публикация того же события
    перезаписывает ту же страницу, и второй строки в архиве быть не должно.
    """
    kept = [item for item in entries if item.get("name") != entry["name"]]
    return sorted(
        [entry, *kept],
        key=lambda item: (item.get("date", ""), item.get("part", 0)),
        reverse=True,
    )


def _json_for_html(entries: list[dict]) -> str:
    """JSON, безопасный внутри <script>.

    `json.dumps` не трогает угловые скобки, а HTML закрывает script-data
    на первом же `</script` — заголовок от модели с такой подстрокой
    вырвался бы из блока данных наружу и стал разметкой. Экранируем `<`
    и `&`: внутри JSON это законные escape-последовательности, читается
    обратно без потерь.
    """
    return (
        json.dumps(entries, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русский счёт: 1 разбор, 3 разбора, 11 разборов."""
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _reports_word(count: int) -> str:
    return _plural(count, "разбор", "разбора", "разборов")


def _claims_word(count: int) -> str:
    return _plural(count, "расхождение", "расхождения", "расхождений")


def _shell(
    *, eyebrow: str, title: str, subtitle: str, description: str,
    body: str, footer: str, tail: str = "",
) -> str:
    """Скелет страницы без разбора: архив и расхождения.

    Одна копия на обе: `<head>` тут содержательный — og-разметка под превью
    в Telegram, встроенный CSS, — и две копии разошлись бы молча, а заметно
    это стало бы только в чужой ленте.
    """
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="{html.escape(title, quote=True)}">
<meta property="og:description" content="{html.escape(description, quote=True)}">
<meta name="theme-color" content="#f8fafc">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
{CSS_PATH.read_text(encoding="utf-8")}</style>
</head>
<body>
<div class="container">

  <div class="eyebrow"><span>{html.escape(eyebrow)}</span></div>
  <h1>{html.escape(title)}</h1>
  <p class="subtitle">{html.escape(subtitle)}</p>

{body}

  <footer>
{footer}
  </footer>

</div>{tail}
</body>
</html>"""


def render_divergences(entries: list[dict]) -> str:
    """Страница «где сайт расходится с документацией».

    Самое редкое, что умеет система: поймать фан-сайт на утверждении,
    которого официальная документация не подтверждает. По одному разбору
    такая находка теряется среди механики и примеров, а собранные вместе
    они и есть ответ на вопрос «насколько вообще можно верить сайту».

    Данные берутся из записей архива и ниоткуда больше: второй файл рядом
    с индексом обязан был бы с ним совпадать, а значит однажды разошёлся бы.
    Записи, сделанные до появления поля, поле просто не имеют — страница
    обязана открыться и без него.
    """
    found = [item for item in entries if item.get("unconfirmed")]
    total = sum(len(item["unconfirmed"]) for item in found)

    if found:
        cards = "\n".join(
            '    <div class="divergence">\n'
            f'      <div class="report-meta">{html.escape(str(item.get("date", "")))} · '
            f'Part {html.escape(str(item.get("part", "")))} · '
            f'{html.escape(str(item.get("kind", "")))}</div>\n'
            f'      <div class="report-title">'
            f'<a href="{html.escape(str(item["name"]), quote=True)}">'
            f'{html.escape(str(item.get("title", "")))}</a></div>\n'
            '      <ul class="plain">\n'
            + "\n".join(
                f"        <li>{html.escape(str(claim))}</li>"
                for claim in item["unconfirmed"]
            )
            + "\n      </ul>\n    </div>"
            for item in found
        )
        body = f'  <div class="report-list">\n{cards}\n  </div>'
        description = (
            f"{total} {_claims_word(total)} с официальной документацией "
            f"в {len(found)} {_reports_word(len(found))}."
        )
    else:
        body = (
            "  <p>Расхождений пока нет: всё, что сайт утверждал, "
            "документация подтвердила.</p>"
        )
        description = "Расхождений с официальной документацией пока не нашлось."

    return _shell(
        eyebrow="расхождения",
        title="Где сайт расходится с документацией",
        subtitle="Утверждения с сайта, которые не удалось подтвердить по официальной "
                 "документации Claude Code. Не обязательно ложь — но и не факт.",
        description=description,
        body=body,
        footer='    <span><a href="index.html">все разборы</a></span>\n'
               f"    <span>{description}</span>",
    )


# Обратные словари ярлыков: записи, сделанные до появления машинных полей,
# несут вердикт и Windows-статус только в строке summary. Строку собирал
# этот же код из этих же словарей, поэтому чтение назад однозначно.
_VERDICT_FROM_SHORT = {label: key for key, label in _VERDICT_SHORT.items()}
_WINDOWS_FROM_SHORT = {label: key for key, label in _WINDOWS_SHORT.items()}
_SUMMARY_KEYS = re.compile(r"Стоит ли: (.+?) · Windows: (.+?)(?: · |$)")

# Порядок полосок — по решению, а не по величине: сверху то, что читателю
# приятнее увидеть. Тона — те же, что в тайлах страницы разбора: «нет»
# гасится, а не краснеет, красное — только «у тебя не заработает».
_VERDICT_ORDER = (("yes", "good"), ("maybe", "warn"), ("no", "muted"))
_WINDOWS_ORDER = (
    ("works", "good"),
    ("needs_adaptation", "warn"),
    ("unconfirmed", "warn"),
    ("macos_only", "bad"),
)


def _entry_keys(item: dict) -> tuple[str | None, str | None]:
    """Вердикт и Windows-статус записи, старой или новой.

    Не разобралось ни из поля, ни из summary — индекс переписали руками;
    запись остаётся в общем счёте, но в распределения не попадает.
    """
    # Только строки: `[] not in dict` — это TypeError, а не False, и
    # рукописная запись с «"verdict": []» роняла бы весь рендер индекса.
    verdict = item.get("verdict")
    windows = item.get("windows")
    if not isinstance(verdict, str):
        verdict = None
    if not isinstance(windows, str):
        windows = None
    if verdict not in _VERDICT_SHORT or windows not in _WINDOWS_SHORT:
        found = _SUMMARY_KEYS.match(str(item.get("summary", "")))
        if found:
            if verdict not in _VERDICT_SHORT:
                verdict = _VERDICT_FROM_SHORT.get(found.group(1))
            if windows not in _WINDOWS_SHORT:
                windows = _WINDOWS_FROM_SHORT.get(found.group(2))
    return (
        verdict if verdict in _VERDICT_SHORT else None,
        windows if windows in _WINDOWS_SHORT else None,
    )


def _chart(title: str, counts: Counter, order: tuple, labels: dict) -> str:
    """Одна карточка с полосками. Пустая категория не рисуется:
    полоса нулевой ширины выглядит поломкой, а не фактом."""
    if not counts:
        return ""
    peak = max(counts.values())
    rows = "\n".join(
        f'    <div class="bar-row"><span class="bar-label">{html.escape(labels[key])}</span>'
        f'<span class="bar-track"><span class="bar-fill {tone}" '
        f'style="width:{round(counts[key] / peak * 100)}%"></span></span>'
        f'<span class="bar-value">{counts[key]}</span></div>'
        for key, tone in order
        if counts.get(key)
    )
    return (
        '  <div class="chart-card">\n'
        f'    <div class="chart-title">{html.escape(title)}</div>\n'
        f"{rows}\n"
        "  </div>"
    )


def _metrics(entries: list[dict], claims: int) -> str:
    """Ряд метрик и распределения: что архив говорит в сумме.

    Плоский список отвечает «что появлялось», метрики — «стоит ли сайту
    верить и работает ли это у читателя». Данные лежат в самих записях,
    второго источника нет — расходиться нечему.
    """
    verdicts: Counter = Counter()
    windows: Counter = Counter()
    for item in entries:
        verdict, win = _entry_keys(item)
        if verdict:
            verdicts[verdict] += 1
        if win:
            windows[win] += 1

    worth = verdicts.get("yes", 0)
    works = windows.get("works", 0)
    tiles = (
        (str(len(entries)), "разборов", ""),
        (str(worth), "стоит времени", "good" if worth else ""),
        (str(works), "работает на Windows", "good" if works else ""),
        (str(claims), "расхождений с докой", "bad" if claims else ""),
    )
    rendered = "\n".join(
        f'    <div><div class="stat-value{" " + tone if tone else ""}">{value}</div>'
        f'<div class="stat-label">{label}</div></div>'
        for value, label, tone in tiles
    )
    out = [f'  <div class="stats-row">\n{rendered}\n  </div>']

    charts = [
        _chart("стоит ли времени", verdicts, _VERDICT_ORDER, _VERDICT_SHORT),
        _chart("на Windows", windows, _WINDOWS_ORDER, _WINDOWS_SHORT),
    ]
    charts = [chart for chart in charts if chart]
    if charts:
        out.append('  <div class="charts-row">\n' + "\n".join(charts) + "\n  </div>")
    return "\n".join(out)


def render_index(entries: list[dict]) -> str:
    """Собрать индексную страницу архива."""
    if entries:
        rows = "\n".join(
            f'    <a href="{html.escape(str(item["name"]), quote=True)}">\n'
            f'      <div class="report-meta">{html.escape(str(item.get("date", "")))} · '
            f'Part {html.escape(str(item.get("part", "")))} · '
            f'{html.escape(str(item.get("kind", "")))}</div>\n'
            f'      <div class="report-title">{html.escape(str(item.get("title", "")))}</div>\n'
            f'      <div class="report-summary">{html.escape(str(item.get("summary", "")))}</div>\n'
            "    </a>"
            for item in entries
        )
        body = f'  <div class="report-list">\n{rows}\n  </div>'
    else:
        body = "  <p>Разборов пока нет.</p>"

    count = f"{len(entries)} {_reports_word(len(entries))}"

    # Ссылка на расхождения стоит над списком, а не в футере: это самое
    # редкое, что система умеет, и ради него архив открывают чаще, чем
    # ради конкретной даты. Расхождений нет — нет и ссылки: пустая
    # страница по обещанию хуже отсутствия обещания.
    claims = sum(len(item.get("unconfirmed") or ()) for item in entries)
    if claims:
        body = (
            f'  <div class="nav-toc"><a href="{DIVERGENCES_NAME}">'
            f"сайт против документации · {claims} {_claims_word(claims)}</a></div>\n"
            f"{body}"
        )
    if entries:
        body = f"{_metrics(entries, claims)}\n{body}"

    return _shell(
        eyebrow="архив",
        title="Разборы howborisusesclaudecode.com",
        subtitle="Что появилось на сайте, что это значит и стоит ли оно времени.",
        description=f"Архив разборов: {count}.",
        body=body,
        footer="    <span>наблюдатель за howborisusesclaudecode.com</span>\n"
               f"    <span>{count}</span>",
        tail=f'\n<script type="application/json" id="reports">{_json_for_html(entries)}</script>',
    )


def update_index(
    entry: dict,
    *,
    repo: str,
    token: str,
    timeout_seconds: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Добавить разбор в индекс архива. Бросает PublishFailed.

    Вызывается после того, как страница уже опубликована, поэтому её
    судьба от исхода не зависит: сломанный индекс — повод для строки в
    журнале, а не для отмены доставки. Но и молчать нельзя, иначе архив
    тихо перестанет пополняться.
    """
    with _client(timeout_seconds, transport) as client:
        existing, sha = _read(client, repo, INDEX_NAME, token)
        entries = merge_entry(index_entries(existing), entry)
        _write(
            client,
            repo,
            INDEX_NAME,
            render_index(entries),
            sha=sha,
            token=token,
            message=f"index: {len(entries)} reports",
        )
        # Страница расхождений — производная от того же списка, поэтому
        # разойтись с архивом не может. Её отказ архив не отменяет: индекс
        # уже записан, а разборы лежат на своих адресах.
        try:
            _, claims_sha = _read(client, repo, DIVERGENCES_NAME, token)
            _write(
                client,
                repo,
                DIVERGENCES_NAME,
                render_divergences(entries),
                sha=claims_sha,
                token=token,
                message="divergences: site vs docs",
            )
        except PublishFailed as exc:
            log.warning("страница расхождений не обновлена: %s", exc)
    log.info("индекс архива обновлён: записей %d", len(entries))


# --------------------------------------------------------------------------
# Публикация
# --------------------------------------------------------------------------


def publish_page(
    page: str,
    *,
    name: str,
    repo: str,
    token: str,
    timeout_seconds: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Записать страницу в репозиторий отчётов через Contents API.

    Почему API, а не git: раннер эфемерный, и второй клон чужого
    репозитория ради одного файла — лишняя точка отказа и лишние права.
    PUT в Contents API делает ровно одно действие и отвечает кодом.

    Существующий файл перезаписывается по его sha — иначе GitHub отвергает
    запись как конфликт. Это и есть идемпотентность повтора: прогон,
    упавший между публикацией и отправкой, следующим разом перезапишет ту
    же страницу.

    Токен в сообщение об ошибке не попадает: он живёт только в заголовке.
    """
    with _client(timeout_seconds, transport) as client:
        _, sha = _read(client, repo, name, token)
        _write(
            client,
            repo,
            name,
            page,
            sha=sha,
            token=token,
            message=f"{'update' if sha else 'add'} report {name}",
        )
    log.info("страница опубликована: %s", name)


def _client(timeout_seconds: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout_seconds), transport=transport)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _read(
    client: httpx.Client, repo: str, name: str, token: str
) -> tuple[str | None, str | None]:
    """Прочитать файл из репозитория: текст и sha. Нет файла — (None, None).

    sha обязателен для перезаписи: без него GitHub считает запись
    конфликтом и отвергает её. Содержимое и sha приходят одним ответом,
    поэтому второго запроса за ним не нужно — на этом и стоит индекс,
    который носит свои данные в себе.
    """
    try:
        response = client.get(
            f"{GITHUB_API}/repos/{repo}/contents/{name}", headers=_headers(token)
        )
    except httpx.HTTPError as exc:
        raise PublishFailed(f"репозиторий отчётов недоступен: {exc}") from exc

    if response.status_code == 404:
        return None, None
    if response.status_code != 200:
        raise PublishFailed(
            f"репозиторий отчётов не отвечает: HTTP {response.status_code} — {_reason(response)}"
        )

    payload = response.json()
    content = payload.get("content") or ""
    text = base64.b64decode(content).decode("utf-8", errors="replace") if content else None
    return text, payload.get("sha")


def _write(
    client: httpx.Client,
    repo: str,
    name: str,
    content: str,
    *,
    sha: str | None,
    token: str,
    message: str,
) -> None:
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    try:
        response = client.put(
            f"{GITHUB_API}/repos/{repo}/contents/{name}",
            headers=_headers(token),
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise PublishFailed(f"публикация не дошла: {exc}") from exc

    if response.status_code not in (200, 201):
        raise PublishFailed(
            f"{name} не записан: HTTP {response.status_code} — {_reason(response)}"
        )


# Расписание опроса: сначала часто, потом реже. Замер 30 июля 2026 — новая
# страница поднялась на 50-й секунде; чаще всего Pages укладывается в
# полминуты, и первые короткие паузы экономят время всего прогона.
PAGE_POLL_DELAYS = (3, 3, 5, 5, 10, 10, 10, 15, 15, 15)

# Потолок ожидания по часам, а не по расписанию пауз. Расписание считает
# только сон, а каждый запрос может ещё и висеть до своего таймаута —
# восемь событий за прогон, и такое ожидание втрое перекрывает лимит
# задачи в GitHub Actions. Прогон не должен умирать по таймауту из-за
# того, что Pages задумался.
PAGE_WAIT_BUDGET_SECONDS = 100.0


def wait_for_page(
    url: str,
    *,
    delays: tuple[int, ...] = PAGE_POLL_DELAYS,
    budget_seconds: float = PAGE_WAIT_BUDGET_SECONDS,
    timeout_seconds: float = 5.0,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Дождаться, пока страница начнёт отдаваться по своему адресу.

    Записать файл и опубликовать страницу — не одно и то же. Contents API
    отвечает мгновенно, а GitHub Pages пересобирает сайт: 30 июля 2026
    страница ответила 200 только через 50 секунд после успешной записи.
    Карточка, отправленная сразу, дала бы читателю ссылку на 404 ровно в
    тот момент, когда он её нажмёт.

    Вернуло False — страницы по адресу пока нет. Это не сбой публикации:
    файл записан и поднимется сам, но ссылку на него давать уже нельзя,
    и вызывающий уходит на полный текст.
    """
    deadline = time.monotonic() + budget_seconds
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        for index, delay in enumerate((0, *delays)):
            if delay:
                if time.monotonic() + delay > deadline:
                    break
                time.sleep(delay)
            # Таймаут запроса урезается остатком бюджета. httpx.Timeout
            # ограничивает отдельные операции — соединение, чтение, — а не
            # вызов целиком: с редиректами и медленной отдачей один GET
            # уезжает за дедлайн, и обещанная граница перестаёт быть
            # границей. Проверка перед сном этого не ловит.
            #
            # Первая попытка делается всегда, даже при исчерпанном
            # бюджете: она стоит одного быстрого запроса и закрывает самый
            # частый случай — страница на месте с прошлого раза.
            left = deadline - time.monotonic()
            if index and left <= 0:
                break
            try:
                response = client.get(
                    url,
                    headers={"Cache-Control": "no-cache"},
                    timeout=httpx.Timeout(timeout_seconds if not index else min(timeout_seconds, left)),
                )
            except httpx.HTTPError as exc:
                log.debug("страница ещё не отвечает: %s", exc)
                continue
            if response.status_code == 200:
                log.info("страница поднялась с попытки %d: %s", index + 1, url)
                return True
    log.warning("страница не поднялась за отведённое время: %s", url)
    return False


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:200]
    except (ValueError, json.JSONDecodeError):
        log.debug("ответ GitHub не разбирается как JSON", exc_info=True)
        return response.text[:200]

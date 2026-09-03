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
    "publish_page",
    "render_page",
]

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


def page_name(event: Event, when: datetime) -> str:
    """Имя файла страницы: дата, часть и блок.

    Не транслит заголовка: имя обязано быть одинаковым при повторе. Прогон,
    упавший после публикации, но до отправки, повторится следующим разом —
    и должен перезаписать ту же страницу, а не создать вторую. `bid`
    вычислен из содержимого и даёт это даром.
    """
    return f"{when:%Y-%m-%d}-part{event.part_number}-{event.bid.removeprefix('b-')}.html"


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
        _stats(analysis, usage, price_usd),
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


def _head(analysis: Analysis, event: Event) -> str:
    summary = (
        f"Стоит ли: {_VERDICT_SHORT[analysis.verdict.worth_it]} · "
        f"Windows: {_WINDOWS_SHORT[analysis.windows.status]}"
    )
    if analysis.unconfirmed:
        summary += f" · расхождений с документацией: {len(analysis.unconfirmed)}"
    summary += f". Part {event.part_number}, {_KIND_BADGE.get(event.kind, event.kind)}."
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


def _stats(analysis: Analysis, usage: Usage | None, price_usd: float | None) -> str:
    cells: list[tuple[str, str, bool]] = [
        (_VERDICT_SHORT[analysis.verdict.worth_it], "стоит ли тебе", False),
        (_WINDOWS_SHORT[analysis.windows.status], "на Windows", False),
    ]
    if usage is not None:
        cells.append((str(usage.searches), "поисков по докам", False))
    cells.append(
        (str(len(analysis.unconfirmed)), "расхождения с докой", bool(analysis.unconfirmed))
    )
    if price_usd is not None:
        cells.append((f"${price_usd:.2f}", "стоил разбор", False))

    rendered = "\n".join(
        f'    <div><div class="stat-value{" bad" if bad else ""}">{html.escape(value)}</div>'
        f'<div class="stat-label">{label}</div></div>'
        for value, label, bad in cells
    )
    return f'  <div class="stats-row">\n{rendered}\n  </div>'


def _glance(analysis: Analysis, prose: _Prose) -> str:
    rows = [
        ("Стоит ли", prose(analysis.verdict.why)),
        ("Windows", prose(analysis.windows.detail)),
    ]
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
        for url in urls
    )
    return _section("sources", "Источники", f'  <div class="sources">\n{links}\n  </div>')


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
        "    <span>наблюдатель за howborisusesclaudecode.com</span>",
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
    url = f"{GITHUB_API}/repos/{repo}/contents/{name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: dict[str, str] = {
        "message": f"add report {name}",
        "content": base64.b64encode(page.encode("utf-8")).decode("ascii"),
    }

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds), transport=transport) as client:
            existing = client.get(url, headers=headers)
            if existing.status_code == 200:
                payload["sha"] = existing.json().get("sha", "")
                payload["message"] = f"update report {name}"
            elif existing.status_code != 404:
                raise PublishFailed(
                    f"репозиторий отчётов не отвечает: HTTP {existing.status_code} — "
                    f"{_reason(existing)}"
                )

            written = client.put(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise PublishFailed(f"публикация не дошла: {exc}") from exc

    if written.status_code not in (200, 201):
        raise PublishFailed(
            f"страница не записана: HTTP {written.status_code} — {_reason(written)}"
        )
    log.info("страница опубликована: %s", name)


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:200]
    except (ValueError, json.JSONDecodeError):
        log.debug("ответ GitHub не разбирается как JSON", exc_info=True)
        return response.text[:200]

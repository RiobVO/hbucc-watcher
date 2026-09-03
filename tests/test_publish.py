"""Тесты страницы разбора и её публикации.

Три вещи, которые здесь ломаются тихо и потому проверяются явно:

  * ЭКРАНИРОВАНИЕ. Текст пишет модель по материалам чужого сайта. На
    странице он попадает в HTML напрямую — то есть незакрытый тег или
    угловая скобка из чужого текста способны переписать разметку. Проверка
    идёт до самого низа: страница целиком разбирается парсером, и все теги
    обязаны сойтись.

  * ДЕГРАДАЦИЯ. Не опубликовалось — читатель получает полный текст, как
    раньше. Карточка со ссылкой на 404 хуже простыни, поэтому publish_page
    обязан падать явным исключением, а не возвращать адрес несуществующей
    страницы.

  * ИДЕМПОТЕНТНОСТЬ. Повторная попытка после сбоя обязана перезаписать ту
    же страницу. У GitHub Contents API это означает: сначала узнать sha
    существующего файла, потом писать. Без sha повторная запись отвергается.

Отдельный тест рендерит настоящий разбор из tests/fixtures — рукописная
фикстура не содержит того, что модель делает на самом деле (обратные
кавычки внутри текстовых полей, например).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from conftest import make_block, make_part

from watcher.analyze import Analysis, Layers, Usage, Verdict, Windows
from watcher.detect import BLOCK_ADDED, Event
from watcher.original import Author
from watcher.publish import (
    PublishFailed,
    page_name,
    page_url,
    publish_page,
    render_page,
)

FIXTURE = Path(__file__).parent / "fixtures" / "analysis-part22-6.json"
WHEN = datetime(2026, 7, 30, 22, 26, tzinfo=timezone.utc)

# Заглушка вместо настоящего PAT. Живёт константой, чтобы в тестах не
# появлялось ничего похожего на записанный в код секрет.
FAKE = "not-a-real-value"

# Пустые элементы: закрывающего тега у них нет по стандарту, и стек
# проверки баланса не должен их ждать.
VOID = {"meta", "link", "br", "img", "input", "hr", "source"}


def make_analysis(**overrides) -> Analysis:
    data = {
        "headline": "Opus 5 добавили как аргумент в пользу Auto Mode",
        "what_it_is": "Совет про то, почему новым правилам вообще можно доверять.",
        "how_it_works": "Классификатор проверяет опасные действия в фоне.",
        "example": "Запусти `claude --permission-mode auto` из каталога проекта.",
        "how_to_verify": "В строке состояния CLI виден Auto Mode.",
        "pitfalls": ["Граница из чата теряется после compaction."],
        "related": ["Permission modes", "Configure auto mode"],
        "layers": Layers(
            original_author="В посте только про устойчивость модели к инъекциям.",
            site_author="Связку с Part 15 дописал автор сайта.",
            official_docs="Режим описан, но Opus 5 в списке моделей не указан.",
            my_conclusion="Пробовать стоит только на длинных локальных задачах.",
        ),
        "windows": Windows(status="works", detail="Ни путей, ни хоткеев тут нет."),
        "verdict": Verdict(worth_it="maybe", why="Смотря сколько у тебя подтверждений в день."),
        "action": "Запусти режим на безопасной локальной задаче.",
        "sources": ["https://x.com/bcherny/status/2080713091688583312"],
        "unconfirmed": ["Совместимость Opus 5 с Auto Mode не подтверждена."],
        "anomalies": [],
    }
    data.update(overrides)
    return Analysis(**data)


@pytest.fixture
def event() -> Event:
    block = make_block(
        "Opus 5 landed the same day.",
        heading="Opus 5 - And the Model That's Hardest to Inject",
        bid="b-be1681fe",
    )
    part = make_part(22, [block], title="The New Rules of Context Engineering")
    return Event(
        kind=BLOCK_ADDED,
        part_number=part.number,
        part_title=part.title,
        bid=block.bid,
        new_block=block,
    )


@pytest.fixture
def page(event) -> str:
    return render_page(
        make_analysis(),
        event,
        author=Author(handle="@bcherny", name="Boris Cherny", bio="Claude Code @anthropicai"),
        site_author="@CarolinaCherry",
        usage=Usage(searches=3, input_tokens=41670, output_tokens=6646),
        price_usd=0.2339,
        generated_at=WHEN,
    )


class TagBalance(HTMLParser):
    """Стек открытых тегов. Пустой в конце — разметка сошлась."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"закрыт непокрытый тег </{tag}>")
        elif self.stack[-1] != tag:
            self.errors.append(f"ожидался </{self.stack[-1]}>, встретился </{tag}>")
        else:
            self.stack.pop()


def balance(html: str) -> TagBalance:
    parser = TagBalance()
    parser.feed(html)
    return parser


def body_of(page: str) -> str:
    """Страница без встроенного CSS.

    Иначе проверка «такой секции на странице нет» находит имя класса в
    таблице стилей и зеленеет там, где должна краснеть.
    """
    return page.split("</style>", 1)[1]


# --------------------------------------------------------------- содержимое


def test_page_carries_headline_and_part(page):
    assert "Opus 5 добавили как аргумент в пользу Auto Mode" in page
    assert "Part 22" in page
    assert "The New Rules of Context Engineering" in page


def test_stylesheet_is_inlined_not_linked(page):
    assert "<style>" in page
    assert ".at-a-glance" in page, "вёрстка не подставилась"
    assert 'href="report.css"' not in page


def test_all_three_new_fields_have_their_place(page):
    assert "Как проверить" in page
    assert "Где споткнёшься" in page
    assert "Рядом" in page


def test_pitfalls_are_a_list_not_a_paragraph(page):
    assert '<ul class="plain">' in page
    assert "Граница из чата теряется после compaction." in page


def test_related_render_as_separate_chips(page):
    """Строкой через запятую это не читается: в названиях есть свои тире."""
    assert '<div class="chips">' in page
    assert "<span>Permission modes</span>" in page
    assert "<span>Configure auto mode</span>" in page


def test_backticks_become_inline_code(page):
    """Модель размечает команды обратными кавычками вопреки промту.

    В Telegram это мусор, а здесь — готовая разметка: ровно эти куски на
    одобренных страницах оформлены <code>.
    """
    assert "<code>claude --permission-mode auto</code>" in page
    assert "`" not in page


def test_long_config_becomes_a_copyable_block(event):
    analysis = make_analysis(
        example='В `.claude/settings.json` укажи `{ "permissions": { "defaultMode": "auto" } }` и перезапусти.'
    )
    page = render_page(analysis, event, generated_at=WHEN)
    assert '<div class="code-block">' in page
    assert "<pre>" in page
    assert "copy-btn" in page
    # Короткий путь остаётся в строке, а не уезжает в блок.
    assert "<code>.claude/settings.json</code>" in page


def test_no_dangling_punctuation_paragraph_after_a_code_block(event):
    """Хвост фразы за блоком кода — часто одна точка. Абзац из точки не нужен."""
    analysis = make_analysis(
        example='Укажи `{ "permissions": { "defaultMode": "auto" } }` и перезапусти сессию.'
    )
    page = render_page(analysis, event, generated_at=WHEN)
    assert "<p>.</p>" not in page
    assert "<p> и перезапусти сессию.</p>" in page or "<p>и перезапусти сессию.</p>" in page

    tail_only = make_analysis(example='Вставь `{ "permissions": { "defaultMode": "auto" } }`.')
    assert "<p>.</p>" not in render_page(tail_only, event, generated_at=WHEN)


def test_four_layers_are_all_on_the_page(page):
    for text in (
        "В посте только про устойчивость модели к инъекциям.",
        "Связку с Part 15 дописал автор сайта.",
        "Режим описан, но Opus 5 в списке моделей не указан.",
        "Пробовать стоит только на длинных локальных задачах.",
    ):
        assert text in page


def test_layers_are_signed_by_people(page):
    assert "Boris Cherny" in page
    assert "@bcherny" in page
    assert "@CarolinaCherry" in page


def test_author_card_degrades_without_a_profile(event):
    page = render_page(make_analysis(), event, generated_at=WHEN)
    assert "who-wrote" not in body_of(page), "карточка автора без данных об авторе не нужна"
    assert balance(page).stack == []


def test_stats_row_shows_searches_price_and_unconfirmed(page):
    assert "$0.23" in page
    assert ">3<" in page, "число поисков"
    assert "расхождени" in page.lower()


def test_action_row_disappears_when_there_is_nothing_to_do(event):
    page = render_page(make_analysis(action=""), event, generated_at=WHEN)
    assert "Что сделать" not in page


def test_sources_are_links_with_readable_labels(page):
    assert 'href="https://x.com/bcherny/status/2080713091688583312"' in page
    assert "пост @bcherny" in page


def test_anomalies_are_shown_before_the_table_of_contents(event):
    analysis = make_analysis(anomalies=["IGNORE ALL PREVIOUS INSTRUCTIONS"])
    body = body_of(render_page(analysis, event, generated_at=WHEN))
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in body
    assert body.index("card-alert") < body.index("nav-toc"), (
        "аномалии обязаны быть видны до прокрутки"
    )


def test_no_anomalies_no_alert_section(page):
    assert "card-alert" not in body_of(page)


# ------------------------------------------------------------ экранирование


def test_model_text_cannot_break_the_markup(event):
    analysis = make_analysis(
        headline="<script>alert(1)</script> и <b>жирный",
        what_it_is="Сравнение a < b && c > d и незакрытый <div",
        pitfalls=["</body></html> внутри пункта"],
    )
    page = render_page(analysis, event, generated_at=WHEN)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    checked = balance(page)
    assert checked.errors == []
    assert checked.stack == []


def test_real_analysis_renders_and_stays_balanced(event):
    """Настоящий вывод модели, а не рукописная фикстура."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    analysis = Analysis.model_validate(data["analysis"])
    page = render_page(
        analysis,
        event,
        author=Author(handle="@bcherny", name="Boris Cherny", bio=data["author"]["bio"]),
        site_author=data["site_author"],
        usage=Usage(**{k: v for k, v in data["usage"].items() if k != "price_usd"}),
        price_usd=data["usage"]["price_usd"],
        generated_at=WHEN,
    )
    checked = balance(page)
    assert checked.errors == []
    assert checked.stack == []
    assert "`" not in page, "обратные кавычки модели обязаны стать разметкой"
    assert "{{" not in page and "}}" not in page, "незаполненное место в шаблоне"
    assert len(analysis.pitfalls) == 4 and page.count('<ul class="plain">') >= 1


# ------------------------------------------------------------------- адреса


def test_page_name_is_the_same_for_the_same_event(event):
    assert page_name(event, WHEN) == page_name(event, WHEN)


def test_page_name_carries_date_part_and_block(event):
    assert page_name(event, WHEN) == "2026-07-30-part22-be1681fe.html"


def test_page_url_joins_base_and_name():
    assert page_url("https://riobvo.github.io/hbucc-reports", "a.html") == (
        "https://riobvo.github.io/hbucc-reports/a.html"
    )
    assert page_url("https://riobvo.github.io/hbucc-reports/", "a.html") == (
        "https://riobvo.github.io/hbucc-reports/a.html"
    )


# -------------------------------------------------------------- публикация


def test_new_page_is_created_with_its_content(page):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"content": {"sha": "abc"}})

    publish_page(
        page,
        name="2026-07-30-part22-be1681fe.html",
        repo="RiobVO/hbucc-reports",
        token=FAKE,
        transport=httpx.MockTransport(handler),
    )

    assert seen["url"].endswith(
        "/repos/RiobVO/hbucc-reports/contents/2026-07-30-part22-be1681fe.html"
    )
    assert seen["auth"] == f"Bearer {FAKE}"
    body = seen["body"]
    assert base64.b64decode(body["content"]).decode("utf-8") == page
    assert "sha" not in body, "новый файл создаётся без sha"


def test_existing_page_is_overwritten_by_sha(page):
    """Повтор после сбоя обязан перезаписать ту же страницу, а не упасть."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "old-sha"})
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": {"sha": "new-sha"}})

    publish_page(
        page,
        name="page.html",
        repo="RiobVO/hbucc-reports",
        token=FAKE,
        transport=httpx.MockTransport(handler),
    )
    assert seen["body"]["sha"] == "old-sha"


def test_refused_write_raises_instead_of_returning(page):
    """Молчаливый успех дал бы карточку со ссылкой на 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(
            403, json={"message": "Resource not accessible by personal access token"}
        )

    with pytest.raises(PublishFailed) as exc:
        publish_page(
            page,
            name="page.html",
            repo="RiobVO/hbucc-reports",
            token=FAKE,
            transport=httpx.MockTransport(handler),
        )
    assert "403" in str(exc.value)


def test_network_failure_raises_publish_failed(page):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть недоступна")

    with pytest.raises(PublishFailed):
        publish_page(
            page,
            name="page.html",
            repo="RiobVO/hbucc-reports",
            token=FAKE,
            transport=httpx.MockTransport(handler),
        )


def test_credential_never_appears_in_the_error(page):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(PublishFailed) as exc:
        publish_page(
            page,
            name="page.html",
            repo="RiobVO/hbucc-reports",
            token=FAKE,
            transport=httpx.MockTransport(handler),
        )
    assert FAKE not in str(exc.value)

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
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import balance, make_block, make_part

from watcher.analyze import Analysis, Layers, Usage, Verdict, Windows
from watcher.detect import BLOCK_ADDED, BLOCK_EDITED, Event
from watcher.original import Author
from watcher.publish import (
    PublishFailed,
    page_name,
    page_url,
    publish_page,
    render_page,
    index_entries,
    merge_entry,
    render_index,
    update_index,
    wait_for_page,
)

FIXTURE = Path(__file__).parent / "fixtures" / "analysis-part22-6.json"
WHEN = datetime(2026, 7, 30, 22, 26, tzinfo=timezone.utc)

# Заглушка вместо настоящего PAT. Живёт константой, чтобы в тестах не
# появлялось ничего похожего на записанный в код секрет.
FAKE = "not-a-real-value"

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


def stats_row(page: str) -> str:
    """Только ряд метрик: дальше идёт блок «Коротко», и он не в счёт."""
    return page.split('class="stats-row"', 1)[1].split("at-a-glance", 1)[0]


def test_stats_row_is_about_the_reader_not_the_bill(page):
    """Ряд метрик отвечает на «читать ли дальше», а не «сколько стоило».

    Число поисков и цена уже стоят в футере. В самом заметном месте
    страницы они занимали два тайла из пяти, на вопрос читателя не
    отвечали, а на узком экране вытесняли то, что отвечает.
    """
    row = stats_row(page)
    assert "поисков" not in row
    assert "стоил разбор" not in row
    assert "поиска" in page.split("<footer>", 1)[1], "расход пропал со страницы совсем"


def test_stats_row_counts_pitfalls(page):
    """Число ловушек — величина про читателя, ей место в ряду метрик."""
    assert "ловушек" in stats_row(page)


def test_verdict_and_windows_carry_their_tone(event):
    """Цвет стоит там, где решение, а не только на расхождениях.

    «Не стоит» и «только macOS» — то, ради чего страницу открывают, и
    нейтральными они читались наравне с подписью «поисков по докам».
    """
    page = render_page(
        make_analysis(
            verdict=Verdict(worth_it="no", why="Ничего нового."),
            windows=Windows(status="macos_only", detail="Хоткей только для macOS."),
        ),
        event,
        generated_at=WHEN,
    )
    row = stats_row(page)
    assert "stat-value muted" in row, "вердикт «не стоит» ничем не отмечен"
    assert "stat-value bad" in row, "«только macOS» ничем не отмечен"


def test_footer_leads_back_to_the_archive(page):
    """Со страницы разбора должно быть куда пойти дальше."""
    assert 'href="index.html"' in page.split("<footer>", 1)[1]


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


def test_cost_lives_in_the_footer_only(page):
    """Расход со страницы не пропал, но ушёл туда, где ему место.

    Раньше цена и число поисков стояли в ряду метрик. Это ответ на вопрос
    «во что обошлось владельцу», а ряд метрик отвечает на «стоит ли это
    твоего времени» — и два тайла из пяти уходили не на того читателя.
    """
    footer = page.split("<footer>", 1)[1]
    assert "$0.23" in footer
    assert "3 поиска" in footer
    assert "расхождени" in stats_row(page).lower()


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
    # Именно {{ИМЯ}}, а не любые двойные скобки: в примере модели стоит
    # настоящий JSON вида {"permissions":{"defaultMode":"auto"}}, и он
    # кончается на «}}» совершенно законно.
    assert not re.search(r"\{\{[A-Z_]+\}\}", page), "незаполненное место в шаблоне"
    assert len(analysis.pitfalls) == 4 and page.count('<ul class="plain">') >= 1


# ------------------------------------------------------------------- адреса


def test_page_name_is_the_same_for_the_same_event(event):
    assert page_name(event) == page_name(event)


def test_page_name_carries_part_block_and_event(event):
    name = page_name(event)
    assert name.startswith("part22-be1681fe-") and name.endswith(".html")
    assert event.event_id.removeprefix("sha256:")[:8] in name


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
        name="part22-be1681fe.html",
        repo="RiobVO/hbucc-reports",
        token=FAKE,
        transport=httpx.MockTransport(handler),
    )

    assert seen["url"].endswith(
        "/repos/RiobVO/hbucc-reports/contents/part22-be1681fe.html"
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


# ------------------------------------------------- страница по своему адресу


def test_ready_page_needs_no_waiting():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    assert wait_for_page(
        "https://example.com/a.html", delays=(), transport=httpx.MockTransport(handler)
    )
    assert len(calls) == 1


def test_page_that_appears_late_is_still_caught():
    """Замер 30 июля: Pages поднял страницу только через 50 секунд."""
    codes = iter([404, 404, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(codes))

    assert wait_for_page(
        "https://example.com/a.html", delays=(0, 0), transport=httpx.MockTransport(handler)
    )


def test_page_that_never_appears_reports_it():
    """False, а не исключение: файл записан, но ссылку давать уже нельзя."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert not wait_for_page(
        "https://example.com/a.html", delays=(0,), transport=httpx.MockTransport(handler)
    )


def test_unreachable_host_is_not_a_ready_page():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть недоступна")

    assert not wait_for_page(
        "https://example.com/a.html", delays=(0,), transport=httpx.MockTransport(handler)
    )


# ----------------------------------------------------------- индекс архива


def entry(name: str, title: str = "Разбор", part: int = 22, date: str = "2026-07-30") -> dict:
    return {
        "name": name,
        "title": title,
        "summary": "Стоит ли: нет · Windows: работает",
        "date": date,
        "part": part,
        "kind": "новый совет",
    }


def test_index_lists_every_report():
    page = render_index([entry("a.html", "Первый"), entry("b.html", "Второй", part=21)])
    assert 'href="a.html"' in page and 'href="b.html"' in page
    assert "Первый" in page and "Второй" in page


def test_index_carries_its_own_data_for_the_next_run():
    """Индекс — сам себе манифест: отдельный файл рядом однажды разъедется."""
    entries = [entry("a.html", "Первый")]
    assert index_entries(render_index(entries)) == entries


def test_index_of_an_empty_repository_is_still_a_page():
    page = render_index([])
    assert page.startswith("<!DOCTYPE html>")
    assert index_entries(page) == []


def test_missing_index_reads_as_no_entries():
    assert index_entries(None) == []
    assert index_entries("<html>руками переписали</html>") == []


def test_same_report_published_twice_gets_one_line():
    entries = merge_entry([entry("a.html", "Старый заголовок")], entry("a.html", "Новый заголовок"))
    assert len(entries) == 1
    assert entries[0]["title"] == "Новый заголовок"


def test_newest_report_comes_first():
    entries = merge_entry(
        [entry("old.html", "Старый", date="2026-07-01")],
        entry("new.html", "Новый", date="2026-07-30"),
    )
    assert [e["name"] for e in entries] == ["new.html", "old.html"]


def test_index_escapes_the_model_text():
    page = render_index([entry("a.html", "<script>alert(1)</script>")])
    assert "<script>alert(1)</script>" not in page.split("id=\"reports\"")[0]
    assert balance(page).stack == []


def test_index_is_written_back_with_its_sha():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            existing = render_index([entry("old.html", "Старый", date="2026-07-01")])
            return httpx.Response(
                200,
                json={
                    "sha": "index-sha",
                    "content": base64.b64encode(existing.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": {"sha": "new"}})

    update_index(
        entry("new.html", "Новый"),
        repo="RiobVO/hbucc-reports",
        token=FAKE,
        transport=httpx.MockTransport(handler),
    )
    body = seen["body"]
    assert body["sha"] == "index-sha"
    written = base64.b64decode(body["content"]).decode("utf-8")
    assert [e["name"] for e in index_entries(written)] == ["new.html", "old.html"]


def test_index_failure_is_reported_not_swallowed(page):
    """Страница уже опубликована — но молчать о сломанном индексе нельзя."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "Internal Server Error"})

    with pytest.raises(PublishFailed):
        update_index(
            entry("a.html"),
            repo="RiobVO/hbucc-reports",
            token=FAKE,
            transport=httpx.MockTransport(handler),
        )


def test_the_archive_counts_in_russian():
    counts = {1: "1 разбор", 2: "2 разбора", 4: "4 разбора", 5: "5 разборов",
              11: "11 разборов", 21: "21 разбор", 22: "22 разбора", 25: "25 разборов"}
    for number, expected in counts.items():
        page = render_index([entry(f"{i}.html") for i in range(number)])
        assert expected in page, f"{number} -> ожидалось «{expected}»"


# --------------------------------------------- находки независимого ревью


def test_a_javascript_url_never_becomes_a_clickable_link(event):
    """`sources` — строки от модели по чужому материалу, схема их не проверяет.

    Экранирование кавычек схему не обезвреживает: javascript:… в href
    остаётся рабочей ссылкой на публичной странице.
    """
    analysis = make_analysis(
        sources=[
            "javascript:alert(document.domain)",
            "data:text/html,<script>alert(1)</script>",
            "https://code.claude.com/docs/en/hooks",
        ]
    )
    page = render_page(analysis, event, generated_at=WHEN)
    assert 'href="javascript:' not in page
    assert 'href="data:' not in page
    # Адрес не исчезает: читатель обязан видеть, на что ссылался материал,
    # — просто нажимать на это он не будет.
    assert "javascript:alert(document.domain)" in page
    assert 'href="https://code.claude.com/docs/en/hooks"' in page
    assert balance(page).errors == []


def test_a_title_cannot_escape_the_index_data_block():
    """`</script>` в заголовке закрывает блок данных и делает разметку кодом.

    json.dumps не экранирует `<`, а HTML закрывает script-data на первом
    же `</script` — то есть заголовок от модели вырывается наружу.
    """
    hostile = entry("a.html", "Разбор </script><script>alert(1)</script> и дальше")
    page = render_index([hostile])

    assert "</script><script>alert(1)" not in page
    # Данные обязаны пережить круг: иначе архив обнуляется на первом же
    # заголовке с угловой скобкой.
    assert index_entries(page) == [hostile]


def test_page_name_survives_a_retry_after_midnight(event):
    """Повтор приходит следующим прогоном — иногда уже в другие сутки.

    Имя, взятое от часов, дало бы вторую страницу того же события и вторую
    строку в архиве.
    """
    assert not re.search(r"20\d\d", page_name(event)), "в имени не должно быть даты"


def test_two_events_on_the_same_block_get_different_pages():
    """Правка того же совета — другое событие и другая страница.

    Иначе разбор правки затёр бы разбор появления, а карточка, отправленная
    раньше, стала бы вести на чужой текст.
    """
    old = make_block("Old text of the advice.", heading="Auto Mode", bid="b-be1681fe")
    new = make_block("New text of the advice.", heading="Auto Mode", bid="b-be1681fe")
    added = Event(kind=BLOCK_ADDED, part_number=22, part_title="T", bid=old.bid, new_block=old)
    edited = Event(
        kind=BLOCK_EDITED, part_number=22, part_title="T", bid=new.bid,
        old_block=old, new_block=new,
    )
    assert page_name(added) != page_name(edited)


def test_waiting_stops_when_the_time_budget_runs_out():
    """Прогон не должен умирать по таймауту оттого, что Pages задумался.

    Расписание пауз считает только сон; каждый запрос сверх того может
    висеть до своего таймаута, а событий за прогон до восьми.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    assert not wait_for_page(
        "https://example.com/a.html",
        delays=(30, 30, 30),
        budget_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    assert len(calls) == 1, "после исчерпанного бюджета опрос не продолжается"


def test_a_slow_page_cannot_stretch_the_wait_past_its_budget():
    """Бюджет обязан ограничивать и сам запрос, а не только паузы.

    httpx.Timeout ограничивает отдельные операции, а не вызов целиком:
    с редиректами и медленной отдачей один GET уезжает за дедлайн, и
    обещанная граница перестаёт быть границей. Восемь событий за прогон —
    и такое ожидание съедает лимит задачи в GitHub Actions.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.2)
        return httpx.Response(404)

    started = time.monotonic()
    assert not wait_for_page(
        "https://example.com/a.html",
        delays=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        budget_seconds=0.3,
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )
    spent = time.monotonic() - started
    assert spent < 1.0, f"ожидание заняло {spent:.2f} с при бюджете 0.3 с"


def test_the_event_id_in_the_name_is_wide_enough_to_forget_about(event):
    """Восемь hex-знаков — 32 бита, порог дня рождения 65 тысяч событий."""
    suffix = page_name(event).removesuffix(".html").rsplit("-", 1)[1]
    assert len(suffix) >= 16


def test_the_page_keeps_the_windows_tile_but_drops_the_duplicate_row(event):
    """На странице места хватает: тайл сканируется глазами и стоит бесплатно.

    А строка в «Коротко» повторяла его словами — и ради статуса works
    занимала место, ничего не добавляя.
    """
    works = render_page(make_analysis(), event, generated_at=WHEN)
    glance = works.split('class="at-a-glance"')[1].split("</div>\n  <nav")[0]
    assert "работает" in works.split('class="stats-row"')[1][:600], "тайл обязан остаться"
    assert "Windows" not in glance, "строка в «Коротко» дублирует тайл"

    adapted = render_page(
        make_analysis(windows=Windows(status="needs_adaptation", detail="Ставь shell powershell.")),
        event, generated_at=WHEN,
    )
    glance = adapted.split('class="at-a-glance"')[1].split("</div>\n  <nav")[0]
    assert "Windows" in glance
    assert "Ставь shell powershell." in glance

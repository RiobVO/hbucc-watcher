"""Тесты загрузчика первоисточников.

Здесь проходит граница доверия в её новом виде. Раньше белый список
доменов применял API провайдера; теперь его применяет наш код, и значит
он обязан быть проверен так же строго, как проверялась граница раньше.

Сеть не трогается: httpx.MockTransport подставляет ответы. Это не
ослабление — предмет проверки в том, ЧТО мы решаем делать с ответом, а не
в том, умеет ли httpx ходить в сеть.
"""

from __future__ import annotations

import httpx
import pytest

from watcher.original import (
    STATUS_OK,
    STATUS_NO_TEXT,
    STATUS_REDIRECTED,
    STATUS_REFUSED,
    Original,
    author_handle,
    domain_allowed,
    extract_text,
    fetch_author,
    fetch_originals,
)

ALLOWED = ["x.com", "docs.claude.com", "code.claude.com", "claude.com"]

POST_HTML = """<!doctype html><html><head>
<title>Boris Cherny on X: &quot;1/ I run 5 Claudes&quot;</title>
<meta property="og:description" content="1/ I run 5 Claudes in parallel in my terminal.">
</head><body><div>JS shell</div></body></html>"""

NO_OG_HTML = """<!doctype html><html><head>
<title>Claude Code settings - Claude Code Docs</title>
</head><body>текст</body></html>"""


def transport(handler):
    return httpx.MockTransport(handler)


def always(html: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, html=html)

    return handler


# --------------------------------------------------------------------------
# Белый список — применяется ДО запроса
# --------------------------------------------------------------------------


def test_allowed_domain_is_fetched():
    result = fetch_originals(
        ["https://x.com/bcherny/status/1"], ALLOWED, transport=transport(always(POST_HTML))
    )
    assert len(result) == 1
    assert result[0].status == "ok"
    assert "5 Claudes in parallel" in result[0].text


def test_subdomain_is_allowed():
    result = fetch_originals(
        ["https://mobile.x.com/bcherny/status/1"], ALLOWED, transport=transport(always(POST_HTML))
    )
    assert result[0].status == "ok"


def test_lookalike_domains_are_never_requested():
    """Ключевая проверка: запрос не должен уйти вообще.

    Недостаточно отбросить ответ — нельзя даже постучаться, иначе мы
    сообщаем чужому серверу, что читаем его страницу.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, html=POST_HTML)

    result = fetch_originals(
        ["https://x.com.evil.ru/p", "https://evil-x.com/p", "https://pastebin.com/raw/x"],
        ALLOWED,
        transport=transport(handler),
    )
    assert requested == []
    assert [r.status for r in result] == ["refused_domain"] * 3
    assert all(r.text == "" for r in result)


def test_refused_url_is_still_reported():
    """Отказ не делает ссылку невидимой — она обязана попасть в разбор."""
    result = fetch_originals(
        ["https://pastebin.com/raw/x"], ALLOWED, transport=transport(always(POST_HTML))
    )
    assert result[0].url == "https://pastebin.com/raw/x"
    assert result[0].host == "pastebin.com"


def test_malformed_url_is_refused():
    """Мусор в поле ссылки — отвергается, но остаётся видимым.

    Пустая строка при этом отбрасывается совсем: это не ссылка, о которой
    читателю надо знать, а отсутствие ссылки.
    """
    result = fetch_originals(["not-a-url", ""], ALLOWED, transport=transport(always(POST_HTML)))
    assert [(r.url, r.status) for r in result] == [("not-a-url", "refused_domain")]


# --------------------------------------------------------------------------
# Редирект — вторая проверка домена, уже по конечному адресу
# --------------------------------------------------------------------------


def test_redirect_off_the_whitelist_is_rejected():
    """Разрешённый адрес, уводящий редиректом наружу, доверия не наследует.

    Без этой проверки белый список обходится одним 302 на чужом сервере.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "x.com":
            return httpx.Response(302, headers={"Location": "https://evil.ru/p"})
        return httpx.Response(200, html=POST_HTML)

    result = fetch_originals(
        ["https://x.com/bcherny/status/1"], ALLOWED, transport=transport(handler)
    )
    assert result[0].status == "redirected_off_whitelist"
    assert result[0].text == ""


def test_redirect_inside_whitelist_is_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "docs.claude.com":
            return httpx.Response(301, headers={"Location": "https://code.claude.com/docs/x"})
        return httpx.Response(200, html=NO_OG_HTML)

    result = fetch_originals(
        ["https://docs.claude.com/en/docs/x"], ALLOWED, transport=transport(handler)
    )
    assert result[0].status == "ok"
    assert "Claude Code settings" in result[0].text


# --------------------------------------------------------------------------
# Отказы сети не роняют прогон
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_http_error_degrades_quietly(status: int):
    result = fetch_originals(
        ["https://x.com/a/status/1"], ALLOWED, transport=transport(always("", status))
    )
    assert result[0].status == "unavailable"
    assert result[0].text == ""


def test_network_error_degrades_quietly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("таймаут", request=request)

    result = fetch_originals(
        ["https://x.com/a/status/1"], ALLOWED, transport=transport(handler)
    )
    assert result[0].status == "unavailable"


# --------------------------------------------------------------------------
# Извлечение текста
# --------------------------------------------------------------------------


def test_og_description_wins_over_title():
    assert extract_text(POST_HTML) == "1/ I run 5 Claudes in parallel in my terminal."


def test_title_is_the_fallback():
    assert extract_text(NO_OG_HTML) == "Claude Code settings - Claude Code Docs"


def test_html_entities_are_unescaped():
    html = '<meta property="og:description" content="Use &quot;plan&quot; &amp; &#x27;auto&#x27;">'
    assert extract_text(html) == "Use \"plan\" & 'auto'"


def test_page_without_text_is_marked():
    result = fetch_originals(
        ["https://x.com/a/status/1"],
        ALLOWED,
        transport=transport(always("<html><body>только тело</body></html>")),
    )
    assert result[0].status == "no_text"


def test_text_is_truncated():
    long = "я" * 5000
    html = f'<meta property="og:description" content="{long}">'
    result = fetch_originals(
        ["https://x.com/a/status/1"], ALLOWED,
        transport=transport(always(html)), max_chars=500,
    )
    assert len(result[0].text) <= 500


# --------------------------------------------------------------------------
# Потолки
# --------------------------------------------------------------------------


def test_url_cap_is_respected():
    urls = [f"https://x.com/a/status/{i}" for i in range(20)]
    result = fetch_originals(urls, ALLOWED, transport=transport(always(POST_HTML)), max_urls=3)
    assert sum(1 for r in result if r.status == "ok") == 3
    assert sum(1 for r in result if r.status == "skipped_cap") == 17


def test_duplicates_are_fetched_once():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, html=POST_HTML)

    url = "https://x.com/a/status/1"
    result = fetch_originals([url, url, url], ALLOWED, transport=transport(handler))
    assert len(calls) == 1
    assert len(result) == 1


def test_empty_input_is_fine():
    assert fetch_originals([], ALLOWED, transport=transport(always(POST_HTML))) == []


def test_result_is_a_frozen_record():
    result = fetch_originals(
        ["https://x.com/a/status/1"], ALLOWED, transport=transport(always(POST_HTML))
    )
    assert isinstance(result[0], Original)
    with pytest.raises(Exception):
        result[0].text = "подмена"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Кто написал первоисточник
# --------------------------------------------------------------------------

PROFILE_HTML = """<!doctype html><html><head>
<title>Thariq (@trq212) / X</title>
<meta property="og:title" content="Thariq (@trq212) on X">
<meta property="og:description" content="Claude Code @anthropicai. prev YC W20, @southpkcommons">
</head><body></body></html>"""


def test_author_is_fetched_by_handle():
    """Читатель не обязан знать хендлы. Имя и род занятий — из профиля."""
    author = fetch_author(
        "@trq212", ["x.com"], transport=transport(always(PROFILE_HTML))
    )
    assert author is not None
    assert author.name == "Thariq"
    assert author.handle == "@trq212"
    assert "Claude Code" in author.bio


def test_author_handle_is_taken_from_a_post_url():
    assert author_handle("https://x.com/bcherny/status/12345") == "@bcherny"
    assert author_handle("https://x.com/bcherny") is None
    assert author_handle("https://code.claude.com/docs/hooks") is None


def test_author_off_whitelist_is_not_requested():
    """Профиль — такой же внешний адрес, и белый список для него тот же."""
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, html=PROFILE_HTML)

    assert fetch_author("@someone", ["docs.claude.com"], transport=transport(handler)) is None
    assert called == []


def test_author_is_none_when_profile_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert fetch_author("@trq212", ["x.com"], transport=transport(handler)) is None


def test_author_without_name_in_title_keeps_the_handle():
    """Заголовок не разобрался — остаётся хендл, выдумывать имя нельзя."""
    html = '<html><head><title>X</title><meta property="og:description" content="био"></head></html>'
    author = fetch_author("@ghost", ["x.com"], transport=transport(always(html)))
    assert author is not None and author.name == "" and author.bio == "био"


# --------------------------------------------------------------------------
# Находки независимого ревью границы доверия
# --------------------------------------------------------------------------


def test_a_malformed_link_is_refused_not_fatal():
    """Одна битая ссылка на сайте роняла разбор всего события.

    `urlparse('https://[::1').hostname` бросает ValueError, а он не
    httpx.HTTPError — значит не ловится и уносит прогон до вызова модели.
    Ссылка обязана стать честным отказом, как любой чужой домен.
    """
    results = fetch_originals(["https://[::1", "https://x.com/a/status/1"], ["x.com"],
                              transport=httpx.MockTransport(
                                  lambda r: httpx.Response(200, text="<html></html>")))
    assert len(results) == 2
    assert results[0].status == STATUS_REFUSED
    assert results[0].url == "https://[::1"


def test_a_non_http_scheme_is_refused_before_the_request():
    """Хост разрешён, схема — нет. До транспорта это доходить не должно."""
    assert not domain_allowed("file://x.com/etc/passwd", ["x.com"])
    assert not domain_allowed("ftp://x.com/resource", ["x.com"])
    assert domain_allowed("https://x.com/a", ["x.com"])
    assert domain_allowed("http://x.com/a", ["x.com"])


def test_a_lookalike_host_does_not_yield_an_x_handle():
    """`endswith('x.com')` принимал notx.com за X.

    Ссылка с запрещённого домена давала хендл, по которому код затем
    открывал НАСТОЯЩИЙ профиль X — и приписывал разбору чужого автора.
    """
    assert author_handle("https://notx.com/anthropic/status/123") is None
    assert author_handle("https://evil-x.com/bcherny/status/1") is None
    assert author_handle("https://x.com.evil.ru/bcherny/status/1") is None
    assert author_handle("https://x.com/bcherny/status/1") == "@bcherny"
    assert author_handle("https://mobile.x.com/bcherny/status/1") == "@bcherny"


def test_a_huge_body_is_cut_before_it_is_read_into_memory():
    """max_source_chars резал текст ПОСЛЕ загрузки — то есть не резал трафик."""
    huge = "<html><body>" + ("а" * 5_000_000) + "</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=huge)

    results = fetch_originals(
        ["https://x.com/a/status/1"], ["x.com"],
        max_bytes=64_000, transport=httpx.MockTransport(handler),
    )
    assert results[0].status in (STATUS_OK, STATUS_NO_TEXT)
    assert len(results[0].text) <= 1200


def test_the_refused_redirect_target_reaches_the_analysis():
    """Ссылку, которую не открыли, читатель обязан увидеть как факт.

    Иначе белый список делает неизвестный адрес невидимым — ровно то, чего
    вся эта конструкция избегает.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if "x.com" in str(request.url):
            return httpx.Response(302, headers={"location": "https://evil.ru/payload"})
        raise AssertionError(f"запрос на запрещённый домен: {request.url}")

    results = fetch_originals(
        ["https://x.com/a/status/1"], ["x.com"], transport=httpx.MockTransport(handler)
    )
    assert results[0].status == STATUS_REDIRECTED
    assert results[0].redirect_to == "https://evil.ru/payload"

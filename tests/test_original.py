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

from watcher.original import Original, extract_text, fetch_originals

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

"""Загрузка первоисточников — слой 1 достоверности.

Сайт пересказывает чужие посты. Чтобы разбор мог сказать «автор оригинала
написал вот это», нужен текст того самого поста, а не пересказ. Раньше его
доставал server-side инструмент провайдера, и белый список доменов применял
API. Теперь загрузку делает этот модуль, а белый список применяет код —
тот, который покрыт тестами.

ЭТО ГРАНИЦА ДОВЕРИЯ, и она проходит здесь по трём линиям:

  1. домен проверяется ДО запроса — на чужой сервер не уходит даже стук;
  2. домен проверяется ЗАНОВО на каждом редиректе, иначе белый список
     обходится одним 302 на разрешённом хосте;
  3. из ответа берётся не тело страницы, а короткое описание — то, что
     сайт сам объявил о себе в og:description. Пятьсот килобайт чужого
     JS-шелла в промт модели не попадают физически.

Отказ здесь никогда не роняет прогон. Не удалось загрузить — слой 1
честно превращается в «не открывал», и это записано в разборе как факт.
"""

from __future__ import annotations

import html as html_module
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

# Больше трёх переходов подряд — это не канонизация адреса, а цепочка,
# в которой уже нельзя понять, куда мы идём.
MAX_REDIRECTS = 3

STATUS_OK = "ok"
STATUS_REFUSED = "refused_domain"
STATUS_REDIRECTED = "redirected_off_whitelist"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NO_TEXT = "no_text"
STATUS_SKIPPED = "skipped_cap"

__all__ = [
    "Author",
    "Original",
    "author_handle",
    "domain_allowed",
    "extract_text",
    "fetch_author",
    "fetch_originals",
    "STATUS_OK",
    "STATUS_REFUSED",
    "STATUS_REDIRECTED",
    "STATUS_UNAVAILABLE",
    "STATUS_NO_TEXT",
    "STATUS_SKIPPED",
]


@dataclass(frozen=True)
class Original:
    """Результат попытки открыть первоисточник.

    Frozen намеренно: объект уходит в сборку промта, и его содержимое не
    должно меняться после того, как граница доверия уже принята.
    """

    url: str
    host: str
    status: str
    text: str = ""


@dataclass(frozen=True)
class Author:
    """Кто написал первоисточник.

    Хендл читателю ничего не говорит: «@trq212 написал» и «разработчик
    Claude Code из Anthropic написал» — это разный вес одного и того же
    утверждения. Имя и род занятий берутся со страницы профиля, то есть
    остаются фактом, а не догадкой модели о том, кто есть кто.
    """

    handle: str
    name: str = ""
    bio: str = ""

    @property
    def credited(self) -> str:
        """Как называть автора в тексте: «Имя (@хендл)» либо просто хендл."""
        return f"{self.name} ({self.handle})" if self.name else self.handle


def author_handle(url: str) -> str | None:
    """Хендл автора поста — из адреса поста, а не из его текста.

    Профиль сам по себе (`x.com/bcherny`) сюда не годится: адрес автора
    нужен нам как свойство ПОСТА, и брать его следует только оттуда, где
    он однозначен — из ссылки вида `/handle/status/…`.
    """
    parsed = urlparse(url)
    if not (parsed.hostname or "").removeprefix("www.").endswith("x.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[1] == "status":
        return f"@{parts[0]}"
    return None


def fetch_author(
    handle: str,
    allowed: list[str],
    *,
    timeout_seconds: float = 15.0,
    user_agent: str = "hbucc-watcher/1.0",
    transport: httpx.BaseTransport | None = None,
) -> "Author | None":
    """Открыть профиль и достать имя с описанием. None — не вышло.

    Профиль — такой же внешний адрес, поэтому идёт через ту же границу
    доверия: домен проверяется до запроса и на каждом редиректе. Не
    открылось или заголовок не разобрался — возвращаем то, что достоверно,
    вплоть до None. Придумывать, кто этот человек, нельзя: неверно
    приписанная должность хуже, чем голый хендл.
    """
    url = f"https://x.com/{handle.lstrip('@')}"
    if not domain_allowed(url, allowed):
        return None

    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        headers={"User-Agent": user_agent, "Accept": "text/html,*/*"},
        transport=transport,
    ) as client:
        try:
            status, body = _get(client, url, allowed)
        except httpx.HTTPError as exc:
            log.info("профиль %s не открылся: %s", handle, exc)
            return None

    if status != STATUS_OK:
        return None

    tree = HTMLParser(body)
    title_node = tree.css_first("title")
    raw_title = html_module.unescape(title_node.text(strip=True)) if title_node else ""
    # «Thariq (@trq212) / X» -> «Thariq». Не разобралось — имени нет.
    name = raw_title.split("(")[0].strip() if "(" in raw_title else ""
    return Author(handle=handle, name=name, bio=extract_text(body))


def domain_allowed(url: str, allowed: list[str]) -> bool:
    """Разрешён ли домен ссылки.

    Сравниваем хост целиком или как поддомен: 'x.com' разрешает
    'x.com' и 'mobile.x.com', но НЕ 'evil-x.com' и не 'x.com.evil.ru'.
    Наивная проверка через `in` пропустила бы оба.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in (x.lower() for x in allowed))


def extract_text(html: str) -> str:
    """Достать краткое описание страницы.

    Порядок неслучаен. `og:description` — то, что страница объявила о себе
    сама; для поста в X это полный текст твита, ради которого мы и пришли.
    Заголовок — запасной вариант для страниц документации. Тело не берём
    вообще: у X это JS-заглушка на полмегабайта без единого слова поста.
    """
    tree = HTMLParser(html)
    for selector in (
        'meta[property="og:description"]',
        'meta[name="og:description"]',
        'meta[name="twitter:description"]',
        'meta[name="description"]',
    ):
        node = tree.css_first(selector)
        if node is not None:
            content = (node.attributes.get("content") or "").strip()
            if content:
                return html_module.unescape(content)

    title = tree.css_first("title")
    if title is not None:
        text = title.text(strip=True)
        if text:
            return html_module.unescape(text)
    return ""


def _get(
    client: httpx.Client, url: str, allowed: list[str]
) -> tuple[str, str]:
    """Пройти по редиректам вручную, проверяя домен на каждом шаге.

    httpx умеет follow_redirects сам, но тогда проверка домена случилась бы
    только для первого адреса — а доверие наследовать нельзя.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        response = client.get(current)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if not location:
                return STATUS_UNAVAILABLE, ""
            current = urljoin(current, location)
            if not domain_allowed(current, allowed):
                log.info("редирект увёл за белый список: %s", urlparse(current).hostname)
                return STATUS_REDIRECTED, ""
            continue
        if response.status_code != 200:
            return STATUS_UNAVAILABLE, ""
        return STATUS_OK, response.text
    return STATUS_UNAVAILABLE, ""


def fetch_originals(
    urls: list[str],
    allowed: list[str],
    *,
    timeout_seconds: float = 15.0,
    max_urls: int = 6,
    max_chars: int = 1200,
    user_agent: str = "hbucc-watcher/1.0",
    transport: httpx.BaseTransport | None = None,
) -> list[Original]:
    """Открыть первоисточники, соблюдая белый список.

    Возвращает запись на КАЖДУЮ ссылку, включая отвергнутые: белый список
    не должен делать неизвестную ссылку невидимой. Читатель обязан узнать,
    что материал на что-то ссылается, даже если мы это не открывали.

    `transport` существует ради тестов: граница доверия должна проверяться
    без выхода в сеть, иначе её проверка зависит от доступности x.com.
    """
    unique: list[str] = list(dict.fromkeys(u for u in urls if u))
    if not unique:
        return []

    results: list[Original] = []
    attempted = 0

    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        headers={"User-Agent": user_agent, "Accept": "text/html,*/*"},
        transport=transport,
    ) as client:
        for url in unique:
            host = (urlparse(url).hostname or "") if "//" in url else ""
            if not domain_allowed(url, allowed):
                results.append(Original(url=url, host=host, status=STATUS_REFUSED))
                continue
            if attempted >= max_urls:
                results.append(Original(url=url, host=host, status=STATUS_SKIPPED))
                continue

            attempted += 1
            try:
                status, body = _get(client, url, allowed)
            except httpx.HTTPError as exc:
                log.info("первоисточник недоступен (%s): %s", host, exc)
                results.append(Original(url=url, host=host, status=STATUS_UNAVAILABLE))
                continue

            if status != STATUS_OK:
                results.append(Original(url=url, host=host, status=status))
                continue

            text = extract_text(body)[:max_chars]
            results.append(
                Original(
                    url=url,
                    host=host,
                    status=STATUS_OK if text else STATUS_NO_TEXT,
                    text=text,
                )
            )

    return results

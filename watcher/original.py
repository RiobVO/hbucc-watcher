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

# Потолок на тело чужой страницы. Нам от неё нужен один мета-тег, а
# страница X — полмегабайта JS-бандла; двух мегабайт хватает с запасом на
# любую документацию. Читается потоком: без потолка многогигабайтный или
# бесконечный ответ с разрешённого домена съел бы память раннера раньше,
# чем дошло бы до обрезки текста.
PROFILE_MAX_BYTES = 2_000_000

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
    # Куда увёл редирект, если увёл за белый список. Адрес не запрашивался,
    # но обязан попасть в разбор как факт: белый список не должен делать
    # неизвестную ссылку невидимой — ни исходную, ни ту, на которую её
    # молча перенаправили.
    redirect_to: str = ""


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

    Хост проверяется так же строго, как в белом списке, и по той же
    причине: `endswith("x.com")` принимал за X и `notx.com`, и
    `evil-x.com`. Хендл, вынутый из чужой ссылки, вёл к загрузке
    НАСТОЯЩЕГО профиля X — и разбор приписывал первоисточник постороннему
    человеку.
    """
    host = host_of(url).removeprefix("www.")
    if not (host == "x.com" or host.endswith(".x.com")):
        return None
    parts = [p for p in urlparse(url).path.split("/") if p]
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
            # Профиль — такая же чужая страница: потолок на тело тот же.
            status, body, _ = _get(client, url, allowed, PROFILE_MAX_BYTES)
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


def host_of(url: str) -> str:
    """Хост ссылки или пустая строка. Никогда не бросает.

    Разбор чужого адреса — тоже недоверенная операция: `urlparse` на
    строке вида `https://[::1` бросает ValueError. Один такой адрес на
    сайте ронял разбор всего события до вызова модели, потому что ValueError
    не httpx.HTTPError и не ловился нигде по пути.
    """
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        log.info("адрес не разбирается, считаю запрещённым: %.80s", url)
        return ""


def domain_allowed(url: str, allowed: list[str]) -> bool:
    """Разрешена ли ссылка: сначала схема, потом домен.

    Схема проверяется первой. Хост у `file://x.com/etc/passwd` разрешённый,
    и без этой проверки такой адрес доходил до транспорта и тратил слот из
    лимита загрузок — то есть граница полагалась на то, что httpx откажет
    сам.

    Домен сравниваем целиком или как поддомен: 'x.com' разрешает
    'x.com' и 'mobile.x.com', но НЕ 'evil-x.com' и не 'x.com.evil.ru'.
    Наивная проверка через `in` пропустила бы оба.
    """
    try:
        if urlparse(url).scheme.lower() not in ("http", "https"):
            return False
    except ValueError:
        return False
    host = host_of(url)
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
    client: httpx.Client, url: str, allowed: list[str], max_bytes: int
) -> tuple[str, str, str]:
    """Пройти по редиректам вручную, проверяя домен на каждом шаге.

    httpx умеет follow_redirects сам, но тогда проверка домена случилась бы
    только для первого адреса — а доверие наследовать нельзя.

    Возвращает статус, текст и адрес отвергнутого редиректа. Третье
    значение существует затем, чтобы отказ не превращался в молчание:
    читатель должен узнать, куда его пытались увести.

    Тело читается потоком с потолком в байтах. Раньше ответ загружался
    целиком, а лимит применялся к уже полученной строке — то есть
    ограничивал промт, но не трафик и не память. Многогигабайтный ответ с
    разрешённого домена клал бы раннер до того, как лимит вообще
    применится.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        with client.stream("GET", current) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    return STATUS_UNAVAILABLE, "", ""
                current = urljoin(current, location)
                if not domain_allowed(current, allowed):
                    log.info("редирект увёл за белый список: %s", host_of(current))
                    return STATUS_REDIRECTED, "", current
                continue
            if response.status_code != 200:
                return STATUS_UNAVAILABLE, "", ""

            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    log.info("тело первоисточника обрезано на %d байтах: %s", size, current)
                    break
            body = b"".join(chunks)[:max_bytes]
            return STATUS_OK, body.decode(response.encoding or "utf-8", errors="replace"), ""
    return STATUS_UNAVAILABLE, "", ""


def fetch_originals(
    urls: list[str],
    allowed: list[str],
    *,
    timeout_seconds: float = 15.0,
    max_urls: int = 6,
    max_chars: int = 1200,
    max_bytes: int = 2_000_000,
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
            host = host_of(url)
            if not domain_allowed(url, allowed):
                results.append(Original(url=url, host=host, status=STATUS_REFUSED))
                continue
            if attempted >= max_urls:
                results.append(Original(url=url, host=host, status=STATUS_SKIPPED))
                continue

            attempted += 1
            try:
                status, body, blocked = _get(client, url, allowed, max_bytes)
            except httpx.HTTPError as exc:
                log.info("первоисточник недоступен (%s): %s", host, exc)
                results.append(Original(url=url, host=host, status=STATUS_UNAVAILABLE))
                continue

            if status != STATUS_OK:
                results.append(
                    Original(url=url, host=host, status=status, redirect_to=blocked)
                )
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

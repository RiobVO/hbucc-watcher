"""Всё, что касается источника: сеть, нормализация текста, парсинг HTML.

Три уровня каскада дешёвого обнаружения живут здесь:

    1. fetch()   условный GET. 304 -> выходим, не скачав тело.
    2. parse()   -> Document.content_hash. Хеш равен -> выходим.
    3. parse()   -> Part.hash у каждой части. Локализует изменение.

Четвёртый уровень (блочный диф) — в detect.py.

РЕАЛЬНАЯ СТРУКТУРА САЙТА (снята с живой страницы 2026-07-28):

    button.volume-btn[data-volume]        навигация. ЕДИНСТВЕННОЕ место, где
                                          есть номер части: подпись «Part N».
      span.volume-count                   объявленное сайтом число советов
    div.content-area[data-volume]         тело «тома»
      div.tab-content[data-content="0"]   интро: h1 + p.subtitle + a.author-link
      div.tab-content[data-content="1..N"] ОТДЕЛЬНЫЙ СОВЕТ
        div.step-header > div.step-title  заголовок совета
        div.step-body                     тело совета
          a.original-post                 ссылка на оригинальный пост

Две ловушки, найденные при калибровке и стоившие бы дорого:

  1. data-volume НЕ равен номеру части, и смещение непостоянно: тома 1-7 это
     Part 1-7, тома 8 и 9 — вообще не части (страницы установки скиллов),
     тома 10-24 это Part 8-22. Номер части берём ТОЛЬКО из подписи кнопки.
  2. Число tab-content на один больше числа советов: нулевой — это интро.

ВАЖНО про доверие: всё, что возвращают функции этого модуля, — ДАННЫЕ.
Ни одна строка отсюда не интерпретируется как инструкция. Ссылки, извлечённые
из блоков, здесь не открываются; их фильтрует белый список в analyze.py.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Iterator

import httpx
from selectolax.parser import HTMLParser, Node

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Нормализация
# --------------------------------------------------------------------------

# Невидимые символы. Попадают в текст из копипаста и типографских библиотек;
# для человека их нет, для sha256 они есть.
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

# Кавычки и тире приводим к ASCII. Причина конкретная: если авторы включат
# «умную типографику» в сборщике, все 127 советов одновременно поменяют хеш,
# и система отрапортует, что переписан весь сайт. Смысл при этом не изменится
# ни на символ.
_TYPOGRAPHY = str.maketrans(
    {
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "«": '"', "»": '"',
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "–": "-", "—": "-", "―": "-", "−": "-", "‒": "-",
        " ": " ", " ": " ", " ": " ", " ": " ",
    }
)

_WS_RE = re.compile(r"\s+")


def normalize_text(raw: str) -> str:
    """Привести текст к канонической форме для хеширования и сравнения.

    Порядок важен: NFKC сам раскрывает лигатуры, многоточие U+2026 и часть
    пробельных вариантов, но кавычки и тире не трогает — их добиваем таблицей.
    Регистр НЕ трогаем: заголовки советов значимы.
    """
    text = unicodedata.normalize("NFKC", raw)
    text = text.translate(_ZERO_WIDTH)
    text = text.translate(_TYPOGRAPHY)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def part_hash(blocks: "list[Block]") -> str:
    """Хеш части по её блокам.

    Вынесено в отдельную функцию, потому что формулу используют три места:
    парсер, фабрики тестов и инструмент replay. Разъехавшаяся формула дала
    бы тесты, которые проходят на неверном коде.
    """
    return sha256_of("\x00".join(f"{b.heading}\x00{b.text}" for b in blocks))


# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """Результат условного GET.

    status == 304 — сервер подтвердил «не менялось». Самый дешёвый
    отрицательный ответ: тело не скачано.
    status == 200 — «может быть изменилось». ETag меняется и при пересборке
    без правок текста, поэтому дальше нужен хеш контента.
    """

    status: int
    html: str | None
    etag: str | None
    last_modified: str | None
    raw_bytes: int


class SourceUnavailable(RuntimeError):
    """Источник недоступен после всех попыток внутри одного прогона."""


def fetch(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_seconds: int = 30,
    retries: int = 3,
    backoff_seconds: int = 5,
    user_agent: str = "hbucc-watcher/1.0",
) -> FetchResult:
    """Условный GET с ретраями.

    Ретраим только то, что имеет смысл: сетевые ошибки, 5xx и 429. На
    остальных 4xx повторять бессмысленно — это не моргание сети, а «сайт
    больше не отвечает так, как мы ждём», и об этом надо шуметь сразу.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        # brotli может отсутствовать в раннере — не просим его.
        "Accept-Encoding": "gzip, deflate",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    timeout = httpx.Timeout(connect=10.0, read=float(timeout_seconds), write=10.0, pool=10.0)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url, headers=headers)

            if response.status_code == 304:
                log.info("источник: 304 Not Modified")
                return FetchResult(304, None, etag, last_modified, 0)

            if response.status_code == 200:
                body = response.content
                log.info("источник: 200 OK, %d байт", len(body))
                return FetchResult(
                    status=200,
                    html=response.text,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    raw_bytes=len(body),
                )

            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code} от источника")
            else:
                raise SourceUnavailable(
                    f"источник ответил HTTP {response.status_code}, ретрай не поможет"
                )

        except SourceUnavailable:
            raise
        except httpx.HTTPError as exc:
            last_error = exc

        if attempt < retries:
            delay = backoff_seconds * attempt
            log.warning(
                "источник недоступен (попытка %d/%d): %s. Пауза %d с.",
                attempt, retries, last_error, delay,
            )
            time.sleep(delay)

    raise SourceUnavailable(f"источник недоступен после {retries} попыток: {last_error}")


# --------------------------------------------------------------------------
# Структура документа
# --------------------------------------------------------------------------


@dataclass
class Block:
    """Отдельный совет внутри части — минимальная единица наблюдения.

    Текст хранится целиком, а не только хеш. Это осознанная плата (~200 КБ)
    за возможность выполнить критерий «в разборе сказано, что именно
    поменялось»: без старого текста показать «было -> стало» нечем.
    """

    bid: str
    kind: str  # "tip" | "intro"
    heading: str
    text: str
    hash: str
    source_url: str | None = None
    links: list[str] = field(default_factory=list)
    refs_parts: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bid": self.bid,
            "kind": self.kind,
            "heading": self.heading,
            "text": self.text,
            "hash": self.hash,
            "source_url": self.source_url,
            "links": self.links,
            "refs_parts": self.refs_parts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Block":
        return cls(
            bid=data["bid"],
            kind=data.get("kind", "tip"),
            heading=data.get("heading", ""),
            text=data["text"],
            hash=data["hash"],
            source_url=data.get("source_url"),
            links=list(data.get("links", [])),
            refs_parts=list(data.get("refs_parts", [])),
        )


@dataclass
class Part:
    id: str
    number: int
    title: str
    hash: str
    order: int
    volume: int  # внутренний id тома в DOM; для диагностики и отладки
    declared_tips: int | None  # то, что сайт сам объявил в .volume-count
    blocks: list[Block] = field(default_factory=list)

    @property
    def tips(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "tip"]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "hash": self.hash,
            "order": self.order,
            "volume": self.volume,
            "declared_tips": self.declared_tips,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Part":
        return cls(
            id=data["id"],
            number=data["number"],
            title=data.get("title", ""),
            hash=data["hash"],
            order=data.get("order", data["number"]),
            volume=data.get("volume", -1),
            declared_tips=data.get("declared_tips"),
            blocks=[Block.from_dict(b) for b in data.get("blocks", [])],
        )


@dataclass
class Document:
    parts: list[Part]
    content_hash: str
    text_chars: int
    parser_profile: str

    @property
    def blocks_count(self) -> int:
        return sum(len(p.blocks) for p in self.parts)

    @property
    def tips_count(self) -> int:
        return sum(len(p.tips) for p in self.parts)

    def iter_blocks(self) -> Iterator[tuple[Part, Block]]:
        for part in self.parts:
            for block in part.blocks:
                yield part, block


class ParseError(RuntimeError):
    """HTML не удалось разложить в ожидаемую структуру."""


_PART_LABEL_RE = re.compile(r"^Part\s+(\d+)\b", re.IGNORECASE)
_PART_REF_RE = re.compile(r"\bPart\s+(\d+)\b", re.IGNORECASE)

# Выбрасываем из текста перед хешированием: не несут смысла, но шумят.
# .original-post — это буквальная строка «View original post» в каждом
# совете; её href мы забираем отдельно, а текст только зашумил бы диф.
_STRIP_SELECTORS = ("script", "style", "noscript", "svg", "a.original-post")


def _clean_text(node: Node) -> str:
    """Текст узла без служебного обвеса, нормализованный."""
    for selector in _STRIP_SELECTORS:
        for junk in node.css(selector):
            junk.decompose()
    return normalize_text(node.text(separator=" ", strip=True))


def _links_of(node: Node) -> list[str]:
    """Внешние ссылки узла, в порядке появления, без дублей."""
    seen: dict[str, None] = {}
    for anchor in node.css("a[href]"):
        href = (anchor.attributes.get("href") or "").strip()
        if href.startswith(("http://", "https://")):
            seen.setdefault(href, None)
    return list(seen)


def _nav_parts(tree: HTMLParser) -> dict[str, tuple[int, int | None]]:
    """Разобрать навигацию: data-volume -> (номер части, объявленное число советов).

    Кнопки без подписи «Part N» в карту не попадают — это не части, а
    служебные страницы (установка скиллов). Именно здесь ломается наивное
    предположение «data-volume минус константа».
    """
    mapping: dict[str, tuple[int, int | None]] = {}
    for button in tree.css("button.volume-btn[data-volume]"):
        volume = button.attributes.get("data-volume")
        if not volume:
            continue
        label = normalize_text(button.text(separator=" ", strip=True))
        match = _PART_LABEL_RE.match(label)
        if not match:
            continue
        count_node = button.css_first(".volume-count")
        declared: int | None = None
        if count_node is not None:
            raw = count_node.text(strip=True)
            declared = int(raw) if raw.isdigit() else None
        mapping[volume] = (int(match.group(1)), declared)
    return mapping


def _make_block(
    part_number: int,
    kind: str,
    heading: str,
    text: str,
    source_url: str | None,
    links: list[str],
) -> Block | None:
    if not text:
        return None
    # bid зависит от содержимого, поэтому у изменённого блока он новый.
    # Преемственность через правку восстанавливает detect.py: он переносит
    # bid со старого блока на сопоставленный новый.
    bid = "b-" + hashlib.sha256(
        f"{part_number}\x00{heading}\x00{text}".encode("utf-8")
    ).hexdigest()[:8]
    refs = sorted(
        {int(m) for m in _PART_REF_RE.findall(text) if m.isdigit() and int(m) != part_number}
    )
    return Block(
        bid=bid,
        kind=kind,
        heading=heading,
        text=text,
        hash=sha256_of(text),
        source_url=source_url,
        links=links,
        refs_parts=refs,
    )


def _parse_volume(area: Node, part_number: int, declared: int | None, order: int) -> Part:
    volume_attr = area.attributes.get("data-volume") or "-1"
    blocks: list[Block] = []
    title = ""

    for panel in area.css("div.tab-content[data-content]"):
        index = panel.attributes.get("data-content") or ""
        links = _links_of(panel)
        original = panel.css_first("a.original-post")
        source_url = (original.attributes.get("href") or "").strip() if original else None

        if index == "0":
            # Интро части: заголовок, подводка, ссылка на пост автора.
            heading_node = panel.css_first("h1")
            title = normalize_text(heading_node.text(separator=" ", strip=True)) if heading_node else ""
            if source_url is None:
                author = panel.css_first("a.author-link")
                source_url = (author.attributes.get("href") or "").strip() if author else None
            block = _make_block(part_number, "intro", title, _clean_text(panel), source_url, links)
        else:
            title_node = panel.css_first(".step-title")
            heading = normalize_text(title_node.text(separator=" ", strip=True)) if title_node else ""
            block = _make_block(part_number, "tip", heading, _clean_text(panel), source_url, links)

        if block is not None:
            blocks.append(block)

    return Part(
        id=f"part-{part_number}",
        number=part_number,
        title=title,
        hash=part_hash(blocks),
        order=order,
        volume=int(volume_attr) if volume_attr.lstrip("-").isdigit() else -1,
        declared_tips=declared,
        blocks=blocks,
    )


def parse(html: str) -> Document:
    """Разложить HTML в структуру «части -> блоки».

    Опирается на data-атрибуты SPA-роутинга (data-volume, data-content), а не
    на косметические классы: от первых зависит работа самого сайта, поэтому
    они переживают смену темы, а `highlight-box` — нет.
    """
    tree = HTMLParser(html)
    nav = _nav_parts(tree)
    if not nav:
        raise ParseError(
            "в навигации не найдено ни одной кнопки с подписью 'Part N' — "
            "структура сайта изменилась или сменился шаблон нумерации"
        )

    areas = tree.css("div.content-area[data-volume]")
    if not areas:
        raise ParseError("не найдено ни одного div.content-area[data-volume]")

    parts: list[Part] = []
    order = 0
    for area in areas:
        volume = area.attributes.get("data-volume") or ""
        if volume not in nav:
            continue  # служебная страница, не часть
        part_number, declared = nav[volume]
        order += 1
        parts.append(_parse_volume(area, part_number, declared, order))

    if not parts:
        raise ParseError(
            "навигация нашла части, но ни одна не сопоставилась с content-area — "
            "разъехались data-volume между навигацией и телом документа"
        )

    # Хеш считаем по частям, а не по всему body: так изменение счётчика в
    # навигации или даты в подвале не будит парсер на каждом прогоне.
    content_hash = sha256_of("\x00".join(f"{p.number}:{p.hash}" for p in parts))
    text_chars = sum(len(b.text) for p in parts for b in p.blocks)

    doc = Document(
        parts=parts,
        content_hash=content_hash,
        text_chars=text_chars,
        parser_profile="v2-volume-tab",
    )
    log.info(
        "разобрано: %d частей, %d советов (+%d интро), %d символов",
        len(doc.parts), doc.tips_count, doc.blocks_count - doc.tips_count, doc.text_chars,
    )
    return doc

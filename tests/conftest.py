"""Фабрики для тестов.

Собирают Block/Part/Document теми же функциями, что и парсер, чтобы тест
не мог случайно разойтись с продакшеном в способе вычисления хеша.
"""

from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.source import (  # noqa: E402
    Block,
    Document,
    Part,
    normalize_text,
    part_hash,
    sha256_of,
)


# Пустые элементы: закрывающего тега у них нет по стандарту, и стек
# проверки баланса не должен его ждать.
VOID = {"meta", "link", "br", "img", "input", "hr", "source"}


class TagBalance(HTMLParser):
    """Стек открытых тегов. Пустой в конце — разметка сошлась.

    Живёт здесь, а не в одном файле тестов: проверка нужна и странице
    разбора, и кускам для Telegram, а вторая копия однажды разойдётся с
    первой и позеленеет там, где должна краснеть.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        # Все встреченные теги, включая пустые: по ним видно, не появился
        # ли на странице элемент, которого в шаблоне нет, — то есть не
        # прорвался ли чужой текст из данных в разметку.
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
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


def balance(markup: str) -> TagBalance:
    parser = TagBalance()
    parser.feed(markup)
    return parser


def make_block(text: str, *, heading: str = "", kind: str = "tip", bid: str | None = None) -> Block:
    normalized = normalize_text(text)
    return Block(
        bid=bid or ("b-" + sha256_of(normalized)[7:15]),
        kind=kind,
        heading=heading,
        text=normalized,
        hash=sha256_of(normalized),
    )


def make_part(number: int, blocks: list[Block], *, title: str = "", order: int | None = None,
              declared: int | None = None) -> Part:
    tips = [b for b in blocks if b.kind == "tip"]
    return Part(
        id=f"part-{number}",
        number=number,
        title=title or f"Part {number} title",
        hash=part_hash(blocks),
        order=order if order is not None else number,
        volume=number,
        declared_tips=declared if declared is not None else len(tips),
        blocks=blocks,
    )


def make_document(parts: list[Part]) -> Document:
    return Document(
        parts=parts,
        content_hash=sha256_of("\x00".join(f"{p.number}:{p.hash}" for p in parts)),
        text_chars=sum(len(b.text) for p in parts for b in p.blocks),
        parser_profile="v2-volume-tab",
    )


@pytest.fixture
def simple_parts() -> list[Part]:
    """Две части по два совета — базовая сцена для дифа."""
    return [
        make_part(1, [
            make_block("Run five Claudes in parallel using worktrees.", heading="Parallel"),
            make_block("Start every complex task in plan mode first.", heading="Plan mode"),
        ]),
        make_part(2, [
            make_block("Configure your terminal for better output.", heading="Terminal"),
        ]),
    ]

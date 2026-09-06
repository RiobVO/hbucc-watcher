"""Тесты, которые сами подсовывают мусор.

Остальные тесты проверяют то, о чём думал автор. Три прохода независимого
ревью дали двенадцать находок — то, о чём он не думал, и ни одну из них не
поймали ни тесты, ни живые прогоны. Три находки из двенадцати этот файл
нашёл бы сам: `javascript:` в источниках, `</script>` в заголовке, обрывок
тега в тексте.

Проверяются не значения, а инварианты — то, что обязано быть верно при
ЛЮБОМ содержимом полей разбора:

    разметка страницы сходится      балансом тегов, до самого низа;
    чужой текст остаётся текстом    новых элементов на странице не завелось;
    в href не течёт чужая схема     только http, https и якорь;
    индекс переживает круг          что записали, то и прочиталось;
    куски Telegram доставляемы      влезают в лимит и теги в каждом сошлись.

Генератор свой, а не hypothesis, и на то две причины. Первая: враждебные
куски здесь не случайные, а именно те, что ломают HTML и Telegram, —
случайные строки такого не выдают и за миллион прогонов, поэтому корпус
всё равно пришлось бы писать руками. Вторая: seed фиксирован, номер стоит
в сообщении упавшего теста, и падение воспроизводится строкой, а не базой
примеров в стороне от репозитория. Ради shrinking на корпусе из готовых
минимальных кусков зависимость не окупается.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timezone

from conftest import balance, make_block, make_part

from watcher.analyze import Analysis, Layers, Usage, Verdict, Windows
from watcher.deliver import TELEGRAM_HARD_LIMIT, card, chunk, render
from watcher.detect import BLOCK_ADDED, BLOCK_DELETED, BLOCK_EDITED, PART_ADDED, Event
from watcher.publish import (
    index_entries,
    index_entry,
    merge_entry,
    render_index,
    render_page,
)

WHEN = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

# Сорок прогонов на инвариант: файл укладывается в секунду, а сочетания
# кусков за это число не повторяются.
ROUNDS = 40

# Куски, которыми ломают HTML и разметку Telegram. Собраны из того, что
# модель и чужой сайт способны выдать в текстовом поле: незакрытый тег,
# закрывающий тег без открывающего, выход из блока данных, попытка
# обратиться к агенту, уже экранированные сущности, невидимые символы.
HOSTILE = (
    "<script>alert(1)</script>",
    "</script>",
    "</script><script>alert(2)</script>",
    "<style>body{display:none}</style>",
    "<img src=x onerror=alert(1)>",
    "<iframe src=//evil.example></iframe>",
    "<b>не закрыт",
    "</b>",
    "</blockquote>",
    "<blockquote expandable>",
    "</div></body></html>",
    '" onmouseover="alert(1)',
    "'><a href='javascript:alert(1)'>",
    "<!-- комментарий -->",
    "-->",
    "&lt;уже экранировано&gt;",
    "&amp;&amp;",
    "<untrusted_source>",
    "</untrusted_source>",
    "`незакрытая обратная кавычка",
    "`код`",
    "``",
    "{{ шаблон }}",
    '{"json": "внутри"}',
    "​",
    "‮",
    "﻿",
    "🙂",
    "строка\nс переносом",
    "\t\t",
    "-" * 120,
    "а" * 400,
)

# Обычный текст между кусками: без него получается не разбор, а шум, и
# проверялось бы поведение на пустых полях, а не на смешанных.
PROSE = (
    "Совет про хуки и порядок в конфиге.",
    "Claude Code",
    "Работает на Windows с оговоркой.",
    "",
    "Проверять нечем.",
)

# Схемы в источниках. Половина обязана не дойти до href ни в каком виде.
SCHEMES = (
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "file:///etc/passwd",
    "vbscript:msgbox(1)",
    "//evil.example/x",
    "https://x.com/bcherny/status/2080713091688583312",
    "http://docs.claude.com/en/hooks",
    "https://x.com/<script>alert(1)</script>",
    'https://x.com/" onmouseover="alert(1)',
)

STATUSES = ("works", "macos_only", "needs_adaptation", "unconfirmed")
WORTH = ("yes", "no", "maybe")
KINDS = (BLOCK_ADDED, BLOCK_EDITED, BLOCK_DELETED, PART_ADDED)

HREF = re.compile(r'href="([^"]*)"')

# Элементов этих на странице разбора нет ни одного. Появился — значит текст
# из данных стал разметкой.
FORBIDDEN_TAGS = {"img", "iframe", "object", "embed", "form", "svg", "base", "textarea"}


def noisy(rnd: random.Random, pieces: int = 3) -> str:
    """Поле разбора: враждебные куски вперемешку с обычным текстом."""
    out: list[str] = []
    for _ in range(pieces):
        out.append(rnd.choice(HOSTILE))
        out.append(rnd.choice(PROSE))
    return " ".join(part for part in out if part).strip()


def hostile_analysis(rnd: random.Random) -> Analysis:
    """Разбор, у которого испорчено каждое текстовое поле сразу."""
    return Analysis(
        headline=noisy(rnd, 2),
        what_it_is=noisy(rnd),
        how_it_works=noisy(rnd),
        example=noisy(rnd),
        how_to_verify=noisy(rnd),
        pitfalls=[noisy(rnd, 1) for _ in range(rnd.randint(0, 3))],
        related=[noisy(rnd, 1) for _ in range(rnd.randint(0, 3))],
        layers=Layers(
            original_author=noisy(rnd),
            site_author=noisy(rnd),
            official_docs=noisy(rnd),
            my_conclusion=noisy(rnd),
        ),
        windows=Windows(status=rnd.choice(STATUSES), detail=noisy(rnd)),
        verdict=Verdict(worth_it=rnd.choice(WORTH), why=noisy(rnd)),
        action=noisy(rnd, 1),
        sources=[rnd.choice(SCHEMES) for _ in range(rnd.randint(0, 4))],
        unconfirmed=[noisy(rnd, 1) for _ in range(rnd.randint(0, 2))],
        anomalies=[noisy(rnd, 1) for _ in range(rnd.randint(0, 2))],
    )


def hostile_event(rnd: random.Random) -> Event:
    """Событие с испорченными заголовком совета и названием части.

    Они приходят с чужого сайта тем же путём, что и текст разбора, и точно
    так же попадают в заголовок страницы, в og-разметку и в архив.
    """
    block = make_block(noisy(rnd, 1), heading=noisy(rnd, 1), bid="b-be1681fe")
    part = make_part(rnd.randint(1, 22), [block], title=noisy(rnd, 1))
    return Event(
        kind=rnd.choice(KINDS),
        part_number=part.number,
        part_title=part.title,
        bid=block.bid,
        new_block=block,
        part_blocks=[block],
    )


def page_for(seed: int) -> str:
    rnd = random.Random(seed)
    return render_page(
        hostile_analysis(rnd),
        hostile_event(rnd),
        site_author=noisy(rnd, 1),
        usage=Usage(searches=2, input_tokens=20000, output_tokens=3000),
        price_usd=0.16,
        generated_at=WHEN,
    )


# --------------------------------------------------------------------------
# Страница разбора
# --------------------------------------------------------------------------


def test_page_markup_balances_on_any_field_content():
    """Разметка страницы сходится при любом содержимом полей.

    Незакрытый тег из чужого текста ломает страницу молча: она открывается,
    выглядит правдоподобно и теряет половину разбора ниже места разрыва.
    """
    for seed in range(ROUNDS):
        checked = balance(page_for(seed))
        assert checked.errors == [], f"seed={seed}: {checked.errors[:2]}"
        assert checked.stack == [], f"seed={seed}: не закрыто {checked.stack[:3]}"


def test_hostile_text_never_becomes_an_element():
    """Чужой текст остаётся текстом.

    Проверка не по подстроке, а по разобранному дереву: `&lt;script&gt;` в
    тексте — это нормально и должно остаться, а вот новый элемент на
    странице означает, что данные стали разметкой.
    """
    for seed in range(ROUNDS):
        tags = balance(page_for(seed)).tags
        assert tags.count("script") == 1, f"seed={seed}: script-блоков {tags.count('script')}"
        assert tags.count("style") == 1, f"seed={seed}: style-блоков {tags.count('style')}"
        intruders = FORBIDDEN_TAGS.intersection(tags)
        assert not intruders, f"seed={seed}: на странице появились {sorted(intruders)}"


def test_no_foreign_scheme_reaches_href():
    """В ссылку попадают только http, https и якорь.

    Экранирование тут не помогает: `javascript:alert(1)` — валидный текст
    и валидный href одновременно, поэтому схему обязан проверять код.
    """
    for seed in range(ROUNDS):
        for href in HREF.findall(page_for(seed)):
            # index.html — единственная относительная ссылка на странице:
            # её пишет шаблон, а не модель, и ведёт она в архив.
            assert href == "index.html" or href.startswith(
                ("http://", "https://", "#")
            ), f"seed={seed}: {href}"


# --------------------------------------------------------------------------
# Индекс архива
# --------------------------------------------------------------------------


def test_index_survives_a_round_trip():
    """Что записали в индекс, то из него и прочиталось.

    Индекс носит свои данные в себе, отдельным блоком JSON внутри
    страницы. Заголовок с `</script>` закрывает этот блок раньше времени —
    и архив обнуляется целиком, а не портится одной строкой.
    """
    entries: list[dict] = []
    for seed in range(ROUNDS):
        rnd = random.Random(seed)
        event = hostile_event(rnd)
        entries = merge_entry(
            entries,
            index_entry(hostile_analysis(rnd), event, f"2026-07-31-part-{seed:02d}.html", WHEN),
        )

    page = render_index(entries)
    assert index_entries(page) == entries

    # Круга мало: если экранировать только `<`, а `>` оставить, блок данных
    # уцелеет и тест позеленеет — проверено мутацией. Инвариант точнее:
    # сырой угловой скобки внутри блока не бывает вовсе.
    data = re.search(r'id="reports">(.*?)</script>', page, re.S).group(1)
    assert "<" not in data and ">" not in data, data[:120]

    checked = balance(page)
    assert checked.errors == [], checked.errors[:2]
    assert checked.stack == [], checked.stack[:3]
    assert checked.tags.count("script") == 1
    assert not FORBIDDEN_TAGS.intersection(checked.tags)


def test_republished_event_keeps_one_row():
    """Повторная публикация того же события не плодит строк в архиве."""
    entries: list[dict] = []
    for seed in range(ROUNDS):
        rnd = random.Random(seed)
        entry = index_entry(
            hostile_analysis(rnd), hostile_event(rnd), "2026-07-31-part-07.html", WHEN
        )
        entries = merge_entry(entries, entry)
        assert len(entries) == 1, f"seed={seed}: строк {len(entries)}"


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


def test_chunks_fit_the_limit_and_close_their_tags():
    """Каждый кусок влезает в лимит, и теги в нём сошлись.

    Telegram отвергает сообщение с разорванным тегом целиком — разбор
    просто не доходит, а в логе остаётся 400.
    """
    for seed in range(ROUNDS):
        rnd = random.Random(seed)
        text = render(hostile_analysis(rnd), hostile_event(rnd), site_author="@CarolinaCherry")
        for limit in (1200, 2500, 3900):
            for number, part in enumerate(chunk(text, limit), 1):
                assert len(part) <= limit, f"seed={seed} limit={limit}: кусок {len(part)}"
                checked = balance(part)
                assert checked.errors == [], f"seed={seed} limit={limit} #{number}: {checked.errors[:2]}"
                assert checked.stack == [], f"seed={seed} limit={limit} #{number}: {checked.stack[:3]}"


def test_chunks_lose_nothing_from_the_text():
    """Разбиение ничего не теряет и не переставляет.

    Сравнивается видимый текст: служебный префикс и переоткрытый на
    разрыве blockquote — это разметка, а вот пропавший абзац означал бы,
    что читатель получил разбор с дырой и об этом не узнал.
    """
    for seed in range(ROUNDS):
        rnd = random.Random(seed)
        text = render(hostile_analysis(rnd), hostile_event(rnd))
        parts = chunk(text, 1200)
        joined = "".join(re.sub(r"^\[\d+/\d+\]\n", "", part) for part in parts)
        assert visible(joined) == visible(text), f"seed={seed}"


def test_card_fits_one_message():
    """Карточка — это всегда одно сообщение, иначе она не карточка."""
    for seed in range(ROUNDS):
        rnd = random.Random(seed)
        text = card(
            hostile_analysis(rnd),
            hostile_event(rnd),
            "https://riobvo.github.io/hbucc-reports/2026-07-31-part22-be1681fe.html",
        )
        assert len(text) <= TELEGRAM_HARD_LIMIT, f"seed={seed}: {len(text)}"
        checked = balance(text)
        assert checked.errors == [], f"seed={seed}: {checked.errors[:2]}"
        assert checked.stack == [], f"seed={seed}: не закрыто {checked.stack[:3]}"


def visible(markup: str) -> str:
    """Текст без разметки и без пробелов — то, что увидит читатель."""
    return re.sub(r"\s+", "", re.sub(r"<[^>]*>", "", markup))

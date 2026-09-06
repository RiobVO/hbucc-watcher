"""Автопроверка разбора: слушалась ли модель промта.

Зачем это существует. Правка кода проверяется тестом за секунду. Правка
промта проверяется живым прогоном за $0.16 и полторы минуты — поэтому её
делают один раз и считают сделанной. Починка `pitfalls` подтверждена ровно
одним прогоном, то есть статистически это анекдот.

Здесь кодом проверяется то, что промт требует буквально. Каждое живое
событие становится бесплатными данными о качестве промта: замечания
пишутся в журнал доставок и видны в `git diff state/delivered.json`.

ПРОВЕРКА НИЧЕГО НЕ БЛОКИРУЕТ. Разбор уходит читателю в любом случае:
замечание к формулировке — не причина оставить человека без новости, а
ошибка проверки не имеет права стоить доставки. Отсюда и тон замечаний:
это наблюдения для владельца системы, а не вердикты.

Молчание тут ценнее полноты: шумящий сигнал перестают читать раньше, чем
он ловит первую настоящую ошибку. Поэтому проверок мало, и каждая стоит
на прямой цитате из prompts/system.md.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from watcher.analyze import Analysis
from watcher.deliver import strip_citations
from watcher.detect import similarity
from watcher.original import domain_allowed

log = logging.getLogger(__name__)

# Текстовые поля разбора: то, что читатель видит прозой.
_PROSE_FIELDS = (
    "headline",
    "what_it_is",
    "how_it_works",
    "example",
    "how_to_verify",
    "action",
)

# «Не вставляй ссылки и markdown-разметку внутрь текстовых полей. Адреса
# живут только в sources» — prompts/system.md.
_URL = re.compile(r"https?://", re.I)
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\([^)]+\)")

# «До 80 символов» — описание поля headline в схеме.
_HEADLINE_LIMIT = 80

# «pitfalls до четырёх пунктов, related до четырёх названий» — ориентиры
# по объёму из промта.
_LIST_LIMITS = {"pitfalls": 4, "related": 4}

# Один и тот же факт в pitfalls и unconfirmed — то, ради чего правился
# промт после живого разбора Part 22/6. Порог высокий: пересказ одной
# мысли разными словами дублем не считается, ловим повтор почти дословный.
_DUPLICATE_RATIO = 0.7

# Обороты, перечисленные в промте поимённо как ИИ-слоп. Слева — как это
# называет промт, справа — шаблон: русский текст склоняется, и «мощным
# инструментом» обязано ловиться тем же правилом, что «мощный инструмент».
# Список короткий намеренно: только то, что не бывает уместным в этом
# регистре, иначе проверка начнёт спорить с живой речью.
_SLOP = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        ("является", r"явля[её]тся"),
        ("представляет собой", r"представля\w*\s+собой"),
        ("осуществить", r"осуществ\w+"),
        ("играет важную роль", r"игра\w*\s+\w*важн\w*\s+роль"),
        ("неотъемлемая часть", r"неотъемлем\w+"),
        ("ряд преимуществ", r"ряд\w*\s+преимуществ"),
        ("в современном мире", r"в\s+современном\s+мире"),
        ("в наши дни", r"в\s+наши\s+дни"),
        ("сегодня как никогда", r"сегодня\s+как\s+никогда"),
        ("мощный инструмент", r"мощн\w*\s+инструмент\w*"),
        ("революционный", r"революционн\w+"),
        ("инновационный", r"инновационн\w+"),
        ("бесшовный", r"бесшовн\w+"),
        ("лучшие практики", r"лучш\w*\s+практик\w*"),
        ("раскрывает потенциал", r"раскрыва\w*\s+\w*\s*потенциал"),
        ("таким образом", r"таким\s+образом"),
        ("более того", r"более\s+того"),
        ("следует отметить", r"следует\s+отметить"),
        ("стоит подчеркнуть", r"стоит\s+подчеркнуть"),
        ("будущее за", r"будущее\s+за\s"),
    )
)

_NOT_CONFIRMED = "не удалось подтвердить"


def check(analysis: Analysis, *, allowed_domains: Iterable[str]) -> list[str]:
    """Замечания к разбору. Пустой список — разбор написан по промту."""
    notes: list[str] = []
    fields = {name: getattr(analysis, name) for name in _PROSE_FIELDS}
    fields.update(analysis.layers.model_dump())
    fields["verdict.why"] = analysis.verdict.why
    fields["windows.detail"] = analysis.windows.detail

    for name, raw in fields.items():
        # Смотрим текст ПОСЛЕ страховки. Модель врезает markdown-цитаты
        # вида ([домен](url)) вопреки промту — это известное поведение, и
        # strip_citations выкусывает их, перенося адреса в источники.
        # Первый живой разбор дал пять таких вставок разом: ругаться на
        # каждую значит утопить проверку в шуме на первом же событии.
        # Ссылка, пережившая страховку, — другое дело: она доедет до
        # читателя и промт её запрещает прямо.
        text, _ = strip_citations(raw)
        if _URL.search(text) or _MARKDOWN_LINK.search(text):
            notes.append(f"ссылка в поле {name} — адреса живут только в sources")
        for label, pattern in _SLOP:
            if pattern.search(text):
                notes.append(f"слоп «{label}» в поле {name}")

    if len(analysis.headline) > _HEADLINE_LIMIT:
        notes.append(
            f"заголовок {len(analysis.headline)} символов при лимите {_HEADLINE_LIMIT}"
        )

    for name, limit in _LIST_LIMITS.items():
        count = len(getattr(analysis, name))
        if count > limit:
            notes.append(f"{name}: {count} пунктов при ориентире {limit}")

    for pitfall in analysis.pitfalls:
        for claim in analysis.unconfirmed:
            if similarity(pitfall, claim) >= _DUPLICATE_RATIO:
                notes.append(f"дубль pitfalls и unconfirmed: «{pitfall[:60]}»")
                break

    for url in analysis.sources:
        if not domain_allowed(url, allowed_domains):
            notes.append(f"источник вне белого списка: {url}")

    # Промт: не нашёл в документации — скажи прямо И вынеси утверждение в
    # unconfirmed. Первая половина без второй означает, что расхождение
    # названо в прозе и потеряно для страницы расхождений.
    if _NOT_CONFIRMED in analysis.layers.official_docs.lower() and not analysis.unconfirmed:
        notes.append(
            "official_docs говорит «не удалось подтвердить», а unconfirmed пуст"
        )

    if notes:
        log.warning("автопроверка разбора: замечаний %d", len(notes))
        for note in notes:
            log.warning("  - %s", note)
    return notes

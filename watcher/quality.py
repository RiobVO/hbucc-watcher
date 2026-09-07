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

# «Все текстовые поля — по-русски» — prompts/system.md. Проверяется только
# ЗАГОЛОВОК, и это решение калибровки, а не лени.
#
# Первая версия смотрела все поля и утонула: под «английские слова» попали
# имена (Boris Cherny), хендлы, идентификаторы вроде bypassPermissions и
# названия частей сайта, которые по-английски и есть. На разборах terra
# выходило по шесть замечаний, а пример из самого промта переставал
# проходить проверку — то есть сигнал умирал ровно так, как описано выше.
#
# Заголовок — другое дело. Он короткий, он один уезжает в Telegram и в
# архив, у него в промте отдельное правило, и имени собственному там взяться
# почти неоткуда.
_CODE_SPAN_ANY = re.compile(r"`[^`]*`")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z-]{2,}")
_PRODUCT_NAMES = {
    "claude", "code", "anthropic", "windows", "macos", "linux", "powershell",
    "bash", "mcp", "git", "github", "python", "json", "cli", "api", "url",
    "raycast", "vim", "emacs", "opus", "sonnet", "haiku", "auto", "mode",
    "pull", "request", "part", "http", "https", "markdown",
}
# Три слова на поле: одно-два непереведённых термина в живой речи бывают
# («режим auto mode»), а три подряд — уже жаргон вместо объяснения.
_LATIN_LIMIT = 3


def check(analysis: Analysis, *, allowed_domains: Iterable[str]) -> list[str]:
    """Замечания к разбору. Пустой список — разбор написан по промту."""
    notes: list[str] = []
    fields = {name: getattr(analysis, name) for name in _PROSE_FIELDS}
    fields.update(analysis.layers.model_dump())
    fields["verdict.why"] = analysis.verdict.why
    fields["windows.detail"] = analysis.windows.detail
    # Пункты списков — тот же текст, что и проза, и правило для них то же.
    # Проверка их не смотрела, пока замер luna не показал три ловушки из
    # трёх со вставками внутри: цитату страховка снимет, а голый адрес
    # доедет до страницы незамеченным.
    # anomalies сюда не входят намеренно: это дословные цитаты чужого
    # текста, и промт требует приводить их как есть. Ссылка внутри такой
    # цитаты — выполненное требование, а не нарушение.
    for name in ("pitfalls", "related", "unconfirmed"):
        for index, item in enumerate(getattr(analysis, name)):
            fields[f"{name}[{index}]"] = item

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


    foreign = [
        word
        for word in _LATIN_WORD.findall(_CODE_SPAN_ANY.sub(" ", analysis.headline))
        if word.lower() not in _PRODUCT_NAMES
    ]
    if len(foreign) >= _LATIN_LIMIT:
        notes.append("заголовок не по-русски: " + ", ".join(sorted(set(foreign))))

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

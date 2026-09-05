"""Обнаружение: инварианты целостности и детерминированный диф.

Разделение обязанностей, на котором держится вся система:

    ЧТО ИЗМЕНИЛОСЬ решает этот модуль. Детерминированно, из кода, без сети
    и без модели. Результат — список событий с точными «было» и «стало».

    ПОЧЕМУ ЭТО ВАЖНО решает модель (analyze.py), получая готовое событие.

Модель никогда не отвечает на вопрос «что изменилось». Это убирает целый
класс галлюцинаций: нельзя выдумать изменение, которого нет в дифе.

Порядок проверок жёсткий:

    check_structure()   до дифа. Ловит перевёрстку, заглушку, обрезанный ответ.
    diff_documents()    собственно диф.
    check_diff_scale()  после дифа. Ловит перевёрстку, замаскированную под
                        сотню правок, — это самый коварный случай.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

from watcher.source import Block, Document, Part

log = logging.getLogger(__name__)


# Виды событий. Больше ничего система не различает — и этого достаточно
# для всех критериев обнаружения из задачи.
PART_ADDED = "part_added"
PART_REMOVED = "part_removed"
BLOCK_ADDED = "block_added"
BLOCK_EDITED = "block_edited"
BLOCK_DELETED = "block_deleted"


@dataclass(frozen=True)
class Violation:
    """Нарушенный инвариант. Любое = ALERT + состояние не двигаем."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass
class Event:
    """Одно обнаруженное изменение — единица работы для модели и доставки."""

    kind: str
    part_number: int
    part_title: str
    bid: str
    old_block: Block | None = None
    new_block: Block | None = None
    # Для part_added/part_removed: все советы части целиком.
    part_blocks: list[Block] = field(default_factory=list)
    # Ставится дешёвым регексп-предсканом до вызова модели.
    injection_suspected: bool = False
    # Похожесть старого и нового текста — та самая, по которой матчер решил,
    # что это правка, а не другой совет. Есть только у block_edited: у
    # добавления и удаления сравнивать не с чем, и там это None, а не ноль.
    # В event_id НЕ входит: id обязан считаться от содержимого, иначе смена
    # формулы похожести переоткрыла бы все уже доставленные события.
    ratio: float | None = None

    @property
    def event_id(self) -> str:
        """Детерминированный идентификатор события.

        Пересчёт дифа на следующем прогоне обязан дать тот же id — на этом
        держится фильтрация уже доставленного. Поэтому в основу берутся
        только стабильные величины: вид, часть, bid и хеши содержимого.
        """
        old_hash = self.old_block.hash if self.old_block else ""
        new_hash = self.new_block.hash if self.new_block else ""
        if self.kind in (PART_ADDED, PART_REMOVED):
            # У части нет bid; идентичность даёт её собственный состав.
            new_hash = sha_of_blocks(self.part_blocks)
        raw = f"{self.kind}\x00part-{self.part_number}\x00{self.bid}\x00{old_hash}\x00{new_hash}"
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def headline(self) -> str:
        block = self.new_block or self.old_block
        heading = (block.heading if block else "") or (block.text[:60] if block else "")
        labels = {
            PART_ADDED: "новая часть",
            PART_REMOVED: "часть удалена",
            BLOCK_ADDED: "новый совет",
            BLOCK_EDITED: "совет изменён",
            BLOCK_DELETED: "совет удалён",
        }
        label = labels.get(self.kind, self.kind)
        if self.kind in (PART_ADDED, PART_REMOVED):
            return f"Part {self.part_number} - {label}: {self.part_title}"
        return f"Part {self.part_number} - {label}: {heading}"


def sha_of_blocks(blocks: Iterable[Block]) -> str:
    joined = "\x00".join(b.hash for b in blocks)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _word_matcher(old_words: list[str], new_words: list[str]) -> SequenceMatcher:
    """Сравнение ПО СЛОВАМ и с autojunk=False.

    Единственное место, где живут оба этих решения, и берут его отсюда все
    три потребителя: похожесть, матчинг блоков и разбор вида правки.
    Посимвольное сравнение считает похожесть по общим буквам, что для
    текста бессмысленно, а autojunk на последовательностях длиннее 200
    элементов объявляет мусором всё частое — и правка 5% слов давала ratio
    0.36 вместо 0.95.
    """
    return SequenceMatcher(None, old_words, new_words, autojunk=False)


def similarity(old_text: str, new_text: str) -> float:
    """Похожесть двух текстов — ЕДИНСТВЕННОЕ определение в системе.

    Вынесено отдельно, чтобы инструмент калибровки (tools/replay.py) мерил
    ровно ту же величину, по которой принимает решение матчер. Разошедшиеся
    формулы означали бы калибровку порога по числу, которое в проде не
    используется, — и именно так был бы не замечен баг с autojunk.
    """
    return _word_matcher(old_text.split(), new_text.split()).ratio()


# Пунктуация, которая в прозе прилипает к слову и словом быть ему не мешает.
# Всё остальное — кавычки, дефис, слэш, знак равенства, цифры, точка ВНУТРИ
# токена — признак того, что это не слово, а команда, путь, версия или пара
# «ключ=значение».
_EDGE_PUNCTUATION = ".,;:!?"


def _is_plain_word(token: str) -> bool:
    """Токен — обычное слово прозы, а не идентификатор и не команда.

    Проверок три, и каждая закрывает свой способ уместить в один токен
    несколько значимых величин:

      латиница — письменность без пробельного деления кладёт в токен целую
      фразу, а проза сайта английская, так что дешевле требовать латиницу,
      чем гадать, сколько слов внутри;

      только буквы — цифра, флаг, слэш, точка внутри и знак равенства
      означают команду, путь, версию или пару «ключ=значение»;

      никаких заглавных дальше первой буквы — `allowSafeMode`,
      `PostCompact`, `PreToolUse` состоят из одних букв, но меняют по две
      величины за одну замену. Слово прозы бывает строчным или с заглавной
      первой буквой, и оба случая остаются словами.
    """
    core = token.strip(_EDGE_PUNCTUATION)
    if not core.isascii() or not core.isalpha():
        return False
    return not any(char.isupper() for char in core[1:])


def is_single_word_replacement(old_text: str, new_text: str) -> bool:
    """Правка — замена ровно одного слова, без вставок и удалений.

    Похожесть отвечает на вопрос «насколько текст изменился», но не на
    вопрос «как именно». Замер по всем 127 советам сайта показал, что для
    вставки этой разницы хватает, чтобы всё сломать: замена слова теряет
    старое и добавляет новое (два несовпадения), а вставка только добавляет
    (одно). Поэтому вставленное «not» даёт ratio от 0.989 и на 126 советах
    из 127 прошло бы молча при любом разумном пороге.

    Отсюда второе условие мелочи, от длины совета не зависящее: одна
    операция замены, ровно одно слово в ней. Вставка, удаление и правка
    нескольких слов доходят до разбора всегда.

    Слово при этом обязано быть словом. Разделение идёт по пробелам, а один
    пробельный токен умеет нести несколько значимых величин сразу:
    `allow=true,mode=safe` -> `allow=false,mode=unsafe` формально замена
    одного слова, а по сути две правки настройки. Так же выглядят флаг,
    путь, версия и адрес — поэтому токен с чем-либо кроме букв и краевой
    пунктуации мелочью не бывает.
    """
    old_words, new_words = old_text.split(), new_text.split()
    changes = [
        op for op in _word_matcher(old_words, new_words).get_opcodes() if op[0] != "equal"
    ]
    if len(changes) != 1:
        return False
    tag, old_start, old_end, new_start, new_end = changes[0]
    if tag != "replace" or old_end - old_start != 1 or new_end - new_start != 1:
        return False
    return _is_plain_word(old_words[old_start]) and _is_plain_word(new_words[new_start])


def is_minor_change(old_text: str, new_text: str, minor_ratio: float) -> bool:
    """Оба условия мелочи разом — и для прода, и для калибровки.

    Вынесено ровно по той же причине, что и similarity: инструмент
    (tools/replay.py --minor) обязан спрашивать то же самое, что спрашивает
    прогон, а не повторять условие своими руками.
    """
    return similarity(old_text, new_text) >= minor_ratio and is_single_word_replacement(
        old_text, new_text
    )


# --------------------------------------------------------------------------
# Инварианты до дифа
# --------------------------------------------------------------------------


def check_structure(
    doc: Document,
    *,
    previous_shape: dict[str, Any] | None,
    min_parts_absolute: int,
    max_text_shrink_pct: int,
) -> list[Violation]:
    """Проверить, что документ вообще похож на то, что мы ждём.

    Первый инвариант — самый сильный, и он появился только после калибровки
    на живом сайте: сайт САМ публикует в навигации число советов в каждой
    части (`.volume-count`). Сверка с ним превращает поломку парсера из
    расплывчатого «текст усох на 40%» в точное «в части 14 распарсили 3
    совета, сайт объявил 6». Эвристические пороги ниже остаются как
    страховка на случай, если исчезнет и сам счётчик.
    """
    violations: list[Violation] = []

    for part in doc.parts:
        if part.declared_tips is None:
            continue
        got = len(part.tips)
        if got != part.declared_tips:
            violations.append(
                Violation(
                    "declared_mismatch",
                    f"Part {part.number}: распарсили {got} советов, "
                    f"сайт объявил {part.declared_tips}",
                )
            )

    known_parts = (previous_shape or {}).get("parts_count")
    floor = known_parts if known_parts else min_parts_absolute
    if len(doc.parts) < floor:
        violations.append(
            Violation(
                "parts_shrunk",
                f"частей стало {len(doc.parts)}, ожидалось не меньше {floor}",
            )
        )

    previous_chars = (previous_shape or {}).get("text_chars")
    if previous_chars:
        shrink_pct = 100 * (previous_chars - doc.text_chars) / previous_chars
        if shrink_pct > max_text_shrink_pct:
            violations.append(
                Violation(
                    "text_shrunk",
                    f"объём текста упал на {shrink_pct:.0f}% "
                    f"({previous_chars} -> {doc.text_chars} символов)",
                )
            )

    return violations


def check_diff_scale(
    events: list[Event], previous_blocks_count: int, max_changed_pct: int
) -> list[Violation]:
    """Проверить масштаб дифа.

    Самый важный инвариант из всех. Без него сценарий «сайт перевёрстан»
    выглядит как «пришло сорок разборов подряд» — и это худший возможный
    исход: одновременно шум, потраченные деньги и потерянное состояние.
    Сто правок за раз на сайте с темпом «одна часть в 8-9 дней» не бывают.
    """
    if not previous_blocks_count:
        return []
    changed = sum(1 for e in events if e.kind != PART_ADDED)
    changed += sum(len(e.part_blocks) for e in events if e.kind == PART_ADDED)
    pct = 100 * changed / previous_blocks_count
    if pct > max_changed_pct:
        return [
            Violation(
                "mass_change",
                f"изменилось {changed} из {previous_blocks_count} блоков ({pct:.0f}%) — "
                f"это перевёрстка, а не {changed} правок",
            )
        ]
    return []


def check_assumptions(doc: Document, assumptions: dict[str, Any]) -> list[str]:
    """Проверить допущения, которые названы невечными.

    Возвращает не Violation, а предупреждения: нарушенное допущение — не
    поломка, а сообщение «то, на чём построена система, изменилось».
    Прогон при этом продолжается.
    """
    notes: list[str] = []

    if assumptions.get("new_parts_appended_at_end", True):
        numbers = [p.number for p in doc.parts]
        if numbers != sorted(numbers):
            notes.append(
                "части в документе идут не по возрастанию номеров — допущение "
                "«новое дописывается в конец» перестало выполняться"
            )

    return notes


# --------------------------------------------------------------------------
# Диф
# --------------------------------------------------------------------------


def _match_blocks(
    old_blocks: list[Block], new_blocks: list[Block], threshold: float
) -> tuple[
    list[tuple[Block, Block]], list[tuple[Block, Block, float]], list[Block], list[Block]
]:
    """Сопоставить блоки старой и новой версии части.

    Возвращает (unchanged, edited, added, deleted); у каждой правки —
    похожесть, по которой она правкой и признана.

    Три шага по убыванию надёжности:
      1. Точное совпадение хеша — блок не менялся.
      2. Жадное сопоставление остатков по SequenceMatcher.ratio() выше
         порога, от самых похожих пар к менее похожим — это правка.
      3. Что не сопоставилось: старое = удалено, новое = добавлено.

    Позиционный индекс не используется намеренно: вставка совета в середину
    части сдвинула бы все последующие и дала бы ложное «изменились все».

    Два неочевидных решения в сравнении, каждое найдено калибровкой на
    реальных текстах через tools/replay.py:

      сравниваем СЛОВА, а не символы — посимвольное сравнение считает
      похожесть по общим буквам, что для текста бессмысленно;

      autojunk=False — обязательно. По умолчанию SequenceMatcher на
      последовательностях длиннее 200 элементов объявляет «мусором» всё,
      что встречается чаще чем в 1% случаев. На тексте совета это пробелы
      и частые слова, то есть почти всё. С включённым autojunk правка 5%
      слов давала ratio 0.36 вместо 0.95, и КАЖДАЯ реальная правка
      классифицировалась бы как удаление плюс добавление.
    """
    unchanged: list[tuple[Block, Block]] = []
    remaining_old = list(old_blocks)
    remaining_new: list[Block] = []

    by_hash: dict[str, list[Block]] = {}
    for block in remaining_old:
        by_hash.setdefault(block.hash, []).append(block)

    for block in new_blocks:
        bucket = by_hash.get(block.hash)
        if bucket:
            matched = bucket.pop(0)
            remaining_old.remove(matched)
            # Переносим bid: идентичность блока живёт дольше его текста.
            block.bid = matched.bid
            unchanged.append((matched, block))
        else:
            remaining_new.append(block)

    old_tokens = [b.text.split() for b in remaining_old]
    new_tokens = [b.text.split() for b in remaining_new]

    candidates: list[tuple[float, int, int]] = []
    for i, _ in enumerate(remaining_old):
        for j, _ in enumerate(remaining_new):
            matcher = _word_matcher(old_tokens[i], new_tokens[j])
            # Дешёвые верхние оценки отсекают заведомо непохожие пары до
            # полного сравнения. На 10-15 блоках в части это не
            # принципиально, но и стоит ноль.
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            ratio = matcher.ratio()
            if ratio >= threshold:
                candidates.append((ratio, i, j))

    candidates.sort(reverse=True)
    used_old: set[int] = set()
    used_new: set[int] = set()
    edited: list[tuple[Block, Block, float]] = []
    for ratio, i, j in candidates:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        old, new = remaining_old[i], remaining_new[j]
        new.bid = old.bid
        edited.append((old, new, ratio))
        log.debug("правка: %s ratio=%.2f", old.bid, ratio)

    deleted = [b for i, b in enumerate(remaining_old) if i not in used_old]
    added = [b for j, b in enumerate(remaining_new) if j not in used_new]
    return unchanged, edited, added, deleted


def diff_documents(
    old_parts: list[Part], new_parts: list[Part], *, threshold: float
) -> list[Event]:
    """Сравнить два состояния и получить список событий.

    Части сопоставляются по НОМЕРУ, а не по позиции: множественная разность
    видит часть, вставленную в середину, а сравнение «номер больше
    максимального» — нет.
    """
    old_by_number = {p.number: p for p in old_parts}
    new_by_number = {p.number: p for p in new_parts}
    events: list[Event] = []

    for number in sorted(set(new_by_number) - set(old_by_number)):
        part = new_by_number[number]
        events.append(
            Event(
                kind=PART_ADDED,
                part_number=number,
                part_title=part.title,
                bid="",
                part_blocks=part.tips,
            )
        )

    for number in sorted(set(old_by_number) - set(new_by_number)):
        part = old_by_number[number]
        events.append(
            Event(
                kind=PART_REMOVED,
                part_number=number,
                part_title=part.title,
                bid="",
                part_blocks=part.tips,
            )
        )

    for number in sorted(set(old_by_number) & set(new_by_number)):
        old_part, new_part = old_by_number[number], new_by_number[number]
        if old_part.hash == new_part.hash:
            continue  # третий уровень каскада: часть не трогали

        _, edited, added, deleted = _match_blocks(
            old_part.blocks, new_part.blocks, threshold
        )

        for old_block, new_block, ratio in edited:
            events.append(
                Event(
                    kind=BLOCK_EDITED,
                    part_number=number,
                    part_title=new_part.title,
                    bid=new_block.bid,
                    old_block=old_block,
                    new_block=new_block,
                    ratio=ratio,
                )
            )
        for block in added:
            events.append(
                Event(
                    kind=BLOCK_ADDED,
                    part_number=number,
                    part_title=new_part.title,
                    bid=block.bid,
                    new_block=block,
                )
            )
        for block in deleted:
            events.append(
                Event(
                    kind=BLOCK_DELETED,
                    part_number=number,
                    part_title=old_part.title,
                    bid=block.bid,
                    old_block=block,
                )
            )

    return _drop_moves(events)


def _drop_moves(events: list[Event]) -> list[Event]:
    """Убрать перемещения блоков между частями.

    Блок с тем же хешем, который исчез в одной части и появился в другой, —
    это перестановка, а не новость. Без этой чистки перенос совета из
    Part 3 в Part 9 приехал бы двумя сообщениями: «удалили» и «добавили».
    """
    deleted_by_hash: dict[str, Event] = {
        e.old_block.hash: e for e in events if e.kind == BLOCK_DELETED and e.old_block
    }
    moved_hashes: set[str] = set()

    for event in events:
        if event.kind == BLOCK_ADDED and event.new_block:
            counterpart = deleted_by_hash.get(event.new_block.hash)
            if counterpart is not None:
                moved_hashes.add(event.new_block.hash)
                log.info(
                    "перемещение блока %s: Part %d -> Part %d (не событие)",
                    event.new_block.bid, counterpart.part_number, event.part_number,
                )

    if not moved_hashes:
        return events

    kept: list[Event] = []
    for event in events:
        block = event.new_block if event.kind == BLOCK_ADDED else event.old_block
        if event.kind in (BLOCK_ADDED, BLOCK_DELETED) and block and block.hash in moved_hashes:
            continue
        kept.append(event)
    return kept


# --------------------------------------------------------------------------
# Значимость правки
# --------------------------------------------------------------------------


def split_minor_edits(
    events: list[Event], minor_ratio: float
) -> tuple[list[Event], list[Event]]:
    """Отделить правки, ради которых не стоит будить модель.

    Возвращает (мелочь, остальное); порядок внутри обеих половин сохраняется,
    потому что от него зависит порядок доставки.

    Зачем: исправленная на сайте опечатка доходила до модели наравне с
    переписанным заново советом — те же $0.08–0.27 и то же сообщение в
    Telegram. Похожесть матчер считал и выбрасывал; теперь она решает.

    Мелочью бывает ТОЛЬКО правка. У добавленного и удалённого совета
    сравнивать не с чем, похожести нет, и отсутствие не имеет права сойти
    за «очень похоже»: в эту сторону ошибка съедает новый совет молча.

    Условий два, и второе не заменяет первое:

      похожесть выше порога — правка мала ОТНОСИТЕЛЬНО длины совета. Мера
      та же, что у матчинга (см. similarity), поэтому порог зависит от
      длины: одно слово из 119 (медиана сайта) даёт 0.992, из 43 (самый
      короткий совет) — 0.977. Перекос сознательный: в коротком тексте
      слово весит больше, и промолчать о нём опаснее, чем потратить разбор;

      замена ровно одного слова — правка мала САМА ПО СЕБЕ, независимо от
      длины (см. is_single_word_replacement). Без этого условия вставленное
      «not» проходило бы молча, а на самых длинных советах — и правка пяти
      слов подряд.

    Остаточный риск назван прямо: «supported» -> «unsupported» это замена
    одного слова, и она пройдёт молча. Мера текста такое не ловит; ловит
    только разбор, а он стоит денег. Поэтому пропущенное остаётся в
    state/delivered.json и видно в git diff.
    """

    def is_minor(event: Event) -> bool:
        if event.kind != BLOCK_EDITED or event.ratio is None:
            return False
        return is_minor_change(event.old_block.text, event.new_block.text, minor_ratio)

    minor = [e for e in events if is_minor(e)]
    significant = [e for e in events if not is_minor(e)]
    for event in minor:
        log.info("правка ниже порога значимости (ratio=%.3f): %s", event.ratio, event.headline)
    return minor, significant


# --------------------------------------------------------------------------
# Предскан на инъекции
# --------------------------------------------------------------------------

_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "disregard previous",
    "you are now",
    "new instructions",
    "system prompt",
    "<|im_start|>",
    "</system>",
    "assistant:",
)


def flag_injections(events: list[Event]) -> None:
    """Дешёвый предскан текста событий на попытки обратиться к агенту.

    Не защита — защита это белый список инструментов и правило «внешний
    текст есть данные». Это ранний маркер: если сработал, разбор придёт с
    пометкой, и модель отдельно попросят процитировать найденное как факт.
    Регексп ловит только грубые случаи и это нормально: он дублирует, а не
    заменяет поле anomalies в схеме ответа.
    """
    for event in events:
        haystack = " ".join(
            b.text.lower()
            for b in (event.old_block, event.new_block, *event.part_blocks)
            if b is not None
        )
        if any(marker in haystack for marker in _INJECTION_MARKERS):
            event.injection_suspected = True
            log.warning(
                "в событии %s найден маркер обращения к агенту — помечено как аномалия",
                event.headline,
            )

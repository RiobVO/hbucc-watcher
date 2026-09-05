"""Прогон дифа по сохранённому HTML с синтетическими правками.

Зачем это существует: порог сопоставления блоков (detect.match_threshold)
разделяет «совет переписали» и «совет удалили + добавили новый». Если порог
угадан неверно, ошибка проявится на первом же настоящем событии — то есть
ровно тогда, когда разбор важен, и уже необратимо (событие уедет в Telegram
кривым). Здесь порог перестаёт быть догадкой.

    # калибровка: как классифицируется правка нарастающего размера
    python tools/replay.py --sweep 22:1

    # калибровка порога значимости: какая правка пройдёт молча, без разбора
    python tools/replay.py --minor

    # проверить конкретный сценарий целиком
    python tools/replay.py --scenario edit
    python tools/replay.py --scenario add
    python tools/replay.py --scenario delete
    python tools/replay.py --scenario move
    python tools/replay.py --scenario newpart
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.config import Config, setup_logging  # noqa: E402
from watcher.detect import (  # noqa: E402
    diff_documents,
    is_minor_change,
    is_single_word_replacement,
    similarity,
)
from watcher.source import Block, Part, parse, part_hash, sha256_of  # noqa: E402

FILLER = "reconsidered adjusted revised amended updated altered replaced".split()


def load_html(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def rebuild(block: Block, text: str) -> Block:
    """Пересобрать блок с новым текстом, как это сделал бы парсер."""
    return Block(
        bid="b-" + hashlib.sha256(
            f"x\x00{block.heading}\x00{text}".encode("utf-8")
        ).hexdigest()[:8],
        kind=block.kind,
        heading=block.heading,
        text=text,
        hash=sha256_of(text),
        source_url=block.source_url,
        links=list(block.links),
        refs_parts=list(block.refs_parts),
    )


def refresh(part: Part) -> Part:
    part.hash = part_hash(part.blocks)
    return part


def mutate(text: str, fraction: float) -> str:
    """Заменить примерно `fraction` слов текста на нейтральные заполнители."""
    words = text.split()
    if not words:
        return text
    step = max(1, int(round(1 / fraction))) if fraction > 0 else len(words) + 1
    out = [
        FILLER[i % len(FILLER)] if i % step == 0 else word
        for i, word in enumerate(words)
    ]
    return " ".join(out)


def pick(parts: list[Part], spec: str) -> tuple[Part, int]:
    part_no, _, index = spec.partition(":")
    part = next((p for p in parts if p.number == int(part_no)), None)
    if part is None:
        raise SystemExit(f"части {part_no} нет в документе")
    tips = [i for i, b in enumerate(part.blocks) if b.kind == "tip"]
    idx = tips[int(index or 0)]
    return part, idx


def sweep(parts: list[Part], spec: str, threshold: float) -> None:
    part, idx = pick(parts, spec)
    original = part.blocks[idx]
    print(f"Часть {part.number}, совет «{original.heading}» ({len(original.text)} символов)")
    print(f"Порог из конфига: {threshold}\n")
    print(f"{'испорчено':>10} {'ratio':>7}  классификация")
    print("-" * 52)

    flip = None
    for pct in (2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90):
        mutated_text = mutate(original.text, pct / 100)
        ratio = similarity(original.text, mutated_text)

        new_parts = copy.deepcopy(parts)
        target = next(p for p in new_parts if p.number == part.number)
        target.blocks[idx] = rebuild(original, mutated_text)
        refresh(target)

        events = diff_documents(parts, new_parts, threshold=threshold)
        verdict = "+".join(sorted(e.kind for e in events)) or "нет событий"
        if flip is None and "block_deleted" in verdict:
            flip = pct
        print(f"{pct:>9}% {ratio:>7.3f}  {verdict}")

    print("-" * 52)
    if flip is None:
        print("Правка классифицируется как block_edited на всём диапазоне.")
        print("Порог можно поднимать — он сейчас слишком мягкий и склеит")
        print("даже полностью переписанный совет с прежним.")
    else:
        print(f"Перелом на {flip}% испорченного текста: до него — block_edited,")
        print(f"после — block_deleted + block_added.")
        print("Порог считается адекватным, если перелом лежит в 40-70%:")
        print("мелкая правка формулировки должна оставаться правкой, а")
        print("замена совета на другой по смыслу — удалением и добавлением.")


def replace_scattered(words: list[str], count: int) -> list[str]:
    """Заменить count слов, разбросанных по тексту, — правка формулировок."""
    out = list(words)
    step = max(1, len(out) // (count + 1))
    for k in range(count):
        out[min(len(out) - 1, step * (k + 1))] = FILLER[k % len(FILLER)]
    return out


def rewrite_run(words: list[str], count: int) -> list[str]:
    """Переписать count подряд идущих слов — переписанное предложение."""
    out = list(words)
    start = len(out) // 3
    for i in range(start, min(len(out), start + count)):
        out[i] = FILLER[(i - start) % len(FILLER)]
    return out


# Типовые правки от самой безобидной к содержательной. Первые две — то, ради
# чего порог значимости и заводится; последние две обязаны доходить до модели.
MINOR_CASES: list[tuple[str, object]] = [
    ("опечатка в одном слове", lambda w: replace_scattered(w, 1)),
    ("вставлено одно слово", lambda w: w[: len(w) // 2] + ["not"] + w[len(w) // 2:]),
    ("удалено одно слово", lambda w: w[: len(w) // 2] + w[len(w) // 2 + 1:]),
    ("правка двух слов", lambda w: replace_scattered(w, 2)),
    ("правка пяти слов", lambda w: replace_scattered(w, 5)),
    ("переписано предложение", lambda w: rewrite_run(w, 10)),
]

CANDIDATE_THRESHOLDS = (0.97, 0.98, 0.99, 0.995)


def minor(parts: list[Part], minor_ratio: float) -> None:
    """Калибровка порога значимости на всех настоящих советах сайта.

    Порог отделяет «мелочь, о которой молчим» от «правки, ради которой
    будим модель». Ошибка здесь необратима в обе стороны: слишком низкий
    порог молча съест содержательную правку, слишком высокий вернёт разбор
    за $0.20 на каждую исправленную запятую. Здесь он перестаёт быть
    догадкой: правки типовые, советы настоящие.
    """
    tips = [b for p in parts for b in p.blocks if b.kind == "tip"]
    lengths = sorted(len(b.text.split()) for b in tips)
    print(f"Советов: {len(tips)}; слов в совете: {lengths[0]}..{lengths[-1]}, "
          f"медиана {lengths[len(lengths) // 2]}")
    print(f"Порог значимости из конфига: {minor_ratio}\n")

    # Рабочий порог обязан быть среди колонок: иначе таблица показывает
    # соседние значения, а про то, которое стоит в конфиге, молчит.
    thresholds = sorted({*CANDIDATE_THRESHOLDS, minor_ratio})

    # Пары «было -> стало» строятся один раз, а решение по ним принимает
    # прод своей же функцией: повторённое здесь руками условие означало бы
    # калибровку не того, что работает.
    versions: dict[str, list[tuple[str, str]]] = {
        label: [(tip.text, " ".join(mutate_words(tip.text.split()))) for tip in tips]
        for label, mutate_words in MINOR_CASES
    }

    print(f"{'правка':<24} {'ratio min':>9} {'медиана':>9} {'max':>9}   замена слова")
    print("-" * 70)
    for label, pairs in versions.items():
        ratios = sorted(similarity(old, new) for old, new in pairs)
        replacements = sum(1 for old, new in pairs if is_single_word_replacement(old, new))
        print(
            f"{label:<24} {ratios[0]:>9.3f} {ratios[len(ratios) // 2]:>9.3f} "
            f"{ratios[-1]:>9.3f}   {replacements:>3} из {len(pairs)}"
        )

    print(f"\nСколько советов из {len(tips)} прошло бы молча, при разном пороге:")
    header = "".join(f"{t:>9}" for t in thresholds)
    print(f"\n{'правка':<24}{header}")
    print("-" * (24 + 9 * len(thresholds)))
    for label, pairs in versions.items():
        line = "".join(
            f"{sum(1 for old, new in pairs if is_minor_change(old, new, t)):>9}"
            for t in thresholds
        )
        print(f"{label:<24}{line}")

    print("-" * (24 + 9 * len(thresholds)))
    print("Нули в строках со вставкой и удалением — работа второго условия:")
    print("похожесть там выше порога, но мелочью считается только замена")
    print("ровно одного слова. Иначе вставленное «not» уходило бы молча.")
    print("Столбец «замена слова» меньше числа советов и в первой строке:")
    print("токен с цифрой, флагом, путём или парой «ключ=значение» словом")
    print("не считается — одна такая замена меняет сразу две величины.")
    print("Остаточный риск остаётся и назван прямо: «supported» ->")
    print("«unsupported» это замена одного слова, и мерой текста она не")
    print("отличается от исправленной опечатки.")


def scenario(parts: list[Part], name: str, threshold: float) -> None:
    new_parts = copy.deepcopy(parts)
    last = new_parts[-1]

    if name == "edit":
        part, idx = pick(new_parts, f"{last.number}:0")
        part.blocks[idx] = rebuild(part.blocks[idx], mutate(part.blocks[idx].text, 0.10))
        refresh(part)
    elif name == "add":
        block = rebuild(last.blocks[-1], "A brand new tip that did not exist before today.")
        block.heading = "Synthetic New Tip"
        last.blocks.append(block)
        last.declared_tips = (last.declared_tips or 0) + 1
        refresh(last)
    elif name == "delete":
        tips = [i for i, b in enumerate(last.blocks) if b.kind == "tip"]
        last.blocks.pop(tips[-1])
        last.declared_tips = (last.declared_tips or 1) - 1
        refresh(last)
    elif name == "move":
        tips = [i for i, b in enumerate(last.blocks) if b.kind == "tip"]
        moved = last.blocks.pop(tips[-1])
        new_parts[0].blocks.append(moved)
        refresh(last)
        refresh(new_parts[0])
    elif name == "newpart":
        fresh = copy.deepcopy(last)
        fresh.number = max(p.number for p in parts) + 1
        fresh.id = f"part-{fresh.number}"
        fresh.title = "Synthetic Brand New Part"
        fresh.order = len(new_parts) + 1
        new_parts.append(fresh)
    else:
        raise SystemExit(f"неизвестный сценарий: {name}")

    events = diff_documents(parts, new_parts, threshold=threshold)
    print(f"Сценарий «{name}»: событий {len(events)}\n")
    for event in events:
        print(f"  {event.kind:<14} {event.headline}")
        print(f"    event_id: {event.event_id[:24]}...")
        if event.old_block and event.new_block:
            ratio = similarity(event.old_block.text, event.new_block.text)
            print(f"    ratio={ratio:.3f}  bid сохранён: {event.old_block.bid == event.new_block.bid}")
        if event.part_blocks:
            print(f"    советов в части: {len(event.part_blocks)}")
    if not events:
        print("  (пусто — так и должно быть для перемещения)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="state/last_raw.html.gz", help="сохранённый HTML")
    ap.add_argument("--sweep", metavar="PART:INDEX", help="калибровка порога, напр. 22:1")
    ap.add_argument(
        "--minor", action="store_true", help="калибровка порога значимости правки"
    )
    ap.add_argument(
        "--scenario",
        choices=["edit", "add", "delete", "move", "newpart"],
        help="прогнать канонический сценарий",
    )
    ap.add_argument("--threshold", type=float, help="переопределить порог из конфига")
    args = ap.parse_args()

    setup_logging()
    cfg = Config.load()
    threshold = args.threshold or cfg.get("detect", "match_threshold")

    doc = parse(load_html(Path(args.file)))
    print(f"База: {len(doc.parts)} частей, {doc.tips_count} советов\n")

    if args.sweep:
        sweep(doc.parts, args.sweep, threshold)
    elif args.minor:
        minor(doc.parts, cfg.get("detect", "minor_edit_ratio"))
    elif args.scenario:
        scenario(doc.parts, args.scenario, threshold)
    else:
        raise SystemExit("укажи --sweep, --minor или --scenario")


if __name__ == "__main__":
    main()

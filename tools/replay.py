"""Прогон дифа по сохранённому HTML с синтетическими правками.

Зачем это существует: порог сопоставления блоков (detect.match_threshold)
разделяет «совет переписали» и «совет удалили + добавили новый». Если порог
угадан неверно, ошибка проявится на первом же настоящем событии — то есть
ровно тогда, когда разбор важен, и уже необратимо (событие уедет в Telegram
кривым). Здесь порог перестаёт быть догадкой.

    # калибровка: как классифицируется правка нарастающего размера
    python tools/replay.py --sweep 22:1

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
from watcher.detect import diff_documents, similarity  # noqa: E402
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
    elif args.scenario:
        scenario(doc.parts, args.scenario, threshold)
    else:
        raise SystemExit("укажи --sweep или --scenario")


if __name__ == "__main__":
    main()

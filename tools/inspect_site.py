"""Разовая утилита калибровки парсера.

Показывает, во что реально разложился документ: сколько частей, сколько
блоков в каждой, какой профиль разбиения выбран, как выглядят заголовки.

Это инструмент для человека, а не часть системы. Он существует потому,
что селекторы чужого сайта нельзя знать заранее — их надо увидеть.

    python tools/inspect_site.py                 # с живого сайта
    python tools/inspect_site.py --save page.html
    python tools/inspect_site.py --file page.html
    python tools/inspect_site.py --part 22       # показать блоки части 22
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.config import Config, setup_logging  # noqa: E402
from watcher.source import fetch, parse  # noqa: E402


def load_html(args: argparse.Namespace, cfg: Config) -> str:
    if args.file:
        path = Path(args.file)
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        return data.decode("utf-8", errors="replace")

    src = cfg.section("source")
    result = fetch(
        src["url"],
        timeout_seconds=src["timeout_seconds"],
        retries=src["retries"],
        backoff_seconds=src["retry_backoff_seconds"],
        user_agent=src["user_agent"],
    )
    if result.html is None:
        raise SystemExit("сервер ответил 304 — нечего разбирать")
    print(f"ETag:          {result.etag}")
    print(f"Last-Modified: {result.last_modified}")
    print(f"Размер:        {result.raw_bytes} байт")
    if args.save:
        Path(args.save).write_text(result.html, encoding="utf-8")
        print(f"Сохранено:     {args.save}")
    return result.html


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="разобрать сохранённый HTML (.html или .html.gz)")
    ap.add_argument("--save", help="сохранить скачанный HTML в файл")
    ap.add_argument("--part", type=int, help="показать блоки конкретной части")
    ap.add_argument("--full", action="store_true", help="показать текст блоков целиком")
    args = ap.parse_args()

    setup_logging()
    cfg = Config.load()
    html = load_html(args, cfg)

    doc = parse(html)

    print()
    print("=" * 72)
    print(f"Профиль парсера : {doc.parser_profile}")
    print(f"Частей          : {len(doc.parts)}")
    print(f"Блоков всего    : {doc.blocks_count}")
    print(f"Символов текста : {doc.text_chars}")
    print(f"content_hash    : {doc.content_hash}")
    print("=" * 72)
    print()

    if args.part is not None:
        target = next((p for p in doc.parts if p.number == args.part), None)
        if target is None:
            raise SystemExit(f"части {args.part} нет в документе")
        print(f"### {target.title}   ({len(target.blocks)} блоков)")
        for i, block in enumerate(target.blocks, 1):
            text = block.text if args.full else block.text[:220]
            print(f"\n  [{i}] {block.bid}  kind={block.kind}")
            if block.heading:
                print(f"      heading: {block.heading}")
            print(f"      len={len(block.text)}  links={len(block.links)}  refs={block.refs_parts}")
            print(f"      {text}{'' if args.full else '...'}")
            for link in block.links[:5]:
                print(f"      -> {link}")
        return

    for part in doc.parts:
        headings = [b.heading for b in part.blocks if b.heading]
        preview = "; ".join(headings[:3])
        print(f"{part.number:>3}. {part.title[:60]:<60} блоков={len(part.blocks):>3}")
        if preview:
            print(f"     {preview[:110]}")


if __name__ == "__main__":
    main()

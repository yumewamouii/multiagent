"""CLI: python -m app.parsers.telegram_export_cli ingest result.json"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram JSON export — parse / ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Статистика экспорта без LLM")
    p_scan.add_argument("export_path", type=Path)

    p_parse = sub.add_parser("parse", help="Нормализованный парсинг в JSON")
    p_parse.add_argument("export_path", type=Path)
    p_parse.add_argument("-o", "--output", type=Path)
    p_parse.add_argument("--limit", type=int, default=0, help="0 = без лимита")

    p_ingest = sub.add_parser("ingest", help="Полный цикл → PostgreSQL")
    p_ingest.add_argument("export_path", type=Path)
    p_ingest.add_argument("--limit", type=int, default=0, help="0 = все сообщения")
    p_ingest.add_argument("--no-heuristic-short-circuit", action="store_true")

    p_year = sub.add_parser("filter-year", help="Новый JSON только за указанный год")
    p_year.add_argument("export_path", type=Path)
    p_year.add_argument(
        "-o",
        "--output",
        type=Path,
        help="По умолчанию: result_YYYY.json рядом с исходником",
    )
    p_year.add_argument("--year", type=int, default=2026)

    args = parser.parse_args()
    path = args.export_path.expanduser().resolve()
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    if args.command == "filter-year":
        from app.parsers.filter_telegram_export import filter_telegram_export_by_year

        out = args.output
        if out is None:
            out = path.parent / f"{path.stem}_{args.year}{path.suffix}"
        stats = filter_telegram_export_by_year(path, out, year=args.year)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    if args.command == "scan":
        from app.parsers.telegram_export import scan_export

        print(json.dumps(scan_export(path), ensure_ascii=False, indent=2))
        return 0

    if args.command == "parse":
        from app.parsers.telegram_export import parse_export_file

        limit = None if args.limit == 0 else args.limit
        data = parse_export_file(path, limit=limit)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
            print(f"saved {args.output}", file=sys.stderr)
        else:
            print(text)
        return 0

    from app.services.telegram_ingest import ingest_telegram_export_file

    limit = None if args.limit == 0 else args.limit
    result = ingest_telegram_export_file(
        path,
        limit=limit,
        use_heuristic_short_circuit=not args.no_heuristic_short_circuit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

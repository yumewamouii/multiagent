"""CLI: python -m app.parsers.docdoc crawl --base-url https://irk.docdoc.ru/ --max-services 5 --save-db"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="DocDoc / СберЗдоровье — загрузка и парсинг")
    sub = parser.add_subparsers(dest="command", required=True)

    p_url = sub.add_parser("url", help="Одна страница по URL")
    p_url.add_argument("page_url", help="Например https://irk.docdoc.ru/service/...")
    p_url.add_argument("-o", "--output", type=Path, help="JSON-файл результата")
    p_url.add_argument("--no-headless", action="store_true")
    p_url.add_argument("--full-reviews", action="store_true", help="Для страницы услуги — все отзывы")

    p_crawl = sub.add_parser("crawl", help="Главная + услуги + клиники + врачи")
    p_crawl.add_argument("--base-url", default="https://irk.docdoc.ru/")
    p_crawl.add_argument("--max-services", type=int, default=10, help="0 = все")
    p_crawl.add_argument("--max-clinics", type=int, default=5, help="0 = все")
    p_crawl.add_argument("--max-doctors", type=int, default=0, help="Профили /doctor/... (0 = нет)")
    p_crawl.add_argument("--no-clinics", action="store_true")
    p_crawl.add_argument("--no-full-reviews", action="store_true", help="Без кликов «Показать ещё»")
    p_crawl.add_argument(
        "--no-hub-discovery",
        action="store_true",
        help="Только услуги с главной (~177), без хабов направлений",
    )
    p_crawl.add_argument("--save-db", action="store_true", help="Сохранить в PostgreSQL")
    p_crawl.add_argument("-o", "--output", type=Path, default=Path("docdoc_crawl.json"))
    p_crawl.add_argument("--no-headless", action="store_true")

    args = parser.parse_args()
    headless = not args.no_headless

    if args.command == "url":
        if getattr(args, "full_reviews", False):
            from app.parsers.docdoc.parse import parse_docdoc_url_full_reviews

            data = parse_docdoc_url_full_reviews(args.page_url, headless=headless)
        else:
            from app.parsers.docdoc.parse import parse_docdoc_url

            data = parse_docdoc_url(args.page_url, headless=headless)
    else:
        from app.parsers.docdoc.crawl import crawl_docdoc

        max_svc = None if args.max_services == 0 else args.max_services
        max_cli = None if args.max_clinics == 0 else args.max_clinics

        def on_progress(phase: str, i: int, n: int) -> None:
            print(f"{phase}: {i}/{n}", flush=True)

        data = crawl_docdoc(
            args.base_url,
            max_services=max_svc,
            max_clinics=max_cli,
            max_doctor_profiles=args.max_doctors,
            fetch_clinics=not args.no_clinics,
            full_reviews=not args.no_full_reviews,
            discover_category_hubs=not args.no_hub_discovery,
            on_progress=on_progress,
        )

        if args.save_db and data.get("ok"):
            from app.services.docdoc_ingest import ingest_docdoc_crawl_result

            ing = ingest_docdoc_crawl_result(data)
            data["db"] = ing
            print(f"DB: source_id={ing.get('source_id')} inserted={ing.get('inserted')}", file=sys.stderr)

    out_path = getattr(args, "output", None)
    if out_path and args.command == "crawl":
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved {out_path}", file=sys.stderr)
    elif out_path:
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved {out_path}", file=sys.stderr)
    elif args.command == "url":
        print(json.dumps(data, ensure_ascii=False, indent=2))

    return 0 if data.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

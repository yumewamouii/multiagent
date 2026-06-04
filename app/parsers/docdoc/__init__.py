from app.parsers.docdoc.crawl import crawl_docdoc, fetch_and_parse
from app.parsers.docdoc.fetch import DocDocFetchError, fetch_html
from app.parsers.docdoc.main_page import extract_service_urls_from_main
from app.parsers.docdoc.parse import parse_docdoc_html, parse_docdoc_url, parse_docdoc_url_full_reviews

__all__ = [
    "DocDocFetchError",
    "crawl_docdoc",
    "extract_service_urls_from_main",
    "fetch_and_parse",
    "fetch_html",
    "parse_docdoc_html",
    "parse_docdoc_url",
    "parse_docdoc_url_full_reviews",
]

"""Главная регионального поддомена: сбор URL услуг /service/..."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

_SERVICE_LEAF_PATH = re.compile(r'href=["\'](/service/[a-z0-9_-]+/[a-z0-9_-]+)["\']', re.I)
_CATEGORY_HUB_PATH = re.compile(r'href=["\'](/service/[a-z0-9_-]+)["\']', re.I)


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def extract_service_urls_from_main(html: str, base_url: str = "https://irk.docdoc.ru/") -> list[str]:
    """Конкретные услуги: /service/{категория}/{услуга}."""
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    found: list[str] = []
    for m in _SERVICE_LEAF_PATH.finditer(html):
        found.append(urljoin(base, m.group(1)))
    if not found:
        for m in re.finditer(r"/service/[a-z0-9_-]+/[a-z0-9_-]+", html, re.I):
            found.append(urljoin(base, m.group(0)))
    return _dedupe_urls(found)


def extract_category_hub_paths(html: str) -> list[str]:
    """
    Хабы направлений: /service/stomatologiya (без второго сегмента).
    На главной ~28 ссылок; внутри хаба сотни leaf-URL.
    """
    hubs: list[str] = []
    seen: set[str] = set()
    for m in _CATEGORY_HUB_PATH.finditer(html):
        path = m.group(1)
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "service":
            continue
        if path in seen:
            continue
        seen.add(path)
        hubs.append(path)
    return sorted(hubs)


def discover_service_urls(
    main_html: str,
    base_url: str,
    hub_html_by_path: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """
    Объединяет услуги с главной и со страниц направлений.
    Возвращает (urls, stats).
    """
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    from_main = extract_service_urls_from_main(main_html, base)
    merged = list(from_main)
    stats = {"from_main": len(from_main), "from_hubs": 0, "hubs_scanned": 0}

    if hub_html_by_path:
        for path, html in hub_html_by_path.items():
            stats["hubs_scanned"] += 1
            found = extract_service_urls_from_main(html, base)
            stats["from_hubs"] += len(found)
            merged.extend(found)

    urls = _dedupe_urls(merged)
    stats["total_unique"] = len(urls)
    return urls, stats


def infer_city_slug_from_host(page_url: str) -> str:
    host = urlparse(page_url).netloc or ""
    sub360 = host.split(".")[0] if host else ""
    return sub360

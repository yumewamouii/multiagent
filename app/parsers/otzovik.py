from urllib.parse import urljoin

from bs4 import BeautifulSoup


def extract_category_links(html: str, base_url: str = "https://otzovik.com/sitemap/") -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.select("li a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        if href.startswith("/auto/"):
            links.append(urljoin(base_url, href))
    return dedupe_preserve_order(links)


def extract_product_links(html: str, base_url: str = "https://otzovik.com/sitemap/") -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.select("div.product-list div.item a.product-name[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        links.append(urljoin(base_url, href))
    return dedupe_preserve_order(links)


def extract_pager_links(html: str, base_url: str = "https://otzovik.com/sitemap/") -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a in soup.select("div.pager a.pager-item[href], div.pager a.next[href], div.pager a.last[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        links.append(urljoin(base_url, href))

    return dedupe_preserve_order(links)


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
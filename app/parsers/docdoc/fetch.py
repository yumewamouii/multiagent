"""Загрузка страниц DocDoc через браузер (обход JS-заглушки)."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from app.parsers.docdoc.clinic_reviews_fetch import collect_clinic_reviews_with_page
from app.parsers.docdoc.playwright_page import goto_and_settle, safe_page_content
from app.parsers.docdoc.reviews_fetch import (
    collect_service_reviews_with_page,
    service_reviews_url,
)

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_PW_LOCK = threading.Lock()
_PW_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_PW_THREAD_PREFIX = "docdoc-playwright"


class DocDocFetchError(RuntimeError):
    pass


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def _playwright_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _PW_POOL
    if _PW_POOL is None:
        _PW_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=_PW_THREAD_PREFIX,
        )
    return _PW_POOL


def _on_playwright_thread() -> bool:
    return threading.current_thread().name.startswith(_PW_THREAD_PREFIX)


def _run_playwright_isolated(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    Sync Playwright нельзя вызывать из asyncio-loop (FastAPI/uvicorn).
    Всегда выполняем в отдельном потоке без running loop.
    """
    if _on_playwright_thread():
        with _PW_LOCK:
            return func(*args, **kwargs)

    def _call() -> Any:
        with _PW_LOCK:
            return func(*args, **kwargs)

    raw_timeout = os.getenv("DOCDOC_FETCH_THREAD_TIMEOUT_SEC", "0").strip()
    timeout_sec = float(raw_timeout) if raw_timeout else 0.0
    future = _playwright_pool().submit(_call)
    try:
        if timeout_sec > 0:
            return future.result(timeout=timeout_sec)
        return future.result()
    except concurrent.futures.TimeoutError as exc:
        raise DocDocFetchError(
            f"таймаут одной операции Playwright ({timeout_sec:.0f}s). "
            f"Увеличьте DOCDOC_FETCH_THREAD_TIMEOUT_SEC или оставьте 0 (без лимита). "
            f"Частичные данные — в docdoc_crawl_checkpoint.json"
        ) from exc
    except Exception as exc:
        msg = str(exc)
        if "asyncio loop" in msg.lower():
            raise DocDocFetchError(
                "Playwright sync API попал в asyncio-loop. "
                "Перезапустите uvicorn; при повторе — обновите зависимости."
            ) from exc
        if "executable doesn't exist" in msg.lower() or "browserType.launch" in msg:
            raise DocDocFetchError(
                "Chromium для Playwright не установлен. "
                "Выполните: pip install playwright && playwright install chromium"
            ) from exc
        raise


def _resolve_fetch_options(
    *,
    wait_ms: int | None,
    headless: bool | None,
    timeout_ms: int | None,
) -> tuple[int, bool, int]:
    resolved_wait = wait_ms if wait_ms is not None else int(os.getenv("DOCDOC_FETCH_WAIT_MS", "4000"))
    if headless is None:
        resolved_headless = os.getenv("DOCDOC_HEADLESS", "true").lower() == "true"
    else:
        resolved_headless = headless
    resolved_timeout = timeout_ms if timeout_ms is not None else int(os.getenv("DOCDOC_FETCH_TIMEOUT_MS", "60000"))
    return resolved_wait, resolved_headless, resolved_timeout


def _launch_browser(p, *, headless: bool):
    return p.chromium.launch(
        headless=headless,
        args=["--disable-dev-shm-usage"],
    )


def _new_browser_context(browser):
    return browser.new_context(
        user_agent=os.getenv("DOCDOC_USER_AGENT", DEFAULT_USER_AGENT),
        locale="ru-RU",
    )


def _fetch_html_impl(
    url: str,
    *,
    wait_ms: int,
    headless: bool,
    timeout_ms: int,
) -> str:
    from playwright.sync_api import sync_playwright

    log.info("fetch %s (headless=%s, wait_ms=%s)", url, headless, wait_ms)

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        try:
            context = _new_browser_context(browser)
            page = context.new_page()
            goto_and_settle(page, url, timeout_ms=timeout_ms, settle_ms=wait_ms)
            html = safe_page_content(page)
        finally:
            browser.close()

    if len(html) < 8000 and "exhkqyad" in html:
        raise DocDocFetchError(
            f"похоже на антибот-заглушку для {url}; увеличьте DOCDOC_FETCH_WAIT_MS или headless=false"
        )
    return html


def fetch_html(
    url: str,
    *,
    wait_ms: int | None = None,
    headless: bool | None = None,
    timeout_ms: int | None = None,
) -> str:
    """
    Открывает URL в Chromium (Playwright) и возвращает page.content().
    Обычный requests на irk.docdoc.ru не подходит — отдаётся антибот-страница.
    """
    if not _playwright_available():
        raise DocDocFetchError(
            "playwright не установлен. Выполните: pip install playwright && playwright install chromium"
        )

    resolved_wait, resolved_headless, resolved_timeout = _resolve_fetch_options(
        wait_ms=wait_ms,
        headless=headless,
        timeout_ms=timeout_ms,
    )
    try:
        return _run_playwright_isolated(
            _fetch_html_impl,
            url,
            wait_ms=resolved_wait,
            headless=resolved_headless,
            timeout_ms=resolved_timeout,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "err_connection_reset" in msg or "net::err_" in msg:
            raise DocDocFetchError(
                f"сеть оборвалась при загрузке {url}: {exc}. "
                "Повторите crawl через минуту или увеличьте DOCDOC_GOTO_RETRIES / DOCDOC_GOTO_RETRY_MS. "
                "При частых сбоях попробуйте DOCDOC_HEADLESS=false."
            ) from exc
        raise


def _fetch_html_batch_impl(
    urls: list[str],
    *,
    wait_ms: int,
    headless: bool,
    timeout_ms: int,
    delay_sec: float,
    on_progress: Callable[[int, int, str], None] | None,
) -> dict[str, str]:
    from playwright.sync_api import sync_playwright

    out: dict[str, str] = {}
    total = len(urls)

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        try:
            context = _new_browser_context(browser)
            page = context.new_page()
            for i, url in enumerate(urls, start=1):
                if on_progress:
                    on_progress(i, total, url)
                log.info("fetch [%s/%s] %s", i, total, url)
                try:
                    goto_and_settle(page, url, timeout_ms=timeout_ms, settle_ms=wait_ms)
                    out[url] = safe_page_content(page)
                except Exception as exc:
                    log.warning("fetch failed [%s/%s] %s: %s", i, total, url, exc)
                    out[url] = ""
                if delay_sec > 0 and i < total:
                    time.sleep(delay_sec)
        finally:
            browser.close()

    return out


def fetch_html_batch(
    urls: list[str],
    *,
    delay_sec: float | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_chunk_done: Callable[[dict[str, str], int, int], None] | None = None,
    **fetch_kwargs,
) -> dict[str, str]:
    """Последовательная загрузка URL; длинные списки — чанками (отдельный браузер на чанк)."""
    if not urls:
        return {}
    if not _playwright_available():
        raise DocDocFetchError(
            "playwright не установлен. Выполните: pip install playwright && playwright install chromium"
        )

    wait_ms, headless, timeout_ms = _resolve_fetch_options(
        wait_ms=fetch_kwargs.pop("wait_ms", None),
        headless=fetch_kwargs.pop("headless", None),
        timeout_ms=fetch_kwargs.pop("timeout_ms", None),
    )
    delay_sec = delay_sec if delay_sec is not None else float(os.getenv("DOCDOC_FETCH_DELAY_SEC", "1.0"))
    chunk_size = int(os.getenv("DOCDOC_FETCH_CHUNK_SIZE", "40"))

    out: dict[str, str] = {}
    total = len(urls)
    global_i = 0
    for start in range(0, total, chunk_size):
        chunk = urls[start : start + chunk_size]

        def _progress(i: int, _n: int, u: str) -> None:
            if on_progress:
                on_progress(global_i + i, total, u)

        part = _run_playwright_isolated(
            _fetch_html_batch_impl,
            chunk,
            wait_ms=wait_ms,
            headless=headless,
            timeout_ms=timeout_ms,
            delay_sec=delay_sec,
            on_progress=_progress if on_progress else None,
        )
        out.update(part)
        global_i += len(chunk)
        if on_chunk_done:
            on_chunk_done(out, min(global_i, total), total)
    return out


def _fetch_service_reviews_full_impl(
    service_urls: list[str],
    *,
    headless: bool,
    max_more_clicks: int,
    on_progress: Callable[[int, int, str], None] | None,
) -> dict[str, list[dict[str, Any]]]:
    from playwright.sync_api import sync_playwright

    out: dict[str, list[dict[str, Any]]] = {}
    total = len(service_urls)

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        try:
            context = _new_browser_context(browser)
            page = context.new_page()
            for i, url in enumerate(service_urls, start=1):
                if on_progress:
                    on_progress(i, total, url)
                try:
                    reviews = collect_service_reviews_with_page(
                        page,
                        url,
                        max_more_clicks=max_more_clicks,
                    )
                    out[url] = reviews
                except Exception as exc:
                    log.warning("service reviews failed [%s/%s] %s: %s", i, total, url, exc)
                    out[url] = []
        finally:
            browser.close()

    return out


def fetch_service_reviews_full(
    service_urls: list[str],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_chunk_done: Callable[[dict[str, list[dict[str, Any]]], int, int], None] | None = None,
    headless: bool | None = None,
    max_more_clicks: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    Для каждой услуги: главная + «Показать ещё» + страница /order/reviews.
    Длинный список — чанками, чтобы не упираться в таймаут одной сессии.
    """
    if not service_urls:
        return {}
    if not _playwright_available():
        raise DocDocFetchError(
            "playwright не установлен. Выполните: pip install playwright && playwright install chromium"
        )

    _, resolved_headless, _ = _resolve_fetch_options(wait_ms=None, headless=headless, timeout_ms=None)
    if max_more_clicks is None:
        max_more_clicks = int(os.getenv("DOCDOC_REVIEWS_MAX_MORE_CLICKS", "30"))
    chunk_size = int(os.getenv("DOCDOC_REVIEWS_CHUNK_SIZE", "25"))

    out: dict[str, list[dict[str, Any]]] = {}
    total = len(service_urls)
    done = 0
    for start in range(0, total, chunk_size):
        chunk = service_urls[start : start + chunk_size]
        base_i = done

        def _progress(i: int, _n: int, u: str) -> None:
            if on_progress:
                on_progress(base_i + i, total, u)

        part = _run_playwright_isolated(
            _fetch_service_reviews_full_impl,
            chunk,
            headless=resolved_headless,
            max_more_clicks=max_more_clicks,
            on_progress=_progress if on_progress else None,
        )
        out.update(part)
        done += len(chunk)
        if on_chunk_done:
            on_chunk_done(out, done, total)
    return out


def _fetch_clinic_reviews_full_impl(
    clinic_urls: list[str],
    *,
    headless: bool,
    max_more_clicks: int,
    on_progress: Callable[[int, int, str], None] | None,
) -> dict[str, list[dict[str, Any]]]:
    from playwright.sync_api import sync_playwright

    out: dict[str, list[dict[str, Any]]] = {}
    total = len(clinic_urls)

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        try:
            context = _new_browser_context(browser)
            page = context.new_page()
            for i, url in enumerate(clinic_urls, start=1):
                if on_progress:
                    on_progress(i, total, url)
                try:
                    reviews = collect_clinic_reviews_with_page(
                        page,
                        url,
                        max_more_clicks=max_more_clicks,
                    )
                    out[url] = reviews
                except Exception as exc:
                    log.warning("clinic reviews failed [%s/%s] %s: %s", i, total, url, exc)
                    out[url] = []
        finally:
            browser.close()

    return out


def fetch_clinic_reviews_full(
    clinic_urls: list[str],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_chunk_done: Callable[[dict[str, list[dict[str, Any]]], int, int], None] | None = None,
    headless: bool | None = None,
    max_more_clicks: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Для каждой клиники: SSR + `/clinics/moreReviews` + «Показать ещё»."""
    if not clinic_urls:
        return {}
    if not _playwright_available():
        raise DocDocFetchError(
            "playwright не установлен. Выполните: pip install playwright && playwright install chromium"
        )

    _, resolved_headless, _ = _resolve_fetch_options(wait_ms=None, headless=headless, timeout_ms=None)
    if max_more_clicks is None:
        max_more_clicks = int(os.getenv("DOCDOC_CLINIC_REVIEWS_MAX_MORE_CLICKS", "30"))
    chunk_size = int(os.getenv("DOCDOC_CLINIC_REVIEWS_CHUNK_SIZE", "25"))

    out: dict[str, list[dict[str, Any]]] = {}
    total = len(clinic_urls)
    done = 0
    for start in range(0, total, chunk_size):
        chunk = clinic_urls[start : start + chunk_size]
        base_i = done

        def _progress(i: int, _n: int, u: str) -> None:
            if on_progress:
                on_progress(base_i + i, total, u)

        part = _run_playwright_isolated(
            _fetch_clinic_reviews_full_impl,
            chunk,
            headless=resolved_headless,
            max_more_clicks=max_more_clicks,
            on_progress=_progress if on_progress else None,
        )
        out.update(part)
        done += len(chunk)
        if on_chunk_done:
            on_chunk_done(out, done, total)
    return out


def fetch_service_pages_dual(
    service_urls: list[str],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    **batch_kwargs,
) -> dict[str, tuple[str, str]]:
    """Быстрее, но без кликов «ещё»: (main_html, reviews_page_html) на услугу."""
    paired_urls: list[str] = []
    index_map: dict[str, tuple[str, str]] = {}
    for su in service_urls:
        rv = service_reviews_url(su)
        index_map[su] = (su, rv)
        paired_urls.extend([su, rv])

    html_by = fetch_html_batch(paired_urls, on_progress=on_progress, **batch_kwargs)
    return {su: (html_by.get(main, ""), html_by.get(rev, "")) for su, (main, rev) in index_map.items()}


def normalize_base_url(base_url: str) -> str:
    u = base_url.strip()
    if not u.startswith("http"):
        u = f"https://{u}"
    parsed = urlparse(u)
    if not parsed.netloc:
        raise ValueError(f"invalid base_url: {base_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}/"

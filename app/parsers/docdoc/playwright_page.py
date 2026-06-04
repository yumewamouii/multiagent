"""Устойчивые goto/content для Playwright (DocDoc часто редиректит после domcontentloaded)."""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def _content_retries() -> int:
    return max(1, int(os.getenv("DOCDOC_PAGE_CONTENT_RETRIES", "8")))


def _content_retry_ms() -> int:
    return max(100, int(os.getenv("DOCDOC_PAGE_CONTENT_RETRY_MS", "750")))


def _goto_retries() -> int:
    return max(1, int(os.getenv("DOCDOC_GOTO_RETRIES", "5")))


def _goto_retry_base_ms() -> int:
    return max(500, int(os.getenv("DOCDOC_GOTO_RETRY_MS", "2000")))


def _settle_ms() -> int:
    return max(0, int(os.getenv("DOCDOC_FETCH_WAIT_MS", "4000")))


def _navigation_timeout_ms() -> int:
    return max(5000, int(os.getenv("DOCDOC_FETCH_TIMEOUT_MS", "60000")))


def _is_navigating_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "navigating" in msg and "content" in msg


def _is_transient_goto_error(exc: BaseException) -> bool:
    """Сетевые/временные сбои — имеет смысл повторить goto."""
    msg = str(exc).lower()
    markers = (
        "err_connection_reset",
        "err_connection_refused",
        "err_network_changed",
        "err_internet_disconnected",
        "err_name_not_resolved",
        "err_connection_aborted",
        "err_connection_timed_out",
        "net::err_",
        "timeout",
        "timed out",
        "target closed",
        "connection closed",
        "econnreset",
    )
    return any(m in msg for m in markers)


def goto_with_retry(
    page: Any,
    url: str,
    *,
    timeout_ms: int | None = None,
    wait_until: str = "domcontentloaded",
    retries: int | None = None,
) -> None:
    attempts = retries if retries is not None else _goto_retries()
    base_delay = _goto_retry_base_ms()
    timeout = timeout_ms if timeout_ms is not None else _navigation_timeout_ms()
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as exc:
            last_exc = exc
            retryable = _is_transient_goto_error(exc) or _is_navigating_error(exc)
            if not retryable:
                raise
            if attempt + 1 >= attempts:
                break
            delay = base_delay * (attempt + 1)
            log.warning(
                "goto retry %s/%s %s: %s (pause %sms)",
                attempt + 1,
                attempts,
                url,
                exc,
                delay,
            )
            page.wait_for_timeout(delay)
    assert last_exc is not None
    raise last_exc


def goto_and_settle(
    page: Any,
    url: str,
    *,
    timeout_ms: int | None = None,
    settle_ms: int | None = None,
) -> None:
    """Открыть URL и дождаться, пока страница перестанет активно навигировать."""
    timeout = timeout_ms if timeout_ms is not None else _navigation_timeout_ms()
    settle = settle_ms if settle_ms is not None else _settle_ms()
    goto_with_retry(page, url, timeout_ms=timeout)
    try:
        page.wait_for_load_state("load", timeout=min(20000, timeout))
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=min(12000, timeout))
    except Exception:
        pass
    if settle > 0:
        page.wait_for_timeout(settle)


def safe_page_content(page: Any, *, retries: int | None = None) -> str:
    """
    page.content() с retry — DocDoc иногда ещё редиректит после goto/wait.
    """
    attempts = retries if retries is not None else _content_retries()
    delay = _content_retry_ms()
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return page.content()
        except Exception as exc:
            last_exc = exc
            if not _is_navigating_error(exc) and not _is_transient_goto_error(exc):
                raise
            if attempt + 1 < attempts:
                log.debug(
                    "page.content retry %s/%s (still navigating/unstable)",
                    attempt + 1,
                    attempts,
                )
                page.wait_for_timeout(delay)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
    assert last_exc is not None
    raise last_exc

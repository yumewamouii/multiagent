from app.parsers.docdoc.playwright_page import (
    _is_navigating_error,
    _is_transient_goto_error,
    goto_with_retry,
    safe_page_content,
)


def test_is_navigating_error():
    assert _is_navigating_error(
        RuntimeError("Page.content: Unable to retrieve content because the page is navigating")
    )
    assert not _is_navigating_error(RuntimeError("timeout"))


def test_is_transient_goto_error():
    assert _is_transient_goto_error(RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at https://x/"))
    assert _is_transient_goto_error(RuntimeError("Timeout 60000ms exceeded"))
    assert not _is_transient_goto_error(RuntimeError("404 Not Found"))


def test_goto_with_retry_on_connection_reset():
    calls = {"n": 0}

    class FakePage:
        def goto(self, url, wait_until="domcontentloaded", timeout=60000):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError(f"Page.goto: net::ERR_CONNECTION_RESET at {url}")
            self.url = url

        def wait_for_timeout(self, ms):
            return None

    goto_with_retry(FakePage(), "https://irk.docdoc.ru/", retries=3)
    assert calls["n"] == 2


def test_safe_page_content_retries_then_succeeds():
    calls = {"n": 0}

    class FakePage:
        def content(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError(
                    "Page.content: Unable to retrieve content because the page is navigating"
                )
            return "<html>ok</html>"

        def wait_for_timeout(self, _ms):
            return None

        def wait_for_load_state(self, _state, timeout=0):
            return None

    html = safe_page_content(FakePage(), retries=5)
    assert html == "<html>ok</html>"
    assert calls["n"] == 3

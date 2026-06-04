"""Нормализация HTML (в т.ч. сохранённого из view-source) и извлечение __NEXT_DATA__."""

from __future__ import annotations

import html as html_module
import json
import re
from typing import Any


def normalize_saved_html(raw: str) -> str:
    """
    Если файл сохранён из Chrome view-source (таблица с line-content),
    собирает исходный HTML. Иначе возвращает строку без изменений.
    """
    if "line-content" not in raw:
        return raw
    parts: list[str] = []
    for m in re.finditer(r'<td[^>]*class="line-content"[^>]*>(.*?)</td>', raw, re.DOTALL):
        cell = m.group(1)
        cell = re.sub(r"<span[^>]*>", "", cell)
        cell = re.sub(r"</span>", "", cell)
        parts.append(html_module.unescape(cell))
    if not parts:
        return raw
    return "".join(parts)


def extract_next_data(html_text: str) -> dict[str, Any] | None:
    m = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return None


def html_to_plain(text: str | None) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(text))
    return re.sub(r"\s+", " ", html_module.unescape(t)).strip()


def vue_attr_json_text(attr_value: str) -> str:
    """Значение вида {...} или [...] с entity &quot; вместо кавычек."""
    s = html_module.unescape(attr_value.strip())
    return s.replace("&quot;", '"')


def parse_vue_attr_json(
    html: str,
    attr_name: str,
    *,
    end_before_colon: bool = True,
) -> Any | None:
    """
    end_before_colon=True: до ближайшего `'` + пробел + `:` (мед-услуги перед :total-reviews).
    False: до следующего атрибута `name='`.
    """
    if end_before_colon:
        m = re.search(rf"{re.escape(attr_name)}='(.+?)'\s+:", html, re.DOTALL)
    else:
        m = re.search(
            rf"{re.escape(attr_name)}='(.+?)'\s+[A-Za-z_-][A-Za-z0-9_-]*\s*=",
            html,
            re.DOTALL,
        )
    if not m:
        return None
    try:
        return json.loads(vue_attr_json_text(m.group(1)))
    except json.JSONDecodeError:
        return None
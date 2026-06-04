"""
Репутационный аналитик одной сущности DocDoc (clinic/service/category/doctor).

Идея: «Проанализируй отзывы по услуге X. Что раздражает пациентов и что использовать в рекламе?».
Выход — структурированный отчёт + черновики ответов на риск-отзывы.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from app.core.llm import chat_completion, parse_json_response
from app.services.docdoc_structured_research import (
    EntityBundle,
    EntityType,
    _collect_rag_snippets,
    _entity_rag_query,
    _format_rag_snippets,
    _matches_entities,
    _rating_value,
    _sample_reviews_text,
    compute_metrics,
    group_reviews_from_crawl,
    group_reviews_from_db,
)

log = logging.getLogger(__name__)

REPORT_FIELDS: tuple[str, ...] = (
    "executive_summary",
    "what_patients_value",
    "top_complaints",
    "service_improvements",
    "landing_page_gaps",
    "ad_angle",
    "target_audience",
    "risk_topics",
)

COMPARE_FIELDS: tuple[str, ...] = (
    "summary",
    "strengths",
    "weaknesses",
    "unique_selling_points",
    "shared_complaints",
    "ad_angle",
)

ReputationDataSource = Literal["db", "json", "auto"]


def _resolve_bundle(
    *,
    entity_type: EntityType,
    entity: str,
    source_id: int | None,
    city_slug: str | None,
    crawl_path: str | None,
    data_source: ReputationDataSource,
) -> tuple[EntityBundle | None, str]:
    """Найти одну сущность в БД или JSON-крауле."""
    if data_source in ("db", "auto"):
        try:
            from app.core.db import SessionLocal

            with SessionLocal() as db:
                bundles = group_reviews_from_db(
                    db,
                    entity_type,
                    entities=[entity],
                    limit=1,
                    source_id=source_id,
                    city_slug=city_slug,
                )
            if bundles:
                return bundles[0], "db"
        except Exception as exc:
            log.warning("reputation: DB lookup failed: %s", exc)

    if data_source in ("json", "auto"):
        path = Path(crawl_path or "docdoc_crawl_last.json").expanduser()
        if not path.is_file():
            repo_root = Path(__file__).resolve().parents[2]
            alt = repo_root / (crawl_path or "docdoc_crawl_last.json")
            if alt.is_file():
                path = alt
        if path.is_file():
            try:
                crawl = json.loads(path.read_text(encoding="utf-8"))
                bundles = group_reviews_from_crawl(
                    crawl,
                    entity_type,
                    entities=[entity],
                    limit=1,
                    match_each_entity=True,
                )
                if bundles:
                    return bundles[0], "json" if data_source == "json" else "json_fallback"
            except Exception as exc:
                log.warning("reputation: JSON lookup failed: %s", exc)
    return None, data_source


def _candidate_names(
    *,
    entity_type: EntityType,
    source_id: int | None,
    city_slug: str | None,
    crawl_path: str | None,
    limit: int = 50,
) -> list[str]:
    """Собирает имена сущностей того же типа — для подсказок при entity_not_found."""
    names: list[str] = []
    try:
        from app.core.db import SessionLocal

        with SessionLocal() as db:
            bundles = group_reviews_from_db(
                db,
                entity_type,
                entities=None,
                limit=limit,
                source_id=source_id,
                city_slug=city_slug,
            )
        names = [b.entity_name for b in bundles if b.entity_name]
    except Exception:
        pass
    if names:
        return names
    path = Path(crawl_path or "docdoc_crawl_last.json").expanduser()
    if not path.is_file():
        repo_root = Path(__file__).resolve().parents[2]
        alt = repo_root / (crawl_path or "docdoc_crawl_last.json")
        if alt.is_file():
            path = alt
    if path.is_file():
        try:
            crawl = json.loads(path.read_text(encoding="utf-8"))
            bundles = group_reviews_from_crawl(
                crawl,
                entity_type,
                entities=None,
                limit=limit,
                match_each_entity=False,
            )
            names = [b.entity_name for b in bundles if b.entity_name]
        except Exception:
            pass
    return names


def _suggest_similar(query: str, names: list[str], k: int = 5) -> list[str]:
    """Подбор близких имён — простая lower-case substring + difflib fallback."""
    if not query or not names:
        return []
    q = query.casefold().strip()
    direct = [n for n in names if q in (n or "").casefold()]
    if direct:
        return direct[:k]
    import difflib

    return difflib.get_close_matches(query, names, n=k, cutoff=0.5)


def _detailed_metrics(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    base = compute_metrics(reviews)
    if not reviews:
        return {**base, "median_rating": None, "p10_rating": None, "negative_unanswered_count": 0}
    ratings = sorted(r for r in (_rating_value(x) for x in reviews) if r is not None)
    median = ratings[len(ratings) // 2] if ratings else None
    p10 = ratings[max(0, int(len(ratings) * 0.1) - 1)] if ratings else None
    negative_unanswered = sum(
        1
        for x in reviews
        if (_rating_value(x) is not None and _rating_value(x) < 7)
        and not (x.get("answer") or "").strip()
    )
    return {
        **base,
        "median_rating": median,
        "p10_rating": p10,
        "negative_unanswered_count": negative_unanswered,
    }


def _pick_risk_reviews(reviews: list[dict[str, Any]], k: int = 5) -> list[dict[str, Any]]:
    """Выбираем риск-отзывы: низкий рейтинг + без ответа."""

    def _key(r: dict[str, Any]) -> tuple[int, float]:
        rating = _rating_value(r) or 10.0
        unanswered = 0 if (r.get("answer") or "").strip() else 1
        return (-unanswered, rating)

    return sorted(reviews, key=_key)[:k]


def _format_risk_reviews(reviews: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, r in enumerate(reviews, 1):
        text = (r.get("text") or "").strip().replace("\n", " ")
        if len(text) > 600:
            text = text[:600] + "…"
        rating = _rating_value(r)
        rid = r.get("review_id")
        clinic = r.get("clinic_name") or ""
        doctor = r.get("doctor_name") or ""
        head = f"#{i}; review_id={rid}; оценка={rating}; клиника={clinic}"
        if doctor:
            head += f"; врач={doctor}"
        lines.append(f"{head}\nтекст: {text}")
    return "\n\n".join(lines)


def _summarize_response_status(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    answered = sum(1 for r in reviews if (r.get("answer") or "").strip())
    return {
        "total": len(reviews),
        "answered": answered,
        "unanswered": len(reviews) - answered,
        "answered_share_pct": round(100 * answered / len(reviews), 1) if reviews else None,
    }


def _build_report_system_prompt(entity_type: EntityType) -> str:
    label = {
        "clinic": "клиники",
        "service": "услуги",
        "category": "направления",
        "doctor": "врача",
    }.get(entity_type, "объекта")
    return (
        f"Ты — репутационный аналитик медицинских {label} (DocDoc / СберЗдоровье). "
        "По набору отзывов и релевантным фрагментам из RAG составь развёрнутый отчёт. "
        "Опирайся только на цитаты — не выдумывай факты, отсутствующие в данных. "
        "Ответ — только JSON без markdown по схеме: "
        "{"
        '"executive_summary": "<3-5 предложений для руководителя>", '
        '"what_patients_value": ["<пункт 1>", "<пункт 2>", ...], '
        '"top_complaints": ["<пункт 1>", ...], '
        '"service_improvements": ["<что починить в сервисе>", ...], '
        '"landing_page_gaps": ["<что добавить на страницу>", ...], '
        '"ad_angle": "<1-2 предложения, готовый угол для рекламы>", '
        '"target_audience": "<сигнал по ЦА из отзывов>", '
        '"risk_topics": ["<тема, на которую жалуются часто и без ответа>", ...]'
        "}. "
        "Каждый список — 3–6 пунктов. По-русски, кратко. "
        "В списках what_patients_value, top_complaints и risk_topics пиши только темы "
        "(например «компетентность врача», «долгое ожидание»), без цитат и без «Я обратился…»."
    )


def _build_report_user_prompt(
    *,
    entity_type: EntityType,
    bundle: EntityBundle,
    metrics: dict[str, Any],
    rag_snippets: list[dict[str, Any]],
    reviews_in_prompt: int,
) -> str:
    parts = [
        f"Тип объекта: {entity_type}",
        f"Объект: {bundle.entity_name}",
        f"Отзывов в выборке: {metrics['reviews_count']}",
    ]
    if metrics.get("avg_rating") is not None:
        parts.append(f"Средняя оценка: {metrics['avg_rating']}")
    if metrics.get("negative_share_pct") is not None:
        parts.append(f"Доля негатива: {metrics['negative_share_pct']}%")
    if metrics.get("unanswered_share_pct") is not None:
        parts.append(f"Без ответа клиники: {metrics['unanswered_share_pct']}%")
    if metrics.get("latest_review"):
        parts.append(f"Последний отзыв: {metrics['latest_review']}")

    out = ["\n".join(parts)]
    if rag_snippets:
        out.append(
            "Релевантные фрагменты RAG (отсортированы по релевантности):\n"
            + _format_rag_snippets(rag_snippets, max_chars=4000)
        )
    out.append(
        "Отзывы из выборки (расширенный сэмпл):\n"
        + _sample_reviews_text(bundle.reviews, max_items=reviews_in_prompt, max_chars=8000)
    )
    return "\n\n".join(out)


def _normalize_report(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {k: ("" if k in {"executive_summary", "ad_angle", "target_audience"} else []) for k in REPORT_FIELDS}
    coerced = _coerce_report_keys(_unwrap_report_dict(parsed) or parsed)
    out: dict[str, Any] = {}
    for k in REPORT_FIELDS:
        v = coerced.get(k)
        if k in {"executive_summary", "ad_angle", "target_audience"}:
            out[k] = str(v or "").strip()
        elif isinstance(v, list):
            out[k] = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            out[k] = [s.strip(" -•") for s in v.split("\n") if s.strip()]
        else:
            out[k] = []
    return out


_REPORT_NESTED_KEYS = ("report", "result", "data", "analysis", "ответ")

_REPORT_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "executive_summary": ("executive_summary", "summary", "сводка", "резюме"),
    "what_patients_value": (
        "what_patients_value",
        "what_patients_like",
        "strengths",
        "positives",
        "плюсы",
        "что_ценят",
    ),
    "top_complaints": ("top_complaints", "complaints", "weaknesses", "negatives", "жалобы"),
    "service_improvements": (
        "service_improvements",
        "improvements",
        "fixes",
        "что_починить",
    ),
    "landing_page_gaps": ("landing_page_gaps", "landing_gaps", "page_gaps", "на_страницу"),
    "ad_angle": ("ad_angle", "advertising_angle", "реклама", "ad"),
    "target_audience": ("target_audience", "audience", "целевая_аудитория", "ца"),
    "risk_topics": ("risk_topics", "risks", "risk", "риски"),
}


def _unwrap_report_dict(parsed: dict[str, Any]) -> dict[str, Any]:
    for nk in _REPORT_NESTED_KEYS:
        inner = parsed.get(nk)
        if isinstance(inner, dict) and (_coerce_report_keys(inner) or any(k in inner for k in REPORT_FIELDS)):
            return inner
    return parsed


def _coerce_report_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Поддержка русских/альтернативных ключей в JSON от LLM."""
    if not isinstance(raw, dict):
        return {}
    lower_index = {str(k).lower().replace(" ", "_"): k for k in raw.keys()}
    out: dict[str, Any] = {}
    for field in REPORT_FIELDS:
        val = None
        for alias in _REPORT_KEY_ALIASES.get(field, (field,)):
            if alias in raw:
                val = raw[alias]
                break
            lk = alias.lower().replace(" ", "_")
            orig = lower_index.get(lk)
            if orig is not None:
                val = raw[orig]
                break
        if val is not None:
            out[field] = val
    return out


def _report_is_empty(report: dict[str, Any]) -> bool:
    if not report:
        return True
    if (report.get("executive_summary") or "").strip():
        return False
    if (report.get("ad_angle") or "").strip() or (report.get("target_audience") or "").strip():
        return False
    for k in REPORT_FIELDS:
        if k in {"executive_summary", "ad_angle", "target_audience"}:
            continue
        if report.get(k):
            return False
    return True


_REVIEW_THEME_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("компетентность и квалификация врача", ("квалифиц", "компетент", "профессион", "грамотн", "опытн", "эксперт")),
    ("вежливость и внимательное отношение", ("вежлив", "внимательн", "доброжелатель", "приветлив", "терпелив")),
    ("понятные объяснения врача", ("объясн", "рассказал", "разъясн", "ответил на вопрос", "подробно")),
    ("качество результата лечения", ("результат", "вылечил", "помог", "удалил", "эффект", "всё удалили")),
    ("быстрое обслуживание", ("быстр", "оператив", "без очеред", "сразу", "не ждал")),
    ("удобная запись и расположение", ("удобн", "запис", "расположен", "легко добраться", "парков", "рядом")),
    ("адекватная цена", ("недорог", "приемлем", "адекватн.*цен", "соотношен.*цен")),
    ("чистота и комфорт в клинике", ("чист", "комфорт", "уют", "современн", "оборудован")),
    ("долгое ожидание и очереди", ("очеред", "ждал", "ожидан", "долго жд", "час")),
    ("высокая цена", ("дорог", "высок.*цен", "переплат", "завышен")),
    ("грубость и плохое отношение", ("груб", "хамств", "невежлив", "неуважен", "не понравилось отношение")),
    ("ошибки диагностики и лечения", ("ошиб", "неправильн", "не помог", "ухудш", "осложнен")),
    ("проблемы с записью и администрацией", ("не запис", "отмен", "перенос", "администрац", "регистратур")),
    ("скрытые доплаты и прозрачность цены", ("доплат", "навяз", "не предупред", "скрыт.*цен")),
    ("качество обслуживания в целом", ("на высшем", "отличн", "рекоменд", "доволен", "супер")),
]

_POSITIVE_THEME_LABELS = {
    "компетентность и квалификация врача",
    "вежливость и внимательное отношение",
    "понятные объяснения врача",
    "качество результата лечения",
    "быстрое обслуживание",
    "удобная запись и расположение",
    "адекватная цена",
    "чистота и комфорт в клинике",
    "качество обслуживания в целом",
}

_NEGATIVE_THEME_LABELS = {
    "долгое ожидание и очереди",
    "высокая цена",
    "грубость и плохое отношение",
    "ошибки диагностики и лечения",
    "проблемы с записью и администрацией",
    "скрытые доплаты и прозрачность цены",
}

_THEME_IMPROVEMENT_HINTS: dict[str, str] = {
    "долгое ожидание и очереди": "Сократить ожидание: слоты, регистратура, информирование о задержках",
    "высокая цена": "Пересмотреть прайс или явно объяснять стоимость до приёма",
    "грубость и плохое отношение": "Стандарты коммуникации персонала и контроль тона на приёме",
    "ошибки диагностики и лечения": "Разбор клинических кейсов и контроль качества",
    "проблемы с записью и администрацией": "Упростить запись и работу регистратуры",
    "скрытые доплаты и прозрачность цены": "Прозрачный прайс и предупреждение о доплатах заранее",
}

_THEME_LANDING_HINTS: dict[str, str] = {
    "долгое ожидание и очереди": "Указать среднее время ожидания и как записаться без очереди",
    "высокая цена": "Добавить цену или вилку цен и что входит в услугу",
    "проблемы с записью и администрацией": "Описать способы записи и политику отмены",
    "скрытые доплаты и прозрачность цены": "Расписать состав услуги и возможные доплаты",
}


def _extract_review_themes(
    reviews: list[dict[str, Any]],
    *,
    allowed_labels: set[str] | None = None,
    top_k: int = 5,
) -> list[str]:
    """Считает частые темы по ключевым словам, без цитирования отзывов."""
    counts: dict[str, int] = {}
    for review in reviews:
        text = (review.get("text") or "").strip().lower()
        if not text:
            continue
        seen_in_review: set[str] = set()
        for label, patterns in _REVIEW_THEME_PATTERNS:
            if allowed_labels is not None and label not in allowed_labels:
                continue
            if label in seen_in_review:
                continue
            for pat in patterns:
                if re.search(pat, text):
                    counts[label] = counts.get(label, 0) + 1
                    seen_in_review.add(label)
                    break
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [label for label, _ in ranked[:top_k]]


def _heuristic_report(
    bundle: EntityBundle,
    metrics: dict[str, Any],
    risk_reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Запасной отчёт без LLM — темы из отзывов и метрик, без цитат."""
    reviews = bundle.reviews or []
    positive = [r for r in reviews if (_rating_value(r) or 0) >= 8]
    negative = [r for r in reviews if (_rating_value(r) or 10) < 7]

    praises = _extract_review_themes(positive, allowed_labels=_POSITIVE_THEME_LABELS)
    complaints = _extract_review_themes(negative, allowed_labels=_NEGATIVE_THEME_LABELS)
    risk_topics = _extract_review_themes(risk_reviews, allowed_labels=_NEGATIVE_THEME_LABELS, top_k=4)

    parts = [f"«{bundle.entity_name}»: {metrics.get('reviews_count', 0)} отзывов в выборке."]
    if metrics.get("avg_rating") is not None:
        parts.append(f"Средняя оценка {metrics['avg_rating']}.")
    if metrics.get("negative_share_pct") is not None:
        parts.append(f"Доля негатива {metrics['negative_share_pct']}%.")
    if metrics.get("unanswered_share_pct") is not None:
        parts.append(f"Без ответа клиники {metrics['unanswered_share_pct']}%.")
    if praises:
        parts.append(f"Чаще всего ценят: {', '.join(praises[:3])}.")
    if complaints:
        parts.append(f"Чаще жалуются на: {', '.join(complaints[:3])}.")
    parts.append("Текст сформирован автоматически из отзывов (LLM недоступен или не вернул JSON).")

    improvements: list[str] = []
    unans = metrics.get("unanswered_share_pct")
    if isinstance(unans, (int, float)) and unans > 15:
        improvements.append("Отвечать на негативные отзывы — высокая доля без ответа")
    neg = metrics.get("negative_share_pct")
    if isinstance(neg, (int, float)) and neg > 20:
        improvements.append("Разобрать повторяющиеся жалобы и зафиксировать стандарты сервиса")
    for theme in complaints[:3]:
        hint = _THEME_IMPROVEMENT_HINTS.get(theme)
        improvements.append(hint or f"Устранить повторяющуюся тему: {theme}")

    landing = [
        "Указать длительность приёма и что входит в услугу",
        "Добавить цену или вилку цен и способ записи",
    ]
    for theme in complaints[:2]:
        hint = _THEME_LANDING_HINTS.get(theme)
        if hint and hint not in landing:
            landing.append(hint)

    if praises:
        ad = f"Акцент для рекламы: {praises[0]} — формулировка из повторяющихся похвал в отзывах."
    else:
        ad = "Сначала соберите больше положительных отзывов для точного рекламного угла."

    audience_bits: list[str] = []
    if any("цена" in t or "запис" in t for t in praises):
        audience_bits.append("удобство записи и цена")
    if any("врач" in t or "объясн" in t or "компетент" in t for t in praises):
        audience_bits.append("компетентность врача")
    if complaints:
        audience_bits.append("предсказуемость сервиса без сюрпризов")
    target_audience = (
        "Пациенты, которым важны " + ", ".join(audience_bits) + "."
        if audience_bits
        else "Пациенты, которым важны качество приёма и предсказуемость сервиса."
    )

    return _normalize_report(
        {
            "executive_summary": " ".join(parts),
            "what_patients_value": praises or ["Мало явно положительных отзывов в выборке"],
            "top_complaints": complaints or ["Явных негативных формулировок мало"],
            "service_improvements": improvements or ["Нужно больше отзывов для точных рекомендаций"],
            "landing_page_gaps": landing,
            "ad_angle": ad,
            "target_audience": target_audience,
            "risk_topics": risk_topics or complaints[:3] or ["Риск-темы не выделены"],
        }
    )


def _build_replies_system_prompt() -> str:
    return (
        "Ты помогаешь клинике писать вежливые и конкретные ответы на негативные отзывы пациентов. "
        "Ответ — JSON-массив без markdown, по одному элементу на каждый review_id из ввода. "
        "Схема элемента: "
        '{"review_id": <int>, "tone": "<empathetic|formal|apologetic>", '
        '"draft_reply": "<готовый текст ответа клиники, 2-5 предложений>", '
        '"talking_points": ["<что упомянуть>", ...]}. '
        "Не обещайте того, чего нельзя гарантировать. Не используйте имя пациента, если его нет в тексте. "
        "Не пишите шаблонных «спасибо за отзыв» без сути — отвечайте на конкретные жалобы."
    )


def _build_replies_user_prompt(reviews: list[dict[str, Any]], entity_name: str) -> str:
    return (
        f"Объект: {entity_name}\n\n"
        "Список риск-отзывов (нужны черновики ответов клиники):\n\n"
        + _format_risk_reviews(reviews)
    )


def _parse_replies_payload(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    start = t.find("[")
    if start < 0:
        parsed = parse_json_response(t)
        if isinstance(parsed, dict) and isinstance(parsed.get("replies"), list):
            return parsed["replies"]
        return []
    depth = 0
    for i, ch in enumerate(t[start:], start=start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    arr = json.loads(t[start : i + 1])
                    return arr if isinstance(arr, list) else []
                except json.JSONDecodeError:
                    return []
    return []


def analyze_entity_reputation(
    *,
    entity_type: EntityType = "clinic",
    entity: str,
    source_id: int | None = None,
    city_slug: str | None = None,
    crawl_path: str | None = None,
    data_source: ReputationDataSource = "auto",
    use_rag: bool = True,
    rag_top_k: int = 12,
    rag_query: str | None = None,
    rag_kinds: list[str] | None = None,
    reviews_in_prompt: int = 18,
    risk_reviews_count: int = 5,
    use_llm: bool = True,
    generate_reply_drafts: bool = True,
    chat_completion_fn: Any | None = None,
    rag_search_fn: Any | None = None,
) -> dict[str, Any]:
    """Глубокий разбор одной сущности."""
    if not entity or not entity.strip():
        return {"ok": False, "error": "empty_entity"}

    bundle, used_data_source = _resolve_bundle(
        entity_type=entity_type,
        entity=entity,
        source_id=source_id,
        city_slug=city_slug,
        crawl_path=crawl_path,
        data_source=data_source,
    )
    if bundle is None:
        suggestions = _suggest_similar(
            entity,
            _candidate_names(
                entity_type=entity_type,
                source_id=source_id,
                city_slug=city_slug,
                crawl_path=crawl_path,
            ),
        )
        hint_parts = [
            "Сущность не найдена ни в БД, ни в JSON.",
            "Проверьте написание (для clinic — clinic_alias или подстрока имени).",
        ]
        if suggestions:
            joined = ", ".join(f"«{s}»" for s in suggestions)
            hint_parts.append(f"Похожие в данных: {joined}.")
        else:
            hint_parts.append(
                "В выбранном источнике нет ни одной сущности этого типа — "
                "сначала сделайте Crawl и Ingest на /dashboard."
            )
        return {
            "ok": False,
            "error": "entity_not_found",
            "hint": " ".join(hint_parts),
            "suggestions": suggestions,
        }

    metrics = _detailed_metrics(bundle.reviews)
    response_status = _summarize_response_status(bundle.reviews)
    risk_reviews_raw = _pick_risk_reviews(bundle.reviews, k=risk_reviews_count)
    risk_reviews_view = [
        {
            "review_id": r.get("review_id"),
            "rating": _rating_value(r),
            "answered": bool((r.get("answer") or "").strip()),
            "text": (r.get("text") or "")[:500],
            "doctor_name": r.get("doctor_name"),
            "clinic_name": r.get("clinic_name"),
            "service_name": r.get("service_name"),
            "source_page_url": r.get("source_page_url"),
        }
        for r in risk_reviews_raw
    ]

    rag_snippets: list[dict[str, Any]] = []
    rag_negative_snippets: list[dict[str, Any]] = []
    if use_rag and rag_top_k > 0:
        rag_snippets = _collect_rag_snippets(
            bundle,
            entity_type,
            llm_keys=list(REPORT_FIELDS),
            rag_top_k=rag_top_k,
            rag_query_override=rag_query,
            city_slug=city_slug,
            source_id=source_id,
            rag_kinds=rag_kinds,
            rag_search_fn=rag_search_fn,
        )
        # отдельный «негатив-запрос» для тематики жалоб
        rag_negative_snippets = _collect_rag_snippets(
            bundle,
            entity_type,
            llm_keys=["top_complaints", "risk_topics", "service_improvements"],
            rag_top_k=max(3, rag_top_k // 2),
            rag_query_override=(
                "негатив, жалобы, плохой опыт, отказ, грубость, "
                "ожидание, цена, качество, ошибка"
            ),
            city_slug=city_slug,
            source_id=source_id,
            rag_kinds=rag_kinds or ["review"],
            rag_search_fn=rag_search_fn,
        )

    # объединяем уникально по chunk_id, негативные приоритетно
    seen: set[Any] = set()
    rag_combined: list[dict[str, Any]] = []
    for s in [*rag_negative_snippets, *rag_snippets]:
        cid = s.get("chunk_id")
        key = cid if cid is not None else (s.get("title"), (s.get("snippet") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        rag_combined.append(s)

    report: dict[str, Any] = _normalize_report({})
    report_source = "empty"
    llm_error: str | None = None
    reply_drafts: list[dict[str, Any]] = []
    llm_used = False
    chat_fn = chat_completion_fn or chat_completion

    if use_llm and bundle.reviews:
        try:
            raw = chat_fn(
                system_prompt=_build_report_system_prompt(entity_type),
                user_prompt=_build_report_user_prompt(
                    entity_type=entity_type,
                    bundle=bundle,
                    metrics=metrics,
                    rag_snippets=rag_combined,
                    reviews_in_prompt=reviews_in_prompt,
                ),
                temperature=0.15,
                max_tokens=1800,
            )
            llm_used = True
            parsed = parse_json_response(raw)
            if parsed is None:
                llm_error = "llm_response_not_json"
            else:
                report = _normalize_report(parsed)
                if not _report_is_empty(report):
                    report_source = "llm"
                else:
                    llm_error = "llm_empty_report"
        except Exception as exc:
            llm_error = str(exc)
            log.warning("reputation report LLM failed: %s", exc)

        if generate_reply_drafts and risk_reviews_raw:
            try:
                raw_replies = chat_fn(
                    system_prompt=_build_replies_system_prompt(),
                    user_prompt=_build_replies_user_prompt(risk_reviews_raw, bundle.entity_name),
                    temperature=0.2,
                    max_tokens=1200,
                )
                reply_drafts = _parse_replies_payload(raw_replies)
            except Exception as exc:
                log.warning("reputation reply drafts LLM failed: %s", exc)

    if _report_is_empty(report):
        if bundle.reviews:
            report = _heuristic_report(bundle, metrics, risk_reviews_raw)
            report_source = "heuristic"
        else:
            report = _normalize_report(
                {
                    "executive_summary": (
                        f"По объекту «{bundle.entity_name}» не найдено отзывов в выбранном источнике данных."
                    ),
                }
            )

    return {
        "ok": True,
        "entity_type": entity_type,
        "entity_id": bundle.entity_id,
        "entity_name": bundle.entity_name,
        "data_source": used_data_source,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "metrics": metrics,
        "response_status": response_status,
        "risk_reviews": risk_reviews_view,
        "report": report,
        "report_source": report_source,
        "llm_error": llm_error,
        "reply_drafts": reply_drafts,
        "rag": {
            "used": bool(use_rag and rag_top_k > 0),
            "top_k": rag_top_k,
            "snippets_total": len(rag_combined),
            "snippets_negative": len(rag_negative_snippets),
            "default_query": _entity_rag_query(entity_type, bundle.entity_name, list(REPORT_FIELDS), rag_query),
            "kinds": rag_kinds,
        },
        "llm_used": llm_used and report_source == "llm",
        "notes": (
            "report — структурированный отчёт. reply_drafts — черновики ответов клиники "
            "на риск-отзывы (низкий рейтинг + без ответа). RAG-фрагменты подмешиваются в "
            "промпт, если включён use_rag и индекс docdoc_chunks построен. "
            "report_source=heuristic — автоматическая выжимка без LLM."
        ),
    }


def _build_compare_system_prompt(entity_type: EntityType | None) -> str:
    if entity_type is None:
        intro = (
            "Ты — репутационный аналитик. Сравни объекты разных типов "
            "(клиники, услуги, направления, врачи) «бок о бок» по отзывам и RAG-фрагментам. "
            "Учитывай, что объекты разнородные: например, клиника даёт обзор всего сервиса, "
            "а услуга — конкретного воркфлоу. "
        )
    else:
        label = {
            "clinic": "клиники",
            "service": "услуги",
            "category": "направления",
            "doctor": "врачей",
        }.get(entity_type, "объекты")
        intro = f"Ты — репутационный аналитик. Сравни {label} «бок о бок» по отзывам и RAG-фрагментам. "
    return (
        intro
        + "Опирайся только на цитаты, не выдумывай факты. "
        "Ответ — только JSON без markdown по схеме: "
        "{"
        '"summary": "<3-5 предложений общего вывода: кто на чём сильнее>", '
        '"per_entity": ['
        "{"
        '"entity_id": "<id из ввода>", '
        '"entity_name": "<имя>", '
        '"strengths": ["<пункт>", ...], '
        '"weaknesses": ["<пункт>", ...], '
        '"unique_selling_points": ["<уникальные плюсы>", ...]'
        "}"
        "], "
        '"shared_complaints": ["<жалобы, общие для всех>", ...], '
        '"ad_angle": "<кому какой угол подходит лучше>", '
        '"winner_by_metric": {'
        '"avg_rating": "<entity_id или null>", '
        '"answer_rate": "<entity_id или null>", '
        '"review_volume": "<entity_id или null>"'
        "}"
        "}. "
        "Каждый список — 2–5 пунктов. По-русски, кратко. "
        "В strengths, weaknesses и shared_complaints пиши только темы "
        "(например «компетентность врача», «долгое ожидание»), без цитат из отзывов. "
        "В winner_by_metric указывай entity_id из ввода, не slug и не русское имя."
    )


def _build_compare_user_prompt(
    *,
    entity_type: EntityType | None,
    items: list[dict[str, Any]],
    reviews_per_entity: int,
    scope_pairs: list[tuple[str, str]] | None = None,
) -> str:
    type_label = entity_type if entity_type else "разнотипные"
    parts = [f"Тип объектов: {type_label}"]
    if scope_pairs:
        parts.append(
            "Контекст сравнения (scope): "
            + ", ".join(f"{k}={v!r}" for k, v in scope_pairs)
            + ". Сравнивай объекты строго в этом контексте, делай USP точечными — годными для лендинга."
        )
    parts.append("Сравниваем следующие объекты:")
    for it in items:
        bundle: EntityBundle = it["bundle"]
        m = it["metrics"]
        rs = it["response_status"]
        rag_snips = it.get("rag_snippets") or []
        item_type = it.get("entity_type") or entity_type or "?"
        head = (
            f"\n### entity_id={bundle.entity_id}\n"
            f"Тип: {item_type}\n"
            f"Название: {bundle.entity_name}\n"
            f"Отзывов: {m['reviews_count']}; "
            f"avg={m.get('avg_rating')}; "
            f"negative_share={m.get('negative_share_pct')}%; "
            f"unanswered={rs.get('unanswered')}/{rs.get('total')}; "
            f"answer_rate={rs.get('answered_share_pct')}%"
        )
        body_parts = [head]
        if rag_snips:
            body_parts.append(
                "RAG-фрагменты:\n" + _format_rag_snippets(rag_snips, max_chars=2000)
            )
        body_parts.append(
            "Сэмпл отзывов:\n"
            + _sample_reviews_text(bundle.reviews, max_items=reviews_per_entity, max_chars=4000)
        )
        parts.append("\n\n".join(body_parts))
    return "\n".join(parts)


def _normalize_compare(parsed: Any, entity_ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "summary": "",
        "per_entity": [],
        "shared_complaints": [],
        "ad_angle": "",
        "winner_by_metric": {"avg_rating": None, "answer_rate": None, "review_volume": None},
    }
    if not isinstance(parsed, dict):
        out["per_entity"] = [
            {"entity_id": eid, "entity_name": "", "strengths": [], "weaknesses": [], "unique_selling_points": []}
            for eid in entity_ids
        ]
        return out
    out["summary"] = str(parsed.get("summary") or "").strip()
    out["ad_angle"] = str(parsed.get("ad_angle") or "").strip()
    sc = parsed.get("shared_complaints")
    out["shared_complaints"] = (
        [str(x).strip() for x in sc if str(x).strip()] if isinstance(sc, list) else []
    )
    wbm = parsed.get("winner_by_metric") or {}
    if isinstance(wbm, dict):
        for k in ("avg_rating", "answer_rate", "review_volume"):
            v = wbm.get(k)
            out["winner_by_metric"][k] = (
                str(v).strip() if isinstance(v, str) and v.strip() and v not in {"null", "None"} else None
            )
    pe = parsed.get("per_entity") or []
    pe_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(pe, list):
        for item in pe:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("entity_id") or "").strip()
            if not eid:
                continue
            pe_by_id[eid] = item
    out_pe = []
    for eid in entity_ids:
        item = pe_by_id.get(eid) or {}
        out_pe.append(
            {
                "entity_id": eid,
                "entity_name": str(item.get("entity_name") or "").strip(),
                "strengths": [str(x).strip() for x in (item.get("strengths") or []) if str(x).strip()],
                "weaknesses": [str(x).strip() for x in (item.get("weaknesses") or []) if str(x).strip()],
                "unique_selling_points": [
                    str(x).strip() for x in (item.get("unique_selling_points") or []) if str(x).strip()
                ],
            }
        )
    out["per_entity"] = out_pe
    return out


def _entity_id_for(item: dict[str, Any]) -> str:
    """Универсально достать entity_id: поддерживает и dict-форму, и обёртку с bundle."""
    if "entity_id" in item:
        return str(item["entity_id"])
    bundle = item.get("bundle")
    return getattr(bundle, "entity_id", "") if bundle is not None else ""


def _winners_from_metrics(per_entity: list[dict[str, Any]]) -> dict[str, str | None]:
    """Помогает LLM: считаем победителей по метрикам, чтобы при ошибке LLM был fallback."""
    if not per_entity:
        return {"avg_rating": None, "answer_rate": None, "review_volume": None}

    def _best(key_fn):
        ranked = [it for it in per_entity if key_fn(it) is not None]
        if not ranked:
            return None
        ranked.sort(key=key_fn, reverse=True)
        return _entity_id_for(ranked[0]) or None

    return {
        "avg_rating": _best(lambda it: (it["metrics"] or {}).get("avg_rating")),
        "answer_rate": _best(lambda it: (it["response_status"] or {}).get("answered_share_pct")),
        "review_volume": _best(lambda it: (it["metrics"] or {}).get("reviews_count")),
    }


def _entity_name_map(items: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items:
        bundle = it.get("bundle")
        if bundle is None:
            continue
        eid = getattr(bundle, "entity_id", "") or ""
        name = (getattr(bundle, "entity_name", "") or "").strip()
        if eid:
            out[eid] = name or eid
    return out


def _resolve_winner_display(
    winners: dict[str, str | None],
    name_map: dict[str, str],
) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for k, v in winners.items():
        if not v:
            out[k] = None
            continue
        out[k] = name_map.get(v, v)
    return out


def _compare_block_is_empty(compare_block: dict[str, Any]) -> bool:
    if (compare_block.get("summary") or "").strip():
        return False
    for pe in compare_block.get("per_entity") or []:
        if pe.get("strengths") or pe.get("weaknesses"):
            return False
    if compare_block.get("shared_complaints"):
        return False
    if (compare_block.get("ad_angle") or "").strip():
        return False
    return True


def _heuristic_compare(
    items: list[dict[str, Any]],
    metrics_winners: dict[str, str | None],
) -> dict[str, Any]:
    """Запасной compare без LLM — темы по каждой сущности и сводка по метрикам."""
    name_by_id = _entity_name_map(items)
    entity_ids = list(name_by_id.keys())
    per_entity_out: list[dict[str, Any]] = []
    negative_theme_sets: list[set[str]] = []

    for it in items:
        bundle: EntityBundle = it["bundle"]
        eid = bundle.entity_id
        name = name_by_id.get(eid, bundle.entity_name or eid)
        reviews = bundle.reviews or []
        positive = [r for r in reviews if (_rating_value(r) or 0) >= 8]
        negative = [r for r in reviews if (_rating_value(r) or 10) < 7]
        strengths = _extract_review_themes(positive, allowed_labels=_POSITIVE_THEME_LABELS, top_k=4)
        weaknesses = _extract_review_themes(negative, allowed_labels=_NEGATIVE_THEME_LABELS, top_k=4)
        negative_theme_sets.append(
            set(_extract_review_themes(negative, allowed_labels=_NEGATIVE_THEME_LABELS, top_k=6))
        )
        per_entity_out.append(
            {
                "entity_id": eid,
                "entity_name": name,
                "strengths": strengths or ["Мало явно положительных отзывов в выборке"],
                "weaknesses": weaknesses or ["Явных жалоб мало"],
                "unique_selling_points": [],
            }
        )

    strength_by_id = {pe["entity_id"]: set(pe["strengths"]) for pe in per_entity_out}
    for pe in per_entity_out:
        others: set[str] = set()
        for oid, themes in strength_by_id.items():
            if oid != pe["entity_id"]:
                others |= themes
        unique = [
            s
            for s in pe["strengths"]
            if s not in others and s != "Мало явно положительных отзывов в выборке"
        ]
        pe["unique_selling_points"] = unique[:3] or pe["strengths"][:2]

    shared: list[str] = []
    if len(negative_theme_sets) >= 2:
        common = negative_theme_sets[0].copy()
        for theme_set in negative_theme_sets[1:]:
            common &= theme_set
        shared = sorted(common)
        if not shared:
            counts: Counter[str] = Counter()
            for theme_set in negative_theme_sets:
                for theme in theme_set:
                    counts[theme] += 1
            threshold = max(2, (len(items) + 1) // 2)
            shared = [theme for theme, count in counts.most_common() if count >= threshold][:4]

    summary_parts: list[str] = []
    for it in items:
        bundle: EntityBundle = it["bundle"]
        m = it["metrics"]
        rs = it["response_status"]
        summary_parts.append(
            f"«{bundle.entity_name}»: {m.get('reviews_count', 0)} отз., "
            f"рейтинг {m.get('avg_rating', '—')}, "
            f"негатив {m.get('negative_share_pct', '—')}%, "
            f"ответы {rs.get('answered_share_pct', '—')}%."
        )
    winner_bits: list[str] = []
    winner_labels = {
        "avg_rating": "рейтинг",
        "answer_rate": "ответы на отзывы",
        "review_volume": "число отзывов",
    }
    for key, label in winner_labels.items():
        wid = metrics_winners.get(key)
        if wid and wid in name_by_id:
            winner_bits.append(f"лучший {label} — «{name_by_id[wid]}»")
    summary = " ".join(summary_parts)
    if winner_bits:
        summary += " По метрикам: " + "; ".join(winner_bits) + "."
    summary += " Текст сформирован автоматически из отзывов (LLM недоступен или не вернул JSON)."

    ad_parts = [
        f"«{pe['entity_name']}» — акцент на {pe['strengths'][0]}"
        for pe in per_entity_out
        if pe["strengths"] and pe["strengths"][0] != "Мало явно положительных отзывов в выборке"
    ]
    ad_angle = (
        "; ".join(ad_parts) + "."
        if ad_parts
        else "Нужно больше отзывов для точных рекламных углов."
    )

    return _normalize_compare(
        {
            "summary": summary,
            "per_entity": per_entity_out,
            "shared_complaints": shared or ["Общих повторяющихся жалоб не выделено"],
            "ad_angle": ad_angle,
            "winner_by_metric": metrics_winners,
        },
        entity_ids,
    )


def _finalize_compare_block(
    compare_block: dict[str, Any],
    items: list[dict[str, Any]],
    metrics_winners: dict[str, str | None],
) -> dict[str, Any]:
    """Подставляет русские имена в победителей и заполняет пустые entity_name."""
    name_map = _entity_name_map(items)
    for pe in compare_block.get("per_entity") or []:
        eid = pe.get("entity_id") or ""
        if not pe.get("entity_name"):
            pe["entity_name"] = name_map.get(eid, eid)
    winners = dict(compare_block.get("winner_by_metric") or {})
    for key, value in metrics_winners.items():
        if winners.get(key) is None:
            winners[key] = value
    compare_block["winner_by_metric"] = _resolve_winner_display(winners, name_map)
    return compare_block


_SCOPE_FIELDS_BY_KIND: dict[str, tuple[str, ...]] = {
    "service": ("service_name", "parent_service_name", "service_url"),
    "category": ("parent_service_name", "category_direction_title", "service_url"),
    "clinic": ("clinic_name", "clinic_alias"),
    "doctor": ("doctor_name", "doctor_external_id", "doctor_id"),
}


def _scope_keys_to_kinds(scope: dict[str, str | None]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in (scope or {}).items():
        if not v or not isinstance(v, str):
            continue
        kind = k.removeprefix("scope_") if k.startswith("scope_") else k
        if kind in _SCOPE_FIELDS_BY_KIND:
            out.append((kind, v.strip()))
    return out


def _review_matches_scope(review: dict[str, Any], scope_pairs: list[tuple[str, str]]) -> bool:
    for kind, needle in scope_pairs:
        haystack_parts: list[str] = []
        for f in _SCOPE_FIELDS_BY_KIND.get(kind, ()):
            v = review.get(f)
            if v is not None:
                haystack_parts.append(str(v))
        if not _matches_entities(" ".join(haystack_parts), [needle]):
            return False
    return True


def _apply_scope_to_bundle(bundle: EntityBundle, scope_pairs: list[tuple[str, str]]) -> EntityBundle:
    """Возвращает новый EntityBundle с отзывами, прошедшими все scope-фильтры."""
    if not scope_pairs:
        return bundle
    filtered = [r for r in bundle.reviews if _review_matches_scope(r, scope_pairs)]
    return EntityBundle(
        entity_id=bundle.entity_id,
        entity_name=bundle.entity_name,
        reviews=filtered,
        filters=bundle.filters,
    )


def _normalize_compare_entities(
    entities: list[Any],
    entity_type: EntityType | None,
) -> tuple[list[tuple[EntityType, str]], dict[str, Any] | None]:
    """Превращает список (str | dict | spec-like) в [(type, value), ...].

    Если хоть один элемент — dict/spec, режим mixed; type каждой сущности обязателен.
    Если все — строки, требуется общий entity_type.
    """
    specs: list[tuple[EntityType, str]] = []
    for e in entities or []:
        if isinstance(e, str):
            value = e.strip()
            if not value:
                continue
            if not entity_type:
                return [], {
                    "ok": False,
                    "error": "entity_type_required_for_string_entities",
                    "hint": "Передайте entity_type или используйте формат [{type, value}, ...]",
                }
            specs.append((entity_type, value))
        elif isinstance(e, dict):
            t = (e.get("type") or "").strip().lower()
            v = (e.get("value") or "").strip()
            if not v:
                continue
            if t not in {"clinic", "service", "category", "doctor"}:
                if not entity_type:
                    return [], {"ok": False, "error": "invalid_entity_spec_type", "hint": f"unknown type={t!r}"}
                t = entity_type
            specs.append((t, v))  # type: ignore[arg-type]
        else:
            t = getattr(e, "type", None)
            v = getattr(e, "value", None)
            if not v:
                continue
            t = (t or entity_type or "").strip().lower() if isinstance(t, str) else (entity_type or "")
            if t not in {"clinic", "service", "category", "doctor"}:
                return [], {"ok": False, "error": "invalid_entity_spec_type"}
            specs.append((t, str(v).strip()))  # type: ignore[arg-type]
    return specs, None


def compare_entities(
    *,
    entity_type: EntityType | None = "clinic",
    entities: list[Any],
    source_id: int | None = None,
    city_slug: str | None = None,
    crawl_path: str | None = None,
    data_source: ReputationDataSource = "auto",
    use_rag: bool = True,
    rag_top_k: int = 6,
    rag_query: str | None = None,
    rag_kinds: list[str] | None = None,
    reviews_per_entity: int = 10,
    use_llm: bool = True,
    scope: dict[str, str | None] | None = None,
    chat_completion_fn: Any | None = None,
    rag_search_fn: Any | None = None,
) -> dict[str, Any]:
    """Сравнить 2–6 сущностей. Поддерживает разнотипные entities ([{type,value}]).

    scope ограничивает сравнение конкретным контекстом, например:
        scope={"service": "Тонзиллор"} — для каждой клиники оставит только отзывы по этой услуге.
    Поддерживаемые ключи: service, category, clinic, doctor (с/без префикса scope_).
    """
    if not entities or len(entities) < 2:
        return {"ok": False, "error": "need_at_least_two_entities"}
    if len(entities) > 6:
        return {"ok": False, "error": "too_many_entities", "hint": "максимум 6"}

    specs, err = _normalize_compare_entities(entities, entity_type)
    if err is not None:
        return err
    if len(specs) < 2:
        return {"ok": False, "error": "need_at_least_two_entities"}

    types_in_use = {t for t, _ in specs}
    is_mixed = len(types_in_use) > 1
    common_type: EntityType | None = next(iter(types_in_use)) if not is_mixed else None  # type: ignore[assignment]

    scope_pairs = _scope_keys_to_kinds(scope or {})

    items: list[dict[str, Any]] = []
    not_found: list[dict[str, str]] = []
    scope_empty: list[dict[str, str]] = []
    used_source: str | None = None

    for etype, value in specs:
        bundle, src = _resolve_bundle(
            entity_type=etype,
            entity=value,
            source_id=source_id,
            city_slug=city_slug,
            crawl_path=crawl_path,
            data_source=data_source,
        )
        if bundle is None:
            not_found.append({"type": etype, "value": value})
            continue
        used_source = used_source or src
        original_review_count = len(bundle.reviews)
        if scope_pairs:
            bundle = _apply_scope_to_bundle(bundle, scope_pairs)
            if not bundle.reviews:
                scope_empty.append({"type": etype, "value": value})
                continue
        m = _detailed_metrics(bundle.reviews)
        m["reviews_before_scope"] = original_review_count if scope_pairs else m["reviews_count"]
        rs = _summarize_response_status(bundle.reviews)
        rag_snips: list[dict[str, Any]] = []
        if use_rag and rag_top_k > 0:
            rag_snips = _collect_rag_snippets(
                bundle,
                etype,
                llm_keys=list(COMPARE_FIELDS),
                rag_top_k=rag_top_k,
                rag_query_override=rag_query,
                city_slug=city_slug,
                source_id=source_id,
                rag_kinds=rag_kinds,
                rag_search_fn=rag_search_fn,
            )
        items.append(
            {
                "bundle": bundle,
                "entity_type": etype,
                "metrics": m,
                "response_status": rs,
                "rag_snippets": rag_snips,
            }
        )

    if len(items) < 2:
        err = "not_enough_matches"
        if scope_pairs and scope_empty and not not_found:
            err = "scope_filtered_out"
        suggestions: dict[str, list[str]] = {}
        if not_found:
            for nf in not_found:
                t = nf.get("type") or (entity_type or "")
                v = nf.get("value") or ""
                if not v:
                    continue
                cands = _suggest_similar(
                    v,
                    _candidate_names(
                        entity_type=t,  # type: ignore[arg-type]
                        source_id=source_id,
                        city_slug=city_slug,
                        crawl_path=crawl_path,
                    ),
                )
                if cands:
                    suggestions[v] = cands
        if err == "scope_filtered_out":
            hint = (
                "Слишком жёсткий scope: после фильтрации не осталось отзывов. "
                "Попробуйте более общее название услуги/клиники."
            )
        else:
            parts = ["Найдено меньше двух сущностей с отзывами."]
            if suggestions:
                for v, cands in suggestions.items():
                    parts.append(f"Похожие на «{v}»: " + ", ".join(f"«{c}»" for c in cands) + ".")
            else:
                parts.append("Проверьте написание или сделайте Crawl/Ingest на /dashboard.")
            hint = " ".join(parts)
        return {
            "ok": False,
            "error": err,
            "found_entities": [it["bundle"].entity_id for it in items],
            "not_found": not_found,
            "scope_empty": scope_empty,
            "suggestions": suggestions,
            "hint": hint,
        }

    entity_ids = [it["bundle"].entity_id for it in items]
    metrics_only_winners = _winners_from_metrics(items)

    compare_block: dict[str, Any] = _normalize_compare({}, entity_ids)
    compare_source = "heuristic"
    llm_used = False
    llm_error: str | None = None
    chat_fn = chat_completion_fn or chat_completion

    if use_llm:
        try:
            raw = chat_fn(
                system_prompt=_build_compare_system_prompt(common_type),
                user_prompt=_build_compare_user_prompt(
                    entity_type=common_type,
                    items=items,
                    reviews_per_entity=reviews_per_entity,
                    scope_pairs=scope_pairs or None,
                ),
                temperature=0.15,
                max_tokens=1500,
            )
            llm_used = True
            candidate = _normalize_compare(parse_json_response(raw), entity_ids)
            if not _compare_block_is_empty(candidate):
                compare_block = candidate
                compare_source = "llm"
            else:
                llm_error = "empty_llm_response"
        except Exception as exc:
            log.warning("compare LLM failed: %s", exc)
            llm_error = str(exc)

    if compare_source == "heuristic":
        compare_block = _heuristic_compare(items, metrics_only_winners)

    compare_block = _finalize_compare_block(compare_block, items, metrics_only_winners)

    return {
        "ok": True,
        "entity_type": common_type,
        "is_mixed": is_mixed,
        "data_source": used_source,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "items": [
            {
                "entity_id": it["bundle"].entity_id,
                "entity_name": it["bundle"].entity_name,
                "entity_type": it["entity_type"],
                "metrics": it["metrics"],
                "response_status": it["response_status"],
                "rag_snippets_count": len(it["rag_snippets"]),
            }
            for it in items
        ],
        "compare": compare_block,
        "compare_source": compare_source,
        "metrics_winners": metrics_only_winners,
        "not_found": not_found,
        "scope": ({k: v for k, v in scope_pairs} if scope_pairs else None),
        "scope_empty": scope_empty,
        "llm_used": llm_used,
        "llm_error": llm_error,
    }

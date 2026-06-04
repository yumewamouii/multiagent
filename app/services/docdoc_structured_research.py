"""
Структурный ресёрч по списку объектов (клиники, услуги, категории) и полям из отзывов DocDoc.

Пример: «10 клиник по полям: число отзывов, средняя оценка, жалобы, ЦА, угол для рекламы» → таблица.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.llm import chat_completion, parse_json_response
from app.models import orm as models
from app.services.docdoc_ingest import iter_reviews_deduped

log = logging.getLogger(__name__)

EntityType = Literal["clinic", "service", "category", "doctor"]
FieldKind = Literal["metric", "llm"]

METRIC_FIELD_LABELS: dict[str, str] = {
    "reviews_count": "Число отзывов",
    "avg_rating": "Средняя оценка",
    "negative_share_pct": "Доля негативных, %",
    "unanswered_share_pct": "Без ответа клиники, %",
    "doctors_mentioned": "Врачей в отзывах",
    "latest_review": "Последний отзыв",
}

LLM_FIELD_LABELS: dict[str, str] = {
    "top_praises": "Что хвалят",
    "top_complaints": "Частые жалобы",
    "target_audience": "Целевая аудитория (сигнал)",
    "service_improvements": "Что улучшить в сервисе",
    "landing_page_gaps": "Чего не хватает на странице",
    "ad_angle": "Угол для рекламы",
    "executive_summary": "Сводка для руководителя",
}

PRESET_FIELDS: dict[str, list[str]] = {
    "clinic_competitors": [
        "reviews_count",
        "avg_rating",
        "negative_share_pct",
        "unanswered_share_pct",
        "top_praises",
        "top_complaints",
        "target_audience",
        "ad_angle",
    ],
    "service_competitors": [
        "reviews_count",
        "avg_rating",
        "negative_share_pct",
        "top_praises",
        "top_complaints",
        "landing_page_gaps",
        "ad_angle",
    ],
    "category_landscape": [
        "reviews_count",
        "avg_rating",
        "top_complaints",
        "target_audience",
        "service_improvements",
        "executive_summary",
    ],
}


@dataclass
class ResearchField:
    key: str
    kind: FieldKind
    label: str


@dataclass
class EntityBundle:
    entity_id: str
    entity_name: str
    reviews: list[dict[str, Any]]
    filters: dict[str, Any] = field(default_factory=dict)


def _filters_for_entity(review: dict[str, Any], entity_type: EntityType) -> dict[str, Any]:
    if entity_type == "clinic":
        alias = (review.get("clinic_alias") or "").strip()
        if alias:
            return {"clinic_alias": alias}
        name = (review.get("clinic_name") or "").strip()
        return {"clinic_name_like": name} if name else {}
    if entity_type == "service":
        out: dict[str, Any] = {}
        if review.get("service_name"):
            out["service_name"] = str(review["service_name"]).strip()
        if review.get("parent_service_name"):
            out["parent_service_name"] = str(review["parent_service_name"]).strip()
        return out
    if entity_type == "category":
        parent = (review.get("parent_service_name") or review.get("category_direction_title") or "").strip()
        return {"parent_service_name": parent} if parent else {}
    if entity_type == "doctor":
        did = review.get("doctor_id") or review.get("doctor_external_id")
        try:
            did_int = int(did) if did is not None else None
        except (TypeError, ValueError):
            did_int = None
        if did_int is not None and did_int > 0:
            return {"doctor_external_id": did_int}
        name = (review.get("doctor_name") or "").strip()
        return {"doctor_name_like": name} if name else {}
    return {}


def resolve_fields(
    field_keys: list[str] | None,
    *,
    preset: str | None = None,
) -> list[ResearchField]:
    keys: list[str] = []
    if preset and preset in PRESET_FIELDS:
        keys = list(PRESET_FIELDS[preset])
    if field_keys:
        keys = list(field_keys)
    if not keys:
        keys = list(PRESET_FIELDS["clinic_competitors"])

    out: list[ResearchField] = []
    for key in keys:
        if key in METRIC_FIELD_LABELS:
            out.append(ResearchField(key=key, kind="metric", label=METRIC_FIELD_LABELS[key]))
        elif key in LLM_FIELD_LABELS:
            out.append(ResearchField(key=key, kind="llm", label=LLM_FIELD_LABELS[key]))
        else:
            out.append(ResearchField(key=key, kind="llm", label=key))
    return out


def _normalize_match_text(text: str) -> str:
    t = (text or "").strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", t)


def _matches_entities(haystack: str, needles: list[str]) -> bool:
    hay = _normalize_match_text(haystack)
    for raw in needles:
        needle = _normalize_match_text(raw)
        if not needle:
            continue
        if needle in hay:
            return True
        # латиница в URL: tonzillor ↔ тонзиллор
        if needle.isascii() and needle.replace(" ", "") in hay.replace(" ", ""):
            return True
    return False


def _entity_key_from_review(review: dict[str, Any], entity_type: EntityType) -> tuple[str, str]:
    if entity_type == "clinic":
        name = (review.get("clinic_name") or review.get("clinic_alias") or "").strip()
        eid = (review.get("clinic_alias") or name or "unknown").strip()
    elif entity_type == "service":
        name = (review.get("service_name") or "").strip()
        parent = (review.get("parent_service_name") or "").strip()
        eid = f"{parent}/{name}" if parent and name else (name or "unknown")
        if parent and name:
            name = f"{parent} — {name}"
    elif entity_type == "category":
        name = (review.get("parent_service_name") or review.get("category_direction_title") or "").strip()
        eid = name or "unknown"
    else:  # doctor
        name = (review.get("doctor_name") or "").strip()
        eid = str(review.get("doctor_id") or name or "unknown")
    if not name:
        name = eid
    return eid, name


def _rating_value(review: dict[str, Any]) -> float | None:
    for key in ("rating_value", "rating_clinic"):
        v = review.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def compute_metrics(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    if not reviews:
        return {
            "reviews_count": 0,
            "avg_rating": None,
            "negative_share_pct": None,
            "unanswered_share_pct": None,
            "doctors_mentioned": 0,
            "latest_review": None,
        }
    ratings = [r for r in (_rating_value(x) for x in reviews) if r is not None]
    negative = sum(1 for r in ratings if r < 7)
    unanswered = sum(1 for x in reviews if not (x.get("answer") or "").strip())
    doctors = {x.get("doctor_id") or x.get("doctor_name") for x in reviews if x.get("doctor_name")}
    dates: list[str] = []
    for x in reviews:
        raw = x.get("created")
        if raw:
            dates.append(str(raw)[:10])
    latest = max(dates) if dates else None
    return {
        "reviews_count": len(reviews),
        "avg_rating": round(mean(ratings), 1) if ratings else None,
        "negative_share_pct": round(100 * negative / len(ratings), 1) if ratings else None,
        "unanswered_share_pct": round(100 * unanswered / len(reviews), 1),
        "doctors_mentioned": len(doctors),
        "latest_review": latest,
    }


DEFAULT_RAG_QUERY_BY_ENTITY: dict[str, str] = {
    "clinic": "жалобы пациентов, ожидание, отношение администраторов, цены, что хвалят и что раздражает",
    "service": "что раздражает пациентов в этой услуге, цена, подготовка, длительность, результат, чего не хватает на странице",
    "category": "повторяющиеся жалобы и похвала по направлению, типичные ожидания пациентов",
    "doctor": "квалификация, манера общения, объяснения, отношение к пациенту, отзывы пациентов",
}


def _entity_rag_query(
    entity_type: EntityType,
    entity_name: str,
    llm_keys: list[str],
    override: str | None,
) -> str:
    if override:
        return override
    base = DEFAULT_RAG_QUERY_BY_ENTITY.get(entity_type, DEFAULT_RAG_QUERY_BY_ENTITY["service"])
    field_hint_parts: list[str] = []
    if "top_complaints" in llm_keys:
        field_hint_parts.append("жалобы")
    if "top_praises" in llm_keys:
        field_hint_parts.append("похвала")
    if "target_audience" in llm_keys:
        field_hint_parts.append("целевая аудитория")
    if "ad_angle" in llm_keys:
        field_hint_parts.append("ракурс для рекламы")
    if "landing_page_gaps" in llm_keys:
        field_hint_parts.append("чего не хватает на странице")
    if "service_improvements" in llm_keys:
        field_hint_parts.append("что улучшить в сервисе")
    parts = [entity_name, base]
    if field_hint_parts:
        parts.append("; ".join(field_hint_parts))
    return ". ".join(p for p in parts if p)


def _auto_rag_kinds(llm_keys: list[str]) -> list[str]:
    keys = set(llm_keys or [])
    kinds: list[str] = ["review"]
    if {"best_doctor", "doctor_signal"} & keys:
        kinds.append("doctor")
    if {"service_description", "landing_page_gaps"} & keys:
        kinds.append("service")
    return kinds


def _collect_rag_snippets(
    bundle: "EntityBundle",
    entity_type: EntityType,
    *,
    llm_keys: list[str],
    rag_top_k: int,
    rag_query_override: str | None,
    city_slug: str | None,
    source_id: int | None,
    rag_kinds: list[str] | None = None,
    rag_search_fn: Any | None = None,
) -> list[dict[str, Any]]:
    if rag_top_k <= 0:
        return []
    try:
        from app.services.docdoc_rag import search_docdoc_rag as _default_search
    except Exception as exc:
        log.debug("docdoc_rag not available: %s", exc)
        return []
    search_fn = rag_search_fn or _default_search
    query = _entity_rag_query(entity_type, bundle.entity_name, llm_keys, rag_query_override)
    filters = bundle.filters or {}
    kinds = list(rag_kinds) if rag_kinds else _auto_rag_kinds(llm_keys)
    kwargs: dict[str, Any] = {
        "top_k": rag_top_k,
        "city_slug": city_slug,
        "source_id": source_id,
        "kinds": kinds,
    }
    if "clinic_alias" in filters:
        kwargs["clinic_alias"] = filters["clinic_alias"]
    if "service_name" in filters:
        kwargs["service_name"] = filters["service_name"]
    if "parent_service_name" in filters:
        kwargs["parent_service_name"] = filters["parent_service_name"]
    if "doctor_external_id" in filters:
        kwargs["doctor_external_id"] = filters["doctor_external_id"]
    try:
        result = search_fn(query, **kwargs)
    except Exception as exc:
        log.warning("RAG search failed for %s: %s", bundle.entity_id, exc)
        return []
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    return [it for it in (result.get("items") or []) if isinstance(it, dict)]


def _format_rag_snippets(snippets: list[dict[str, Any]], max_chars: int = 2400) -> str:
    if not snippets:
        return ""
    lines: list[str] = []
    used = 0
    for i, s in enumerate(snippets, 1):
        snippet = (s.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 380:
            snippet = snippet[:380] + "…"
        rating = s.get("rating_value")
        title = s.get("title") or s.get("doctor_name") or ""
        score = s.get("score")
        line = f"{i}. score={score}; оценка={rating}; контекст: {title} | {snippet}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _sample_reviews_text(reviews: list[dict[str, Any]], max_items: int = 12, max_chars: int = 6000) -> str:
    lines: list[str] = []
    used = 0
    for i, r in enumerate(reviews[:max_items], 1):
        text = (r.get("text") or "").strip().replace("\n", " ")
        if len(text) > 400:
            text = text[:400] + "…"
        rating = _rating_value(r)
        ans = (r.get("answer") or "").strip()
        block = f"{i}. оценка={rating}; ответ_клиники={'да' if ans else 'нет'}; текст: {text}"
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)


def _parse_llm_table_payload(text: str) -> list[dict[str, Any]] | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    start = t.find("[")
    if start < 0:
        parsed = parse_json_response(t)
        if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
            return parsed["rows"]
        return None
    depth = 0
    for i, ch in enumerate(t[start:], start=start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    arr = json.loads(t[start : i + 1])
                    return arr if isinstance(arr, list) else None
                except json.JSONDecodeError:
                    return None
    return None


def _llm_fill_entities(
    bundles: list[EntityBundle],
    llm_keys: list[str],
    *,
    reviews_per_entity: int,
    entity_type: EntityType,
    rag_snippets_by_entity: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    if not llm_keys or not bundles:
        return {}

    key_list = ", ".join(f'"{k}"' for k in llm_keys)
    blocks: list[str] = []
    has_any_rag = False
    for b in bundles:
        rag_block = ""
        snippets = (rag_snippets_by_entity or {}).get(b.entity_id) or []
        if snippets:
            has_any_rag = True
            rag_block = (
                "Релевантные фрагменты (RAG, отсортированы по релевантности):\n"
                + _format_rag_snippets(snippets)
                + "\n"
            )
        blocks.append(
            f"### entity_id={b.entity_id}\n"
            f"Название: {b.entity_name}\n"
            f"{rag_block}"
            f"Отзывы (последние из выборки):\n{_sample_reviews_text(b.reviews, max_items=reviews_per_entity)}"
        )

    system_prompt = (
        "Ты маркетолог и аналитик репутации медицинских клиник (DocDoc / СберЗдоровье). "
        "По отзывам пациентов заполни поля для каждого объекта. "
        "Ответ — только JSON-массив без markdown. Каждый элемент: "
        '{"entity_id": "<как во входе>", '
        + ", ".join(f'"{k}": "<строка>"' for k in llm_keys)
        + "}. "
        "Пиши по-русски, кратко (1–3 предложения на поле). Опирайся только на цитаты из отзывов"
        + (" и предоставленные релевантные фрагменты." if has_any_rag else ".")
    )
    user_prompt = (
        f"Тип объектов: {entity_type}.\n"
        f"Поля: {key_list}.\n\n"
        + "\n\n".join(blocks)
    )

    try:
        raw = chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.15,
            max_tokens=min(4000, 400 + 350 * len(bundles)),
        )
    except Exception as exc:
        log.warning("structured research LLM failed: %s", exc)
        return {}

    rows = _parse_llm_table_payload(raw)
    if not rows:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("entity_id") or "").strip()
        if not eid:
            continue
        out[eid] = {k: str(row.get(k) or "").strip() for k in llm_keys}
    return out


def _bundle_from_service_parsed(parsed: dict[str, Any]) -> EntityBundle | None:
    if not parsed.get("ok"):
        return None
    svc = parsed.get("service") if isinstance(parsed.get("service"), dict) else {}
    name = str(svc.get("name") or "")
    parent = str(svc.get("parent_service_name") or "")
    fake = {"service_name": name, "parent_service_name": parent}
    eid, display = _entity_key_from_review(fake, "service")
    reviews = [r for r in (parsed.get("reviews") or []) if isinstance(r, dict)]
    return EntityBundle(
        entity_id=eid,
        entity_name=display,
        reviews=reviews,
        filters=_filters_for_entity(fake, "service"),
    )


def _best_service_match_for_needle(
    crawl: dict[str, Any],
    needle: str,
) -> EntityBundle | None:
    """Одна услуга с наилучшим совпадением подстроки (для списка конкурентов)."""
    n = _normalize_match_text(needle)
    if not n:
        return None
    best: EntityBundle | None = None
    best_score = 0
    for parsed in crawl.get("services") or []:
        if not isinstance(parsed, dict):
            continue
        svc = parsed.get("service") if isinstance(parsed.get("service"), dict) else {}
        hay = _normalize_match_text(
            f"{svc.get('parent_service_name') or ''} {svc.get('name') or ''} {parsed.get('page_url') or ''}"
        )
        if n not in hay:
            continue
        score = len(n) + (10 if hay.startswith(n) or f" {n} " in f" {hay} " else 0)
        if score > best_score:
            b = _bundle_from_service_parsed(parsed)
            if b:
                best = b
                best_score = score
    return best


def _seed_service_bundles_from_crawl(
    crawl: dict[str, Any],
    needles: list[str] | None,
    *,
    one_per_needle: bool = False,
) -> dict[str, EntityBundle]:
    """Каталог услуг из crawl (даже без отзывов в общей выборке)."""
    grouped: dict[str, EntityBundle] = {}
    if one_per_needle and needles:
        for needle in needles:
            b = _best_service_match_for_needle(crawl, needle)
            if b and b.entity_id not in grouped:
                grouped[b.entity_id] = b
        return grouped

    for parsed in crawl.get("services") or []:
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            continue
        svc = parsed.get("service") if isinstance(parsed.get("service"), dict) else {}
        name = str(svc.get("name") or "")
        parent = str(svc.get("parent_service_name") or "")
        page_url = str(parsed.get("page_url") or "")
        hay = f"{parent} {name} {page_url}"
        if needles and not _matches_entities(hay, needles):
            continue
        b = _bundle_from_service_parsed(parsed)
        if b and b.entity_id not in grouped:
            grouped[b.entity_id] = b
    return grouped


def group_reviews_from_crawl(
    crawl: dict[str, Any],
    entity_type: EntityType,
    *,
    entities: list[str] | None,
    limit: int,
    match_each_entity: bool = False,
) -> list[EntityBundle]:
    needles = [e.strip() for e in (entities or []) if e.strip()] or None
    grouped: dict[str, EntityBundle] = {}

    if entity_type == "service" and needles:
        grouped = _seed_service_bundles_from_crawl(
            crawl,
            needles,
            one_per_needle=match_each_entity,
        )

    for rev in iter_reviews_deduped(crawl):
        eid, name = _entity_key_from_review(rev, entity_type)
        if needles:
            hay = f"{name} {eid}"
            if not _matches_entities(hay, needles):
                continue
        if eid not in grouped:
            grouped[eid] = EntityBundle(
                entity_id=eid,
                entity_name=name,
                reviews=[],
                filters=_filters_for_entity(rev, entity_type),
            )
        elif not grouped[eid].filters:
            grouped[eid].filters = _filters_for_entity(rev, entity_type)
        rid = rev.get("review_id")
        existing_ids = {x.get("review_id") for x in grouped[eid].reviews}
        if rid not in existing_ids:
            grouped[eid].reviews.append(rev)

    ranked = sorted(grouped.values(), key=lambda b: len(b.reviews), reverse=True)
    if not needles:
        ranked = ranked[:limit]
    else:
        ranked = ranked[:limit]
    return ranked


def group_reviews_from_db(
    db: Session,
    entity_type: EntityType,
    *,
    entities: list[str] | None,
    limit: int,
    source_id: int | None,
    city_slug: str | None,
) -> list[EntityBundle]:
    stmt = select(models.DocdocReview)
    if source_id is not None:
        stmt = stmt.where(models.DocdocReview.source_id == source_id)
    if city_slug:
        stmt = stmt.where(models.DocdocReview.city_slug == city_slug)
    rows = db.scalars(stmt).all()
    reviews: list[dict[str, Any]] = []
    for row in rows:
        reviews.append(
            {
                "review_id": row.external_review_id,
                "text": row.body,
                "answer": row.answer,
                "rating_value": row.rating_value,
                "rating_clinic": row.rating_clinic,
                "clinic_name": row.clinic_name,
                "clinic_alias": row.clinic_alias,
                "doctor_id": row.doctor_external_id,
                "doctor_name": row.doctor_name,
                "service_name": row.service_name,
                "parent_service_name": row.parent_service_name,
                "category_direction_title": row.category_direction,
                "created": row.review_created.isoformat() if row.review_created else None,
            }
        )
    return group_reviews_from_crawl(
        {"services": [], "reviews": reviews},
        entity_type,
        entities=entities,
        limit=limit,
    )


def discover_top_entities_db(
    db: Session,
    entity_type: EntityType,
    *,
    limit: int,
    source_id: int | None,
    city_slug: str | None,
) -> list[str]:
    """Топ имён объектов по числу отзывов (для подсказки в запросе)."""
    if entity_type == "clinic":
        col = models.DocdocReview.clinic_name
    elif entity_type == "service":
        col = models.DocdocReview.service_name
    elif entity_type == "category":
        col = models.DocdocReview.parent_service_name
    else:
        col = models.DocdocReview.doctor_name

    stmt = (
        select(col, func.count())
        .where(col != "")
        .group_by(col)
        .order_by(func.count().desc())
        .limit(limit)
    )
    if source_id is not None:
        stmt = stmt.where(models.DocdocReview.source_id == source_id)
    if city_slug:
        stmt = stmt.where(models.DocdocReview.city_slug == city_slug)
    return [str(name) for name, _ in db.execute(stmt).all() if name]


def run_structured_research(
    *,
    entity_type: EntityType = "clinic",
    entities: list[str] | None = None,
    field_keys: list[str] | None = None,
    preset: str | None = "clinic_competitors",
    limit: int = 10,
    source_id: int | None = None,
    city_slug: str | None = None,
    crawl_path: str | None = None,
    reviews_per_entity: int = 12,
    use_llm: bool = True,
    match_each_entity: bool = True,
    use_rag: bool = True,
    rag_top_k: int = 6,
    rag_query: str | None = None,
    rag_kinds: list[str] | None = None,
    rag_search_fn: Any | None = None,
) -> dict[str, Any]:
    fields = resolve_fields(field_keys, preset=preset)
    metric_keys = [f.key for f in fields if f.kind == "metric"]
    llm_keys = [f.key for f in fields if f.kind == "llm"]

    data_source = "db"
    bundles: list[EntityBundle] = []

    if crawl_path:
        path = Path(crawl_path).expanduser()
        if not path.is_file():
            repo_root = Path(__file__).resolve().parents[2]
            alt = repo_root / crawl_path
            path = alt if alt.is_file() else path
        path = path.resolve()
        if not path.is_file():
            return {"ok": False, "error": f"crawl file not found: {path}"}
        crawl = json.loads(path.read_text(encoding="utf-8"))
        bundles = group_reviews_from_crawl(
            crawl,
            entity_type,
            entities=entities,
            limit=limit,
            match_each_entity=match_each_entity and bool(entities),
        )
        data_source = "json"
    else:
        with SessionLocal() as db:
            if not entities:
                entities = discover_top_entities_db(
                    db,
                    entity_type,
                    limit=limit,
                    source_id=source_id,
                    city_slug=city_slug,
                )
            bundles = group_reviews_from_db(
                db,
                entity_type,
                entities=entities,
                limit=limit,
                source_id=source_id,
                city_slug=city_slug,
            )
            if not bundles:
                fallback = Path("docdoc_crawl_last.json")
                if fallback.is_file():
                    crawl = json.loads(fallback.read_text(encoding="utf-8"))
                    bundles = group_reviews_from_crawl(
                        crawl,
                        entity_type,
                        entities=entities,
                        limit=limit,
                        match_each_entity=match_each_entity and bool(entities),
                    )
                    data_source = "json_fallback"

    if not bundles:
        hint = "Загрузите краул в БД (POST /docdoc/ingest-checkpoint) или укажите crawl_path"
        if entities and crawl_path and entity_type == "service":
            try:
                crawl = json.loads(Path(crawl_path).expanduser().resolve().read_text(encoding="utf-8"))
                suggestions: list[str] = []
                for parsed in crawl.get("services") or []:
                    if not isinstance(parsed, dict) or not parsed.get("ok"):
                        continue
                    svc = parsed.get("service") or {}
                    label = f"{svc.get('parent_service_name') or ''} — {svc.get('name') or ''}".strip(" —")
                    if label and _matches_entities(
                        f"{label} {parsed.get('page_url') or ''}",
                        entities,
                    ):
                        suggestions.append(label)
                if suggestions:
                    hint = (
                        "Услуги найдены в каталоге, но без отзывов в крауле — проверьте full_reviews. "
                        f"Совпадения: {', '.join(suggestions[:8])}"
                    )
            except OSError:
                pass
        return {
            "ok": False,
            "error": "no_entities_matched",
            "hint": hint,
        }

    rag_snippets_by_entity: dict[str, list[dict[str, Any]]] = {}
    rag_used_entities = 0
    if use_rag and use_llm and llm_keys and rag_top_k > 0:
        for b in bundles:
            snippets = _collect_rag_snippets(
                b,
                entity_type,
                llm_keys=llm_keys,
                rag_top_k=rag_top_k,
                rag_query_override=rag_query,
                city_slug=city_slug,
                source_id=source_id,
                rag_kinds=rag_kinds,
                rag_search_fn=rag_search_fn,
            )
            if snippets:
                rag_snippets_by_entity[b.entity_id] = snippets
                rag_used_entities += 1

    llm_cells: dict[str, dict[str, Any]] = {}
    if use_llm and llm_keys:
        llm_cells = _llm_fill_entities(
            bundles,
            llm_keys,
            reviews_per_entity=reviews_per_entity,
            entity_type=entity_type,
            rag_snippets_by_entity=rag_snippets_by_entity or None,
        )

    table_rows: list[dict[str, Any]] = []
    for b in bundles:
        metrics = compute_metrics(b.reviews)
        cells: dict[str, Any] = {}
        for f in fields:
            if f.kind == "metric":
                cells[f.key] = metrics.get(f.key)
            else:
                cells[f.key] = (llm_cells.get(b.entity_id) or {}).get(f.key, "")
        table_rows.append(
            {
                "entity_id": b.entity_id,
                "entity_name": b.entity_name,
                "reviews_count": len(b.reviews),
                "cells": cells,
            }
        )

    return {
        "ok": True,
        "entity_type": entity_type,
        "preset": preset,
        "data_source": data_source,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "columns": [{"key": f.key, "label": f.label, "kind": f.kind} for f in fields],
        "rows": table_rows,
        "rag": {
            "used": bool(use_rag and use_llm and llm_keys),
            "top_k": rag_top_k,
            "entities_with_snippets": rag_used_entities,
            "total_snippets": sum(len(v) for v in rag_snippets_by_entity.values()),
        },
        "notes": (
            "Метрики считаются из отзывов; поля llm — по выборке текстов (LM Studio). "
            "Если RAG включён, в промпт также подмешиваются релевантные фрагменты из docdoc_chunks. "
            "Для полного города нужен ingest краула; сравнивайте объекты с похожим числом отзывов."
        ),
    }

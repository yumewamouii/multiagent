import asyncio
import os


import json
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session


import app.models.orm as models
from app.core.db import SessionLocal
from app.core.llm import chat_completion, parse_json_response
from app.models.orm import KnowledgeChunk
from app.parsers.wildberries.crawler import get_product_ids
from app.parsers.wildberries.parser import parse_product
from app.rag.embedding import create_embedding
from app.rag.retrieval import (
    dedupe_chunks_by_review,
    hybrid_rerank_chunks,
    keyword_search_chunks,
    semantic_search_chunks,
)


import logging

logging.basicConfig(
    level = logging.INFO,
    format = "%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(__name__)

INGESTION_STATE: dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_cycle_at": None,
    "last_sources_count": 0,
    "last_error": None,
    "last_error_at": None,
}


# ---------------------------
# utils
# ---------------------------

def is_valid_review_text(text: str) -> bool:
    if not text:
        return False

    text = text.strip()

    if len(text) < 5:
        return False

    if text.lower() in {"-", ".", "👍"}:
        return False

    return True


def summarize_review(text: str, max_chars: int = 220) -> str:
    return text[:max_chars].strip()


# ---------------------------
# LLM
# ---------------------------


def extract_json(text: str) -> dict | None:

    # ищем {...}
    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        return None

    json_str = match.group(0)

    try:
        return json.loads(json_str)
    except Exception:
        return None


def _normalize_review_tags(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        return ", ".join(str(x).strip() for x in raw if str(x).strip())
    return str(raw).strip()


def _parse_review_llm_content(content: str) -> dict | None:
    """Several strategies; models often wrap JSON in markdown or add prose."""
    if not (content or "").strip():
        return None
    parsed = parse_json_response(content)
    if isinstance(parsed, dict):
        return parsed
    parsed = extract_json(content)
    if isinstance(parsed, dict):
        return parsed
    # Частый формат: ключи без строгого JSON (кавычки/запятые)
    rating_m = re.search(r'"rating"\s*:\s*(\d+)', content)
    sentiment_m = re.search(r'"sentiment"\s*:\s*"([^"]*)"', content)
    summary_m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.DOTALL)
    if not summary_m:
        summary_m = re.search(r'"summary"\s*:\s*"([^"]{1,800})', content, re.DOTALL)
    tags_m = re.search(r'"tags"\s*:\s*\[(.*?)\]', content, re.DOTALL)
    if rating_m or sentiment_m or summary_m or tags_m:
        out: dict[str, Any] = {}
        if rating_m:
            out["rating"] = int(rating_m.group(1))
        if sentiment_m:
            out["sentiment"] = sentiment_m.group(1)
        if summary_m:
            out["summary"] = summary_m.group(1).replace("\\n", "\n").strip()
        if tags_m:
            inner = tags_m.group(1)
            parts = re.findall(r'"([^"]*)"', inner)
            out["tags"] = parts if parts else [inner.strip()]
        return out
    return None


def analyze_review_with_llm(text: str) -> dict:
    system_prompt = (
        "Ты анализируешь отзыв. Ответь одним JSON-объектом на одной строке, без markdown и без текста до/после. "
        "Схема: {\"rating\": <целое 1-5>, \"sentiment\": \"positive|neutral|negative\", "
        "\"summary\": \"кратко по-русски\", \"tags\": [\"тег1\", \"тег2\"]}. "
        "tags — 1-5 коротких слов."
    )
    user_prompt = f"Отзыв:\n{text}"
    try:
        content = chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=280,
        )
    except Exception:
        return {
            "rating": 4,
            "sentiment": "",
            "summary": text[:200],
            "tags": "",
        }

    parsed = _parse_review_llm_content(content)

    if not parsed:
        preview = (content or "").replace("\n", " ")[:300]
        log.debug("review LLM parse fallback, raw preview: %s", preview)

        return {
            "rating": 4,
            "sentiment": "",
            "summary": (content or text)[:200],
            "tags": "",
        }

    raw_rating = parsed.get("rating", 4)
    try:
        rating = int(float(raw_rating))
    except (TypeError, ValueError):
        rating = 4
    rating = max(1, min(5, rating))

    return {
        "rating": rating,
        "sentiment": str(parsed.get("sentiment", "") or ""),
        "summary": str(parsed.get("summary", "") or "")[:500],
        "tags": _normalize_review_tags(parsed.get("tags")),
    }


# ---------------------------
# DB helpers
# ---------------------------


def is_product_parsed(
    db: Session,
    source_id: int,
    external_id: str,
) -> bool:

    existing = db.scalar(
        select(models.Review).where(
            models.Review.source_id == source_id,
            models.Review.external_id == external_id,
        )
    )

    return existing is not None


def create_knowledge_chunk(
    db: Session,
    review: models.Review,
    *,
    sentiment: str,
    summary: str,
    tags: str,
):  
    chunk = models.KnowledgeChunk(
        review_id=review.id,
        summary=summary,
        sentiment=sentiment,
        tags=tags,
        embedding=create_embedding(review.body),
    )

    
    
    db.add(chunk)
    db.commit()
    db.refresh(chunk)

    return chunk


# ---------------------------
# WB fetch
# ---------------------------


def fetch_review_pages_from_source(
    source: models.Source,
) -> list[dict]:
    parser_type = (source.parser_type or "").strip().lower()
    wb_aliases = {"wb", "wildberries", "wb_parser", "wildberries_parser"}
    if parser_type not in wb_aliases:
        log.info(
            "skip source '%s': unsupported parser_type='%s' (expected one of %s)",
            source.name,
            source.parser_type,
            ", ".join(sorted(wb_aliases)),
        )
        return []
    
    log.info("WB parsing started")

    db = SessionLocal()

    ids = get_product_ids(30)
    log.info(f"ids found: {ids}")

    payloads = []

    for pid in ids:

        # проверяем, есть ли карточка в БД
        if is_product_parsed(db, source.id, pid):
            log.info(f"skip {pid} — already in DB")
            continue

        log.info(f"parse {pid} — not in DB yet")
        reviews = parse_product(pid)

        log.info(f"reviews found: {len(reviews)}")

        for r in reviews:
            payloads.append(
                {
                    "external_id": f"{pid}_{r['reviewer']}_{hash(r['comment'])}",  # уникальный id для каждого отзыва
                    "product_name": r["product_name"],
                    "author": r["reviewer"],
                    "text": r["comment"],
                    "rating": r["rating"],
                }
            )

        # возвращаем **только одну карточку за вызов**
        break

    db.close()
    log.info(f"TOTAL payloads returned: {len(payloads)}")

    return payloads


# ---------------------------
# ingest
# ---------------------------


def ingest_review_pages_batch(
    db: Session,
    source: models.Source,
    payloads: list[dict],
):

    reviews_to_add = []

    for payload in payloads:

        text = payload.get("text")

        if not is_valid_review_text(text):
            log.info(
                f"skip {payload['external_id']} — bad text"
            )
            continue

        existing = db.scalar(
            select(models.Review).where(
                models.Review.source_id == source.id,
                models.Review.external_id == payload["external_id"],
            )
        )

        if existing:
            log.info(
                f"skip {payload['external_id']} — exists"
            )
            continue

        ai = analyze_review_with_llm(text)

        review = models.Review(
            source_id=source.id,
            external_id=payload["external_id"],
            product_name=payload["product_name"],
            author=payload["author"],
            rating=float(payload["rating"]),
            body=text,
        )

        reviews_to_add.append((review, ai))

    if not reviews_to_add:
        log.info("nothing to save")
        return

    # -------- reviews --------

    db.add_all([r for r, _ in reviews_to_add])
    db.commit()

    log.info(
        f"{len(reviews_to_add)} reviews saved"
    )

    # -------- chunks --------

    chunks = []

    for review, ai in reviews_to_add:

        chunk = models.KnowledgeChunk(
            review_id=review.id,
            summary=ai["summary"],
            sentiment=ai.get("sentiment") or "нейтральное",
            tags=ai["tags"],
            embedding=create_embedding(f'{review.product_name}. {review.body}'),
        )

        chunks.append(chunk)

    db.add_all(chunks)
    db.commit()

    log.info(
        f"{len(chunks)} chunks created"
    )

# ---------------------------
# loop
# ---------------------------


def _ingestion_sync_pass() -> None:
    """Один проход ingestion: только sync I/O и LLM — выполнять в thread pool."""
    db = SessionLocal()
    try:
        sources = db.scalars(select(models.Source)).all()
        INGESTION_STATE["last_cycle_at"] = datetime.utcnow().isoformat()
        INGESTION_STATE["last_sources_count"] = len(sources)

        log.info("sources found: %s", len(sources))

        for source in sources:
            log.info("source: %s", source.name)
            payloads = fetch_review_pages_from_source(source)
            if not payloads:
                log.info("no new cards")
                continue
            ingest_review_pages_batch(db, source, payloads)
    except Exception as e:
        INGESTION_STATE["last_error"] = str(e)
        INGESTION_STATE["last_error_at"] = datetime.utcnow().isoformat()
        log.exception("INGEST ERROR: %s", e)
    finally:
        db.close()


async def run_continuous_ingestion(
    stop_event: asyncio.Event,
):

    poll_interval_sec = int(
        os.getenv(
            "PARSER_POLL_INTERVAL_SEC",
            "30",
        )
    )

    INGESTION_STATE["running"] = True
    INGESTION_STATE["last_started_at"] = datetime.utcnow().isoformat()
    INGESTION_STATE["last_error"] = None
    log.info("INGESTION STARTED")
    try:
        while not stop_event.is_set():
            await asyncio.to_thread(_ingestion_sync_pass)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=poll_interval_sec,
                )
            except asyncio.TimeoutError:
                continue
    finally:
        INGESTION_STATE["running"] = False


def get_ingestion_state() -> dict[str, Any]:
    return dict(INGESTION_STATE)


search_chunks = semantic_search_chunks
search_chunks_keyword = keyword_search_chunks


def _build_context(chunks: list[KnowledgeChunk]) -> str:
    context_lines = []
    for idx, chunk in enumerate(chunks, start=1):
        product = chunk.review.product_name if chunk.review else "unknown"
        review_text = chunk.review.body if chunk.review else ""
        context_lines.append(
            f"[{idx}] product={product}; sentiment={chunk.sentiment}; summary={chunk.summary}; review={review_text[:300]}"
        )
    return "\n".join(context_lines)


def _generate_grounded_answer(query: str, context: str) -> str:
    try:
        return chat_completion(
            system_prompt=(
                "Ты RAG-ассистент. Отвечай только по предоставленному контексту. "
                "Если данных недостаточно, явно скажи это. "
                "Используй ссылки вида [1], [2] на элементы контекста."
            ),
            user_prompt=f"Вопрос:\n{query}\n\nКонтекст:\n{context}",
            temperature=0.1,
            max_tokens=400,
        )
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("rag generation failed: %s", exc)
        return "Не удалось сгенерировать ответ через LLM. Вернул только найденные источники."


async def rag_answer(
    query: str,
    top_k: int = 5,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    candidate_k = max(top_k * 3, top_k)

    # 1) retrieve: vector + keyword
    vector_candidates = await search_chunks(
        query=query,
        top_k=candidate_k,
        source_id=source_id,
        date_from=date_from,
        date_to=date_to,
    )
    keyword_candidates = await search_chunks_keyword(
        query=query,
        top_k=candidate_k,
        source_id=source_id,
        date_from=date_from,
        date_to=date_to,
    )
    retrieved = dedupe_chunks_by_review(vector_candidates + keyword_candidates)

    vector_rank_by_review = {c.review_id: idx for idx, c in enumerate(vector_candidates)}
    keyword_rank_by_review = {c.review_id: idx for idx, c in enumerate(keyword_candidates)}
    query_embedding = create_embedding(f"query: {query}")

    ranked = hybrid_rerank_chunks(
        query_text=query,
        query_embedding=query_embedding,
        chunks=retrieved,
        vector_rank_by_review=vector_rank_by_review,
        keyword_rank_by_review=keyword_rank_by_review,
        candidate_k=candidate_k,
        top_k=top_k,
    )
    reranked = [chunk for chunk, _, _ in ranked]
    rerank_details = {
        (getattr(chunk, "id", None) if getattr(chunk, "id", None) is not None else chunk.review_id): detail
        for chunk, _, detail in ranked
    }


    context = _build_context(reranked)
    answer = _generate_grounded_answer(query=query, context=context)

    citations = []
    for i, chunk in enumerate(reranked, start=1):
        ck = getattr(chunk, "id", None)
        detail = rerank_details.get(ck if ck is not None else chunk.review_id, {})
        citations.append(
            {
                "rank": i,
                "chunk_id": getattr(chunk, "id", None),
                "review_id": chunk.review_id,
                "product_name": chunk.review.product_name if chunk.review else "",
                "summary": chunk.summary,
                "sentiment": chunk.sentiment,
                "tags": chunk.tags,
                "source_id": chunk.review.source_id if chunk.review else None,
                "collected_at": chunk.review.collected_at.isoformat() if chunk.review and chunk.review.collected_at else None,
                "semantic_similarity": round(float(detail.get("semantic_similarity", 0.0)), 4),
                "rerank_score": round(float(detail.get("rerank_score", 0.0)), 4),
            }
        )

    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    return {
        "query": query,
        "answer": answer,
        "citations": citations,
        "metrics": {
            "latency_ms": elapsed_ms,
            "retrieved_candidates": len(retrieved),
            "vector_candidates": len(vector_candidates),
            "keyword_candidates": len(keyword_candidates),
        },
    }


def get_dashboard_insights(
    product_name: str | None = None,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    with SessionLocal() as db:
        stmt = select(models.InsightRun).order_by(models.InsightRun.created_at.desc())
        if product_name:
            stmt = stmt.where(models.InsightRun.product_name.ilike(f"%{product_name}%"))
        if source_id is not None:
            stmt = stmt.where(models.InsightRun.source_id == source_id)
        if date_from is not None:
            stmt = stmt.where(models.InsightRun.created_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(models.InsightRun.created_at <= date_to)

        runs = db.execute(stmt).scalars().all()
        total = len(runs)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        end = start + page_size
        page_runs = runs[start:end]

        avg_confidence = round(sum(run.confidence for run in runs) / total, 3) if total else 0.0
        items = [
            {
                "run_id": run.id,
                "product_name": run.product_name,
                "source_id": run.source_id,
                "summary": run.summary,
                "confidence": run.confidence,
                "created_at": run.created_at.isoformat(),
            }
            for run in page_runs
        ]

        review_stmt = select(models.Review)
        if product_name:
            review_stmt = review_stmt.where(models.Review.product_name.ilike(f"%{product_name}%"))
        if source_id is not None:
            review_stmt = review_stmt.where(models.Review.source_id == source_id)
        if date_from is not None:
            review_stmt = review_stmt.where(models.Review.collected_at >= date_from)
        if date_to is not None:
            review_stmt = review_stmt.where(models.Review.collected_at <= date_to)

        reviews = db.execute(review_stmt).scalars().all()
        review_count = len(reviews)
        avg_rating = round(sum(item.rating for item in reviews) / review_count, 3) if review_count else 0.0
        negative_ratio = (
            round(sum(1 for item in reviews if item.rating <= 2.0) / review_count, 3)
            if review_count
            else 0.0
        )
        positive_ratio = (
            round(sum(1 for item in reviews if item.rating >= 4.0) / review_count, 3)
            if review_count
            else 0.0
        )

        return {
            "total_runs": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "avg_confidence": avg_confidence,
            "kpi": {
                "review_count": float(review_count),
                "avg_rating": avg_rating,
                "negative_ratio": negative_ratio,
                "positive_ratio": positive_ratio,
            },
            "items": items,
        }


def get_dashboard_timeseries(
    product_name: str | None = None,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict[str, float | str | int]]:
    with SessionLocal() as db:
        stmt = select(models.InsightRun).order_by(models.InsightRun.created_at.asc())
        if product_name:
            stmt = stmt.where(models.InsightRun.product_name.ilike(f"%{product_name}%"))
        if source_id is not None:
            stmt = stmt.where(models.InsightRun.source_id == source_id)
        if date_from is not None:
            stmt = stmt.where(models.InsightRun.created_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(models.InsightRun.created_at <= date_to)

        runs = db.execute(stmt).scalars().all()
        grouped: dict[str, list[float]] = defaultdict(list)
        for run in runs:
            key = run.created_at.date().isoformat()
            grouped[key].append(float(run.confidence))

        series = []
        for day in sorted(grouped.keys()):
            values = grouped[day]
            series.append(
                {
                    "date": day,
                    "runs": len(values),
                    "avg_confidence": round(sum(values) / len(values), 3),
                }
            )
        return series
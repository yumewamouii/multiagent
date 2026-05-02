import asyncio
import os


import json
import re
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload


from app import models
from app.db import SessionLocal

from app.parsers.wildberries.crawler import get_product_ids
from app.parsers.wildberries.parser import parse_product

from app.gigachat import gigachat_request


from app.ai.embedding import create_embedding


from app.models import KnowledgeChunk


import logging

logging.basicConfig(
    level = logging.INFO,
    format = "%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(__name__)


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


'''
def create_embedding(text: str) -> list[float] | None:

    if not text:
        return None

    if not os.getenv("GIGACHAT_API_KEY"):
        return None

    try:

        data = {
            "model": "Embeddings",
            "input": text,
        }

        result = gigachat_request(
            "embeddings",
            data,
        )

        embedding = result["data"][0]["embedding"]

        return embedding

    except Exception as e:

        log.warning(f"embedding error: {e}")

        return None
'''
# ---------------------------
# GIGACHAT
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


def analyze_review_with_gigachat(text: str) -> dict:

    if not os.getenv("GIGACHAT_API_KEY"):
        return {
            "rating": 4,
            "sentiment": "",
            "summary": text[:200],
            "tags": "",
        }

    prompt = f"""
Проанализируй отзыв и верни ТОЛЬКО JSON. Без текста. Без объяснений. Без markdown.
Формат:
{{
  "rating": int,
  "sentiment": str,
  "summary": str,
  "tags": list
}}
Отзыв:
{text}
"""
    data = {
        "model": "GigaChat",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
    }
    result = gigachat_request("chat/completions", data)

    content = result["choices"][0]["message"]["content"]

    parsed = extract_json(content)

    if not parsed:
        log.warning(f"GIGACHAT BAD JSON: {content}")

        return {
            "rating": 4,
            "sentiment": "",
            "summary": content[:200],
            "tags": "",
        }

    return {
        "rating": parsed.get("rating", 4),
        "sentiment": parsed.get("sentiment", ""),
        "summary": parsed.get("summary", ""),
        "tags": ", ".join(parsed.get("tags", [])),
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

    if source.parser_type != "wb":
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

        ai = analyze_review_with_gigachat(text)

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


async def run_continuous_ingestion(
    stop_event: asyncio.Event,
):

    poll_interval_sec = int(
        os.getenv(
            "PARSER_POLL_INTERVAL_SEC",
            "30",
        )
    )

    log.info("INGESTION STARTED")

    while not stop_event.is_set():

        db = SessionLocal()

        try:

            sources = db.scalars(
                select(models.Source)
            ).all()

            log.info(
                f"sources found: {len(sources)}"
            )

            for source in sources:

                log.info(
                    f"source: {source.name}"
                )

                payloads = fetch_review_pages_from_source(
                    source
                )

                if not payloads:
                    log.info(
                        "no new cards"
                    )
                    continue

                ingest_review_pages_batch(
                    db,
                    source,
                    payloads,
                )

        except Exception as e:

            log.exception(
                f"INGEST ERROR: {e}"
            )

        finally:

            db.close()

        try:

            await asyncio.wait_for(
                stop_event.wait(),
                timeout=poll_interval_sec,
            )

        except asyncio.TimeoutError:
            continue


async def search_chunks(
    query: str,
    top_k: int = 5,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    # создаём embedding запроса точно так же, как для chunk
    query_embedding = create_embedding(f"query: {query}")

    if query_embedding is None:
        return []

    def db_query():
        with SessionLocal() as db:
            stmt = (
                select(KnowledgeChunk)
                .join(KnowledgeChunk.review)
                .options(selectinload(KnowledgeChunk.review))
            )
            if source_id is not None:
                stmt = stmt.where(models.Review.source_id == source_id)
            if date_from is not None:
                stmt = stmt.where(models.Review.collected_at >= date_from)
            if date_to is not None:
                stmt = stmt.where(models.Review.collected_at <= date_to)

            stmt = stmt.order_by(
                KnowledgeChunk.embedding.cosine_distance(query_embedding)
            ).limit(top_k)

            return db.execute(stmt).scalars().all()

    import asyncio
    return await asyncio.to_thread(db_query)


async def search_chunks_keyword(
    query: str,
    top_k: int = 5,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    def db_query():
        with SessionLocal() as db:
            pattern = f"%{query}%"
            stmt = (
                select(KnowledgeChunk)
                .join(KnowledgeChunk.review)
                .options(selectinload(KnowledgeChunk.review))
                .where(
                    models.Review.product_name.ilike(pattern)
                    | models.Review.body.ilike(pattern)
                    | KnowledgeChunk.summary.ilike(pattern)
                    | KnowledgeChunk.tags.ilike(pattern)
                )
            )
            if source_id is not None:
                stmt = stmt.where(models.Review.source_id == source_id)
            if date_from is not None:
                stmt = stmt.where(models.Review.collected_at >= date_from)
            if date_to is not None:
                stmt = stmt.where(models.Review.collected_at <= date_to)
            stmt = stmt.limit(top_k)
            return db.execute(stmt).scalars().all()

    return await asyncio.to_thread(db_query)


def _dedupe_chunks(chunks: list[KnowledgeChunk]) -> list[KnowledgeChunk]:
    seen_review_ids = set()
    unique = []
    for chunk in chunks:
        if chunk.review_id in seen_review_ids:
            continue
        seen_review_ids.add(chunk.review_id)
        unique.append(chunk)
    return unique


def _keyword_overlap_score(query: str, text: str) -> float:
    q_tokens = {token for token in re.findall(r"\w+", query.lower()) if len(token) > 2}
    if not q_tokens:
        return 0.0
    t_tokens = set(re.findall(r"\w+", text.lower()))
    if not t_tokens:
        return 0.0
    return len(q_tokens.intersection(t_tokens)) / len(q_tokens)


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
    if not os.getenv("GIGACHAT_API_KEY"):
        return (
            "Сформирован ответ по локальному шаблону (без LLM). "
            "Источник информации: retrieved chunks."
        )

    prompt = f"""
Ты RAG-ассистент. Отвечай только по контексту. Если данных мало — так и скажи.
Укажи краткие выводы и опирайся на источники [1], [2], ...

Вопрос:
{query}

Контекст:
{context}
"""
    payload = {
        "model": "GigaChat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    try:
        response = gigachat_request("chat/completions", payload)
        return response["choices"][0]["message"]["content"]
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
    retrieved = _dedupe_chunks(vector_candidates + keyword_candidates)

    # 2) rerank: гибридный скоринг (keyword overlap + ранжирование retrieval)
    vector_ranks = {chunk.review_id: idx for idx, chunk in enumerate(vector_candidates)}
    keyword_ranks = {chunk.review_id: idx for idx, chunk in enumerate(keyword_candidates)}

    scored = []
    for chunk in retrieved:
        product = chunk.review.product_name if chunk.review else ""
        body = chunk.review.body if chunk.review else ""
        combined_text = f"{product} {chunk.summary} {body}"

        overlap = _keyword_overlap_score(query, combined_text)
        vec_rank = vector_ranks.get(chunk.review_id, candidate_k)
        key_rank = keyword_ranks.get(chunk.review_id, candidate_k)
        vec_bonus = max(0.0, 1.0 - (vec_rank * 0.06))
        key_bonus = max(0.0, 1.0 - (key_rank * 0.06))
        score = (0.50 * overlap) + (0.30 * vec_bonus) + (0.20 * key_bonus)
        scored.append((score, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)
    reranked = [chunk for _, chunk in scored[:top_k]]

    # 3) generate: grounded answer with citations
    context = _build_context(reranked)
    answer = _generate_grounded_answer(query=query, context=context)

    citations = []
    for i, chunk in enumerate(reranked, start=1):
        citations.append(
            {
                "rank": i,
                "review_id": chunk.review_id,
                "product_name": chunk.review.product_name if chunk.review else "",
                "summary": chunk.summary,
                "sentiment": chunk.sentiment,
                "tags": chunk.tags,
                "source_id": chunk.review.source_id if chunk.review else None,
                "collected_at": chunk.review.collected_at.isoformat() if chunk.review and chunk.review.collected_at else None,
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
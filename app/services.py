import asyncio
import os


import json
import re


import numpy as np


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


async def search_chunks(query: str, top_k: int = 5):
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
                .where(
                    models.Review.product_name.ilike(f"%{query}%")  # 👈 фильтр
                )
                .order_by(
                    KnowledgeChunk.embedding.cosine_distance(query_embedding)
                )
                .limit(top_k)
            )

            return db.execute(stmt).scalars().all()

    import asyncio
    return await asyncio.to_thread(db_query)
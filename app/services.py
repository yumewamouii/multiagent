import asyncio
import os
from datetime import datetime
from urllib.error import URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import SessionLocal


def summarize_review(text: str, max_chars: int = 220) -> str:
    return text[:max_chars].strip()


def create_embedding(text: str) -> list[float] | None:
    """
    Placeholder for GigaChat embeddings integration.
    In production, call GigaChat embeddings API and return a 1536-dim vector.
    """
    if not os.getenv("GIGACHAT_API_KEY"):
        return None
    return None




def fetch_html_page(url: str, timeout_sec: int = 20) -> str:
    """
    Выполняет GET-запрос к странице и возвращает её HTML.
    """
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; multiagent-bot/1.0)"})
    with urlopen(request, timeout=timeout_sec) as response:  # nosec B310
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_html_pages(urls: list[str]) -> list[str]:
    pages: list[str] = []
    for url in urls:
        try:
            pages.append(fetch_html_page(url))
        except (TimeoutError, URLError, ValueError):
            continue
    return pages

def parse_html_with_gigachat(html_page: str) -> dict[str, str | float]:
    """
    Здесь должна быть интеграция с GigaChat:
    вход: HTML страницы отзыва/листинга
    выход: review_text, rating, sentiment, summary, tags.
    """
    if not os.getenv("GIGACHAT_API_KEY"):
        return {
            "review_text": "GigaChat API key not configured: demo extracted review text.",
            "rating": 3.0,
            "sentiment": "neutral",
            "summary": "Demo summary extracted from HTML by placeholder.",
            "tags": "demo,html,gigachat",
        }

    return {
        "review_text": "GigaChat extracted review text from HTML.",
        "rating": 4.0,
        "sentiment": "positive",
        "summary": "GigaChat summary.",
        "tags": "gigachat,review,html",
    }


def create_knowledge_chunk(
    db: Session,
    review: models.Review,
    *,
    sentiment: str | None = None,
    summary: str | None = None,
    tags: str | None = None,
) -> models.KnowledgeChunk:
    chunk = models.KnowledgeChunk(
        review_id=review.id,
        summary=summary or summarize_review(review.body),
        sentiment=sentiment or "neutral",
        tags=tags or "",
        embedding=create_embedding(review.body),
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


def search_knowledge(db: Session, query: str) -> list[tuple[models.KnowledgeChunk, models.Review]]:
    stmt = (
        select(models.KnowledgeChunk, models.Review)
        .join(models.Review, models.KnowledgeChunk.review_id == models.Review.id)
        .where(models.KnowledgeChunk.summary.ilike(f"%{query}%"))
        .limit(50)
    )
    return list(db.execute(stmt).all())


def fetch_review_pages_from_source(source: models.Source) -> list[dict[str, str]]:
    """
    Получает HTML-страницы по GET и упаковывает их в payload для GigaChat.
    """
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M")
    urls = [source.base_url]
    html_pages = fetch_html_pages(urls)

    if not html_pages:
        html_pages = ["<html><body><div class='review'>Отличный товар, рекомендую.</div></body></html>"]

    return [
        {
            "external_id": f"{source.name}-{stamp}-{idx}",
            "product_name": "Демо товар",
            "author": "parser-bot",
            "html_page": html_page,
        }
        for idx, html_page in enumerate(html_pages, start=1)
    ]


def ingest_review_page(db: Session, source: models.Source, payload: dict[str, str]) -> None:
    existing = db.scalar(
        select(models.Review).where(
            models.Review.source_id == source.id,
            models.Review.external_id == payload["external_id"],
        )
    )
    if existing:
        return

    ai_result = parse_html_with_gigachat(payload["html_page"])
    review = models.Review(
        source_id=source.id,
        external_id=payload["external_id"],
        product_name=payload["product_name"],
        author=payload["author"],
        rating=float(ai_result["rating"]),
        body=str(ai_result["review_text"]),
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    create_knowledge_chunk(
        db,
        review,
        sentiment=str(ai_result["sentiment"]),
        summary=str(ai_result["summary"]),
        tags=str(ai_result["tags"]),
    )


async def run_continuous_ingestion(stop_event: asyncio.Event) -> None:
    poll_interval_sec = int(os.getenv("PARSER_POLL_INTERVAL_SEC", "60"))

    while not stop_event.is_set():
        db = SessionLocal()
        try:
            sources = db.scalars(select(models.Source)).all()
            for source in sources:
                for page in fetch_review_pages_from_source(source):
                    ingest_review_page(db, source, page)
        finally:
            db.close()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
        except asyncio.TimeoutError:
            continue

from __future__ import annotations

import asyncio
import math
import os
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import SessionLocal
from app.models.orm import KnowledgeChunk, Review, TelegramChatAnalysis
from app.rag.embedding import create_embedding


def _as_float_list(embedding: object) -> list[float]:
    if embedding is None:
        return []
    if isinstance(embedding, list):
        return [float(x) for x in embedding]
    return [float(x) for x in embedding]


def cosine_similarity_score(query_vec: list[float], doc_vec: list[float]) -> float:
    if not query_vec or not doc_vec or len(query_vec) != len(doc_vec):
        return 0.0
    dot = sum(a * b for a, b in zip(query_vec, doc_vec, strict=True))
    nq = math.sqrt(sum(a * a for a in query_vec))
    nd = math.sqrt(sum(b * b for b in doc_vec))
    if nq == 0 or nd == 0:
        return 0.0
    sim = dot / (nq * nd)
    return max(0.0, min(1.0, sim))


async def _semantic_vector_search(
    query_embedding: list[float],
    top_k: int,
    source_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[KnowledgeChunk]:
    def db_query():
        with SessionLocal() as db:
            stmt = (
                select(KnowledgeChunk)
                .join(KnowledgeChunk.review)
                .options(selectinload(KnowledgeChunk.review))
            )
            if source_id is not None:
                stmt = stmt.where(Review.source_id == source_id)
            if date_from is not None:
                stmt = stmt.where(Review.collected_at >= date_from)
            if date_to is not None:
                stmt = stmt.where(Review.collected_at <= date_to)

            stmt = stmt.order_by(
                KnowledgeChunk.embedding.cosine_distance(query_embedding)
            ).limit(top_k)

            return db.execute(stmt).scalars().all()

    return await asyncio.to_thread(db_query)


async def semantic_search_chunks(
    query: str,
    top_k: int = 5,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[KnowledgeChunk]:
    query_embedding = create_embedding(f"query: {query}")

    if query_embedding is None:
        return []

    return await _semantic_vector_search(
        query_embedding, top_k, source_id, date_from, date_to
    )


async def semantic_search_chunks_scored(
    query: str,
    top_k: int = 5,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[float] | None, list[tuple[KnowledgeChunk, float]]]:
    query_embedding = create_embedding(f"query: {query}")
    if query_embedding is None:
        return None, []

    chunks = await _semantic_vector_search(
        query_embedding, top_k, source_id, date_from, date_to
    )
    scored: list[tuple[KnowledgeChunk, float]] = []
    for c in chunks:
        dv = _as_float_list(getattr(c, "embedding", None))
        sim = cosine_similarity_score(query_embedding, dv) if dv else 0.0
        scored.append((c, sim))
    return query_embedding, scored


async def keyword_search_chunks(
    query: str,
    top_k: int = 5,
    source_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[KnowledgeChunk]:
    def db_query():
        with SessionLocal() as db:
            pattern = f"%{query}%"
            stmt = (
                select(KnowledgeChunk)
                .join(KnowledgeChunk.review)
                .options(selectinload(KnowledgeChunk.review))
                .where(
                    Review.product_name.ilike(pattern)
                    | Review.body.ilike(pattern)
                    | KnowledgeChunk.summary.ilike(pattern)
                    | KnowledgeChunk.tags.ilike(pattern)
                )
            )
            if source_id is not None:
                stmt = stmt.where(Review.source_id == source_id)
            if date_from is not None:
                stmt = stmt.where(Review.collected_at >= date_from)
            if date_to is not None:
                stmt = stmt.where(Review.collected_at <= date_to)
            stmt = stmt.limit(top_k)
            return db.execute(stmt).scalars().all()

    return await asyncio.to_thread(db_query)


def dedupe_chunks_by_review(chunks: list[KnowledgeChunk]) -> list[KnowledgeChunk]:
    seen_review_ids: set[int] = set()
    unique: list[KnowledgeChunk] = []
    for chunk in chunks:
        if chunk.review_id in seen_review_ids:
            continue
        seen_review_ids.add(chunk.review_id)
        unique.append(chunk)
    return unique


def keyword_overlap_score(query: str, text: str) -> float:
    q_tokens = {token for token in re.findall(r"\w+", query.lower()) if len(token) > 2}
    if not q_tokens:
        return 0.0
    t_tokens = set(re.findall(r"\w+", text.lower()))
    if not t_tokens:
        return 0.0
    return len(q_tokens.intersection(t_tokens)) / len(q_tokens)


def hybrid_rerank_chunks(
    *,
    query_text: str,
    query_embedding: list[float] | None,
    chunks: list[KnowledgeChunk],
    vector_rank_by_review: dict[int, int],
    keyword_rank_by_review: dict[int, int],
    candidate_k: int,
    top_k: int,
) -> list[tuple[KnowledgeChunk, float, dict[str, float]]]:
    w_sem = float(os.getenv("RERANK_SEMANTIC_WEIGHT", "0.55"))
    w_lex = float(os.getenv("RERANK_LEXICAL_WEIGHT", "0.30"))
    w_vr = float(os.getenv("RERANK_VECTOR_RANK_WEIGHT", "0.075"))
    w_kr = float(os.getenv("RERANK_KEYWORD_RANK_WEIGHT", "0.075"))
    total_w = w_sem + w_lex + w_vr + w_kr
    if total_w <= 0:
        total_w = 1.0
    w_sem, w_lex, w_vr, w_kr = w_sem / total_w, w_lex / total_w, w_vr / total_w, w_kr / total_w

    out: list[tuple[KnowledgeChunk, float, dict[str, float]]] = []

    for chunk in chunks:
        product = chunk.review.product_name if chunk.review else ""
        body = chunk.review.body if chunk.review else ""
        combined_text = f"{product} {chunk.summary} {body}"
        lexical = keyword_overlap_score(query_text, combined_text)

        doc_vec = _as_float_list(getattr(chunk, "embedding", None))
        if query_embedding and doc_vec:
            semantic = cosine_similarity_score(query_embedding, doc_vec)
        else:
            semantic = 0.0

        rid = chunk.review_id
        vec_rank = vector_rank_by_review.get(rid, candidate_k)
        key_rank = keyword_rank_by_review.get(rid, candidate_k)
        vec_bonus = max(0.0, 1.0 - (vec_rank * 0.06))
        key_bonus = max(0.0, 1.0 - (key_rank * 0.06))

        score = (
            w_sem * semantic
            + w_lex * lexical
            + w_vr * vec_bonus
            + w_kr * key_bonus
        )

        detail = {
            "semantic_similarity": semantic,
            "lexical_overlap": lexical,
            "vector_rank_bonus": vec_bonus,
            "keyword_rank_bonus": key_bonus,
            "rerank_score": score,
        }
        out.append((chunk, score, detail))

    out.sort(key=lambda item: item[1], reverse=True)
    return out[:top_k]


async def semantic_search_telegram_analyses(
    query: str,
    top_k: int = 8,
    export_key: str | None = None,
    market_research_only: bool = True,
    exclude_spam: bool = True,
) -> list[TelegramChatAnalysis]:
    query_embedding = create_embedding(f"query: {query}")
    if query_embedding is None:
        return []
    return await _semantic_search_telegram_with_embedding(
        query_embedding,
        top_k=top_k,
        export_key=export_key,
        market_research_only=market_research_only,
        exclude_spam=exclude_spam,
    )


async def _semantic_search_telegram_with_embedding(
    query_embedding: list[float],
    *,
    top_k: int,
    export_key: str | None,
    market_research_only: bool,
    exclude_spam: bool,
) -> list[TelegramChatAnalysis]:
    def db_query():
        with SessionLocal() as db:
            stmt = select(TelegramChatAnalysis).where(TelegramChatAnalysis.embedding.isnot(None))
            if export_key:
                stmt = stmt.where(TelegramChatAnalysis.export_key == export_key)
            if market_research_only:
                stmt = stmt.where(TelegramChatAnalysis.market_research_interest.is_(True))
            if exclude_spam:
                stmt = stmt.where(TelegramChatAnalysis.spam_or_ad.is_(False))
            stmt = stmt.order_by(
                TelegramChatAnalysis.embedding.cosine_distance(query_embedding)
            ).limit(top_k)
            return db.execute(stmt).scalars().all()

    return await asyncio.to_thread(db_query)


async def semantic_search_telegram_analyses_scored(
    query: str,
    top_k: int = 8,
    export_key: str | None = None,
    market_research_only: bool = True,
    exclude_spam: bool = True,
) -> tuple[list[float] | None, list[tuple[TelegramChatAnalysis, float]]]:
    query_embedding = create_embedding(f"query: {query}")
    if query_embedding is None:
        return None, []

    rows = await _semantic_search_telegram_with_embedding(
        query_embedding,
        top_k=top_k,
        export_key=export_key,
        market_research_only=market_research_only,
        exclude_spam=exclude_spam,
    )
    scored: list[tuple[TelegramChatAnalysis, float]] = []
    for row in rows:
        dv = _as_float_list(getattr(row, "embedding", None))
        sim = cosine_similarity_score(query_embedding, dv) if dv else 0.0
        scored.append((row, sim))
    return query_embedding, scored

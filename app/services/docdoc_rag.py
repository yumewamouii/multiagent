"""RAG-индекс по DocDoc: отзывы + врачи + услуги в одной таблице `docdoc_chunks`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import orm as models
from app.rag.embedding import create_embedding
from app.rag.retrieval import _as_float_list, cosine_similarity_score
from app.services.docdoc_ingest import (
    _BatchCommitter,
    get_or_create_docdoc_source,
    iter_reviews_deduped,
)

log = logging.getLogger(__name__)

ChunkKind = Literal["review", "doctor", "service"]
KINDS: tuple[ChunkKind, ...] = ("review", "doctor", "service")
DEFAULT_BATCH = 32
MAX_BODY_CHARS = 4000


@dataclass
class ChunkDraft:
    kind: ChunkKind
    ref_external_id: str
    title: str
    body: str
    service_external_id: int | None = None
    service_name: str = ""
    parent_service_name: str = ""
    clinic_alias: str = ""
    clinic_name: str = ""
    doctor_external_id: int | None = None
    doctor_name: str = ""
    rating_value: float | None = None
    source_page_url: str = ""
    tags: str = ""

    def upsert_values(self, source_id: int, city_slug: str) -> dict[str, Any]:
        body = (self.body or "").strip()[:MAX_BODY_CHARS]
        return {
            "source_id": source_id,
            "kind": self.kind,
            "ref_external_id": self.ref_external_id,
            "city_slug": city_slug,
            "service_external_id": self.service_external_id,
            "service_name": self.service_name,
            "parent_service_name": self.parent_service_name,
            "clinic_alias": self.clinic_alias,
            "clinic_name": self.clinic_name,
            "doctor_external_id": self.doctor_external_id,
            "doctor_name": self.doctor_name,
            "title": self.title[:512],
            "body": body,
            "tags": self.tags[:256],
            "rating_value": self.rating_value,
            "source_page_url": self.source_page_url[:512],
        }


def _norm_int(v: Any) -> int | None:
    try:
        n = int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    if n is None or n <= 0:
        return None
    return n


def _norm_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def review_to_chunk(rev: dict[str, Any]) -> ChunkDraft | None:
    rid = _norm_int(rev.get("review_id"))
    text = (rev.get("text") or rev.get("body") or "").strip()
    if rid is None or not text:
        return None
    answer = (rev.get("answer") or "").strip()
    service_name = str(rev.get("service_name") or "")
    parent = str(rev.get("parent_service_name") or "")
    clinic = str(rev.get("clinic_name") or "")
    doctor = str(rev.get("doctor_name") or "")
    parts = [text]
    if answer:
        parts.append(f"\n\n[Ответ клиники]: {answer}")
    head_bits = [parent, service_name, clinic]
    title_main = " — ".join(b for b in head_bits if b)
    rating = _norm_float(rev.get("rating_value")) or _norm_float(rev.get("rating_clinic"))
    return ChunkDraft(
        kind="review",
        ref_external_id=str(rid),
        title=title_main or f"Отзыв #{rid}",
        body="".join(parts),
        service_external_id=_norm_int(rev.get("service_id")),
        service_name=service_name,
        parent_service_name=parent,
        clinic_alias=str(rev.get("clinic_alias") or ""),
        clinic_name=clinic,
        doctor_external_id=_norm_int(rev.get("doctor_id")),
        doctor_name=doctor,
        rating_value=rating,
        source_page_url=str(rev.get("source_page_url") or ""),
        tags="answered" if answer else "no_answer",
    )


def service_parsed_to_chunk(parsed: dict[str, Any]) -> ChunkDraft | None:
    if not parsed.get("ok"):
        return None
    svc = parsed.get("service") if isinstance(parsed.get("service"), dict) else {}
    ext_id = _norm_int(svc.get("id"))
    name = str(svc.get("name") or "")
    if ext_id is None or not name:
        return None
    parent = str(svc.get("parent_service_name") or "")
    description = str(svc.get("description_plain") or "")
    avg_price = svc.get("avg_price")
    crumbs = parsed.get("breadcrumbs") or []
    direction = ""
    if crumbs and isinstance(crumbs[0], dict):
        direction = str(crumbs[0].get("name") or "")
    body_lines = [
        f"Услуга: {name}",
        f"Направление: {parent or direction}",
    ]
    if avg_price:
        body_lines.append(f"Средняя цена: {avg_price}")
    if description:
        body_lines.append("\n" + description)
    synonyms = svc.get("synonyms")
    if isinstance(synonyms, list) and synonyms:
        body_lines.append("Синонимы: " + ", ".join(str(s) for s in synonyms[:8]))
    title = f"{parent} — {name}" if parent else name
    return ChunkDraft(
        kind="service",
        ref_external_id=str(ext_id),
        title=title,
        body="\n".join(body_lines),
        service_external_id=ext_id,
        service_name=name,
        parent_service_name=parent,
        rating_value=None,
        source_page_url=str(parsed.get("page_url") or ""),
        tags="catalog",
    )


def doctor_to_chunk(doc: dict[str, Any]) -> ChunkDraft | None:
    ext_id = _norm_int(doc.get("doctor_id"))
    name = str(doc.get("name") or "")
    if ext_id is None or not name:
        return None
    speciality = str(doc.get("speciality") or doc.get("specialization") or "")
    address = str(doc.get("address") or "")
    rating = _norm_float(doc.get("total_rating"))
    reviews_count = doc.get("reviews_count")
    price = doc.get("price")
    service_name = str(doc.get("service_name") or "")
    parent = str(doc.get("parent_service_name") or "")
    body_lines = [f"Врач: {name}"]
    if speciality:
        body_lines.append(f"Специальность: {speciality}")
    if service_name:
        body_lines.append(f"Услуга: {parent + ' — ' if parent else ''}{service_name}")
    if address:
        body_lines.append(f"Адрес: {address}")
    if rating is not None:
        body_lines.append(f"Рейтинг: {rating}")
    if reviews_count is not None:
        body_lines.append(f"Число отзывов: {reviews_count}")
    if price is not None:
        body_lines.append(f"Цена приёма: {price}")
    return ChunkDraft(
        kind="doctor",
        ref_external_id=str(ext_id),
        title=f"{name} — {speciality}" if speciality else name,
        body="\n".join(body_lines),
        service_external_id=_norm_int(doc.get("service_external_id") or doc.get("service_id")),
        service_name=service_name,
        parent_service_name=parent,
        doctor_external_id=ext_id,
        doctor_name=name,
        rating_value=rating,
        source_page_url=str(doc.get("profile_url") or ""),
        tags="doctor_card",
    )


def iter_drafts_from_crawl(
    crawl: dict[str, Any],
    *,
    kinds: Iterable[ChunkKind],
) -> Iterator[ChunkDraft]:
    kind_set = set(kinds)
    if "service" in kind_set:
        for parsed in crawl.get("services") or []:
            if not isinstance(parsed, dict):
                continue
            ch = service_parsed_to_chunk(parsed)
            if ch:
                yield ch
    if "doctor" in kind_set:
        seen_docs: set[int] = set()
        for parsed in crawl.get("services") or []:
            if not isinstance(parsed, dict):
                continue
            for doc in parsed.get("doctors") or []:
                if not isinstance(doc, dict):
                    continue
                ext = _norm_int(doc.get("doctor_id"))
                if ext is None or ext in seen_docs:
                    continue
                seen_docs.add(ext)
                ch = doctor_to_chunk(doc)
                if ch:
                    yield ch
    if "review" in kind_set:
        for rev in iter_reviews_deduped(crawl):
            ch = review_to_chunk(rev)
            if ch:
                yield ch


def iter_drafts_from_db(
    db: Session,
    *,
    source_id: int | None,
    city_slug: str | None,
    kinds: Iterable[ChunkKind],
) -> Iterator[ChunkDraft]:
    kind_set = set(kinds)
    if "review" in kind_set:
        stmt = select(models.DocdocReview)
        if source_id is not None:
            stmt = stmt.where(models.DocdocReview.source_id == source_id)
        if city_slug:
            stmt = stmt.where(models.DocdocReview.city_slug == city_slug)
        for row in db.scalars(stmt):
            ch = review_to_chunk(
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
                    "service_id": row.service_external_id,
                    "service_name": row.service_name,
                    "parent_service_name": row.parent_service_name,
                    "source_page_url": row.source_page_url,
                }
            )
            if ch:
                yield ch
    if "doctor" in kind_set:
        stmt = select(models.DocdocDoctor)
        if source_id is not None:
            stmt = stmt.where(models.DocdocDoctor.source_id == source_id)
        if city_slug:
            stmt = stmt.where(models.DocdocDoctor.city_slug == city_slug)
        for row in db.scalars(stmt):
            ch = doctor_to_chunk(
                {
                    "doctor_id": row.external_doctor_id,
                    "name": row.name,
                    "speciality": row.speciality,
                    "address": row.address,
                    "total_rating": row.total_rating,
                    "reviews_count": row.reviews_count,
                    "price": row.price,
                    "service_external_id": row.service_external_id,
                    "service_name": row.service_name,
                    "parent_service_name": row.parent_service_name,
                    "profile_url": row.profile_url,
                }
            )
            if ch:
                yield ch
    if "service" in kind_set:
        stmt = select(models.DocdocService)
        if source_id is not None:
            stmt = stmt.where(models.DocdocService.source_id == source_id)
        if city_slug:
            stmt = stmt.where(models.DocdocService.city_slug == city_slug)
        for row in db.scalars(stmt):
            ch = service_parsed_to_chunk(
                {
                    "ok": True,
                    "page_url": row.page_url,
                    "service": {
                        "id": row.external_service_id,
                        "name": row.name,
                        "parent_service_name": row.parent_service_name,
                        "description_plain": row.description_plain,
                        "avg_price": row.avg_price,
                    },
                    "breadcrumbs": [{"name": row.category_direction}],
                }
            )
            if ch:
                yield ch


def _upsert_chunk(
    db: Session,
    *,
    source_id: int,
    city_slug: str,
    draft: ChunkDraft,
    embedding: list[float] | None,
) -> None:
    values = draft.upsert_values(source_id=source_id, city_slug=city_slug)
    values["embedding"] = embedding
    values["collected_at"] = datetime.utcnow()
    stmt = pg_insert(models.DocdocChunk).values(**values)
    update_fields = {k: v for k, v in values.items() if k not in {"source_id", "kind", "ref_external_id"}}
    stmt = stmt.on_conflict_do_update(
        constraint="uq_docdoc_chunk",
        set_=update_fields,
    )
    db.execute(stmt)


def build_docdoc_rag_index(
    *,
    source: Literal["db", "json", "auto"] = "auto",
    crawl_path: str | None = None,
    source_id: int | None = None,
    city_slug: str | None = None,
    kinds: Iterable[ChunkKind] = KINDS,
    skip_existing_embeddings: bool = True,
    max_chunks: int | None = None,
    batch_commit: int | None = None,
) -> dict[str, Any]:
    """
    Строит RAG-индекс по DocDoc.
      - source="db"    — из docdoc_reviews/doctors/services
      - source="json"  — из crawl_path или docdoc_crawl_last.json
      - source="auto"  — БД, при пустой выборке — fallback на JSON
    """
    if batch_commit is None:
        batch_commit = int(os.getenv("DOCDOC_RAG_BUILD_BATCH", str(DEFAULT_BATCH)))

    counts = {"review": 0, "doctor": 0, "service": 0, "skipped": 0, "embedded": 0}
    used_source = source

    def _embed_and_write(drafts: Iterable[ChunkDraft], db: Session, source_id_val: int, city: str) -> None:
        nonlocal counts
        batch = _BatchCommitter(db, batch_commit, label="docdoc_rag")
        for draft in drafts:
            if max_chunks is not None and sum(counts[k] for k in ("review", "doctor", "service")) >= max_chunks:
                break
            existing_emb: list[float] | None = None
            if skip_existing_embeddings:
                existing = db.scalar(
                    select(models.DocdocChunk).where(
                        (models.DocdocChunk.source_id == source_id_val)
                        & (models.DocdocChunk.kind == draft.kind)
                        & (models.DocdocChunk.ref_external_id == draft.ref_external_id)
                    )
                )
                if existing is not None and existing.embedding is not None:
                    existing_emb = _as_float_list(existing.embedding)
            embedding = existing_emb
            if embedding is None:
                emb_text = f"passage: {draft.title}\n{draft.body}"
                embedding = create_embedding(emb_text)
                if embedding is None:
                    counts["skipped"] += 1
                else:
                    counts["embedded"] += 1
            _upsert_chunk(
                db,
                source_id=source_id_val,
                city_slug=city,
                draft=draft,
                embedding=embedding,
            )
            counts[draft.kind] += 1
            batch.bump()
        batch.flush()

    if source in ("json",) or (source == "auto" and not source_id):
        path = Path(crawl_path or "docdoc_crawl_last.json").expanduser()
        if not path.is_file():
            return {"ok": False, "error": f"crawl file not found: {path}"}
        crawl = json.loads(path.read_text(encoding="utf-8"))
        base_url = str(crawl.get("base_url") or "https://irk.docdoc.ru/")
        city = city_slug or str(crawl.get("city_slug") or "unknown")
        with SessionLocal() as db:
            src = get_or_create_docdoc_source(db, base_url, city)
            db.commit()
            _embed_and_write(iter_drafts_from_crawl(crawl, kinds=kinds), db, src.id, city)
            return {
                "ok": True,
                "source_used": "json",
                "source_id": src.id,
                "city_slug": city,
                "counts": counts,
            }

    with SessionLocal() as db:
        sid = source_id
        city = city_slug or ""
        if sid is None:
            src = db.scalar(select(models.Source).where(models.Source.parser_type == "docdoc"))
            if src is None:
                if source == "db":
                    return {"ok": False, "error": "no_docdoc_source_in_db"}
                # fallback на JSON
                pass
            else:
                sid = src.id
                if not city:
                    sample = db.scalar(select(models.DocdocReview).where(models.DocdocReview.source_id == sid))
                    if sample:
                        city = sample.city_slug
        if sid is not None:
            used_source = "db"
            _embed_and_write(iter_drafts_from_db(db, source_id=sid, city_slug=city or None, kinds=kinds), db, sid, city)
            if sum(counts[k] for k in ("review", "doctor", "service")) > 0:
                return {
                    "ok": True,
                    "source_used": used_source,
                    "source_id": sid,
                    "city_slug": city,
                    "counts": counts,
                }
            if source == "db":
                return {
                    "ok": True,
                    "source_used": "db",
                    "source_id": sid,
                    "city_slug": city,
                    "counts": counts,
                }

    # auto-fallback на JSON
    if source == "auto":
        return build_docdoc_rag_index(
            source="json",
            crawl_path=crawl_path,
            city_slug=city_slug,
            kinds=kinds,
            skip_existing_embeddings=skip_existing_embeddings,
            max_chunks=max_chunks,
            batch_commit=batch_commit,
        )
    return {"ok": False, "error": "no_data"}


# ----------------------------- search -----------------------------


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _keyword_overlap(query: str, text: str) -> float:
    q_tokens = {t for t in _TOKEN_RE.findall((query or "").lower()) if len(t) > 2}
    if not q_tokens:
        return 0.0
    t_tokens = set(_TOKEN_RE.findall((text or "").lower()))
    if not t_tokens:
        return 0.0
    return len(q_tokens & t_tokens) / len(q_tokens)


def _apply_filters(stmt, *, kinds, city_slug, source_id, service_name, parent_service_name, clinic_alias, doctor_external_id):
    if kinds:
        stmt = stmt.where(models.DocdocChunk.kind.in_(list(kinds)))
    if city_slug:
        stmt = stmt.where(models.DocdocChunk.city_slug == city_slug)
    if source_id is not None:
        stmt = stmt.where(models.DocdocChunk.source_id == source_id)
    if service_name:
        stmt = stmt.where(models.DocdocChunk.service_name.ilike(f"%{service_name}%"))
    if parent_service_name:
        stmt = stmt.where(models.DocdocChunk.parent_service_name.ilike(f"%{parent_service_name}%"))
    if clinic_alias:
        stmt = stmt.where(models.DocdocChunk.clinic_alias == clinic_alias)
    if doctor_external_id is not None:
        stmt = stmt.where(models.DocdocChunk.doctor_external_id == doctor_external_id)
    return stmt


def search_docdoc_rag(
    query: str,
    *,
    top_k: int = 10,
    kinds: list[ChunkKind] | None = None,
    city_slug: str | None = None,
    source_id: int | None = None,
    service_name: str | None = None,
    parent_service_name: str | None = None,
    clinic_alias: str | None = None,
    doctor_external_id: int | None = None,
    candidate_k: int | None = None,
    semantic_weight: float | None = None,
    lexical_weight: float | None = None,
) -> dict[str, Any]:
    """Гибридный поиск: pgvector cosine + ILIKE keyword + ререйкинг."""
    if not query.strip():
        return {"ok": False, "error": "empty_query", "items": []}
    candidate_k = candidate_k or max(top_k * 4, 30)
    w_sem = semantic_weight if semantic_weight is not None else float(os.getenv("DOCDOC_RAG_SEM_WEIGHT", "0.65"))
    w_lex = lexical_weight if lexical_weight is not None else float(os.getenv("DOCDOC_RAG_LEX_WEIGHT", "0.35"))
    total = w_sem + w_lex
    if total <= 0:
        total = 1.0
    w_sem, w_lex = w_sem / total, w_lex / total

    query_embedding = create_embedding(f"query: {query}")
    pattern = f"%{query.strip()}%"

    rows: list[models.DocdocChunk] = []
    with SessionLocal() as db:
        if query_embedding is not None:
            sem_stmt = select(models.DocdocChunk).where(models.DocdocChunk.embedding.isnot(None))
            sem_stmt = _apply_filters(
                sem_stmt,
                kinds=kinds,
                city_slug=city_slug,
                source_id=source_id,
                service_name=service_name,
                parent_service_name=parent_service_name,
                clinic_alias=clinic_alias,
                doctor_external_id=doctor_external_id,
            ).order_by(models.DocdocChunk.embedding.cosine_distance(query_embedding)).limit(candidate_k)
            rows.extend(db.scalars(sem_stmt).all())
        kw_stmt = select(models.DocdocChunk).where(
            models.DocdocChunk.body.ilike(pattern)
            | models.DocdocChunk.title.ilike(pattern)
            | models.DocdocChunk.tags.ilike(pattern)
        )
        kw_stmt = _apply_filters(
            kw_stmt,
            kinds=kinds,
            city_slug=city_slug,
            source_id=source_id,
            service_name=service_name,
            parent_service_name=parent_service_name,
            clinic_alias=clinic_alias,
            doctor_external_id=doctor_external_id,
        ).limit(candidate_k)
        rows.extend(db.scalars(kw_stmt).all())

    seen: set[int] = set()
    unique: list[models.DocdocChunk] = []
    for r in rows:
        if r.id in seen:
            continue
        seen.add(r.id)
        unique.append(r)

    scored: list[dict[str, Any]] = []
    for r in unique:
        sem = 0.0
        if query_embedding is not None:
            doc_vec = _as_float_list(getattr(r, "embedding", None))
            if doc_vec:
                sem = cosine_similarity_score(query_embedding, doc_vec)
        lex = _keyword_overlap(query, f"{r.title} {r.body} {r.tags}")
        score = w_sem * sem + w_lex * lex
        scored.append(
            {
                "chunk_id": r.id,
                "kind": r.kind,
                "ref_external_id": r.ref_external_id,
                "title": r.title,
                "snippet": (r.body or "")[:400],
                "service_name": r.service_name,
                "parent_service_name": r.parent_service_name,
                "clinic_name": r.clinic_name,
                "clinic_alias": r.clinic_alias,
                "doctor_name": r.doctor_name,
                "rating_value": r.rating_value,
                "source_page_url": r.source_page_url,
                "score": round(score, 4),
                "semantic_similarity": round(sem, 4),
                "lexical_overlap": round(lex, 4),
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {
        "ok": True,
        "query": query,
        "top_k": top_k,
        "embedding_ok": query_embedding is not None,
        "candidate_count": len(unique),
        "items": scored[:top_k],
    }


async def build_docdoc_rag_index_async(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(build_docdoc_rag_index, **kwargs)


async def search_docdoc_rag_async(query: str, **kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(search_docdoc_rag, query, **kwargs)

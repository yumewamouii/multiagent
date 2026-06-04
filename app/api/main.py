import asyncio
import json
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import app.agents.hierarchy as agents
import app.models.orm as models
import app.services.operations as services
import app.services.chat_orchestrator as chat_orchestrator
import app.services.docdoc_chat_router as docdoc_chat_router
import app.services.docdoc_chat_session as docdoc_chat_session
import app.services.docdoc_ingest as docdoc_ingest
import app.services.docdoc_jobs as docdoc_jobs
import app.services.docdoc_rag as docdoc_rag
import app.services.docdoc_reputation as docdoc_reputation
import app.services.docdoc_structured_research as docdoc_structured_research
import app.services.telegram_ingest as telegram_ingest
import app.services.telegram_jobs as telegram_jobs
from app.parsers.docdoc.crawl import crawl_docdoc
from app.api import schemas
from app.core.db import Base, engine, get_db
from app.rag.retrieval import semantic_search_chunks_scored, semantic_search_telegram_analyses_scored
from app.utils import observability

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)

    app.state.ingestion_stop_event = asyncio.Event()
    app.state.ingestion_task = None

    if os.getenv("ENABLE_BACKGROUND_INGESTION", "false").lower() == "true":
        app.state.ingestion_stop_event.clear()
        app.state.ingestion_task = asyncio.create_task(
            services.run_continuous_ingestion(app.state.ingestion_stop_event)
        )

    await agents.runtime.start()
    try:
        yield
    finally:
        task = app.state.ingestion_task
        if task is not None:
            app.state.ingestion_stop_event.set()
            await task
            app.state.ingestion_task = None
        await agents.runtime.stop()


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="Multi-agent Backend",
    version="0.5.0",
    lifespan=lifespan,
)


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles, which forbids browser caching for the whole /ui mount.

    During active UI development the JS files are rewritten frequently, and a
    cached copy of app.js silently breaks all dependent pages (no header nav,
    no data-source bar, no event handlers). no-store eliminates that class of
    bugs at the cost of one extra round trip per file.
    """

    async def get_response(self, path, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount("/ui", _NoCacheStaticFiles(directory=str(FRONTEND_DIR)), name="ui")


def _frontend_response(name: str) -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / name,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/")
def frontend_home():
    return _frontend_response("index.html")


@app.get("/dashboard")
def frontend_dashboard():
    return _frontend_response("dashboard.html")


@app.get("/chat")
def frontend_chat():
    return _frontend_response("chat.html")


@app.get("/search")
def frontend_search():
    return _frontend_response("search.html")


@app.get("/reputation")
def frontend_reputation():
    return _frontend_response("reputation.html")


@app.get("/methodology")
def frontend_methodology():
    return _frontend_response("methodology.html")


@app.get("/compare")
def frontend_compare():
    return _frontend_response("compare.html")


# ---------------------------
# health
# ---------------------------


@app.get("/health")
def healthcheck():

    return {"status": "ok"}


@app.get("/ingestion/status")
def ingestion_status():
    task = app.state.ingestion_task
    return {
        "enabled_by_env": os.getenv("ENABLE_BACKGROUND_INGESTION", "false").lower() == "true",
        "task_running": bool(task and not task.done()),
        "state": services.get_ingestion_state(),
    }


@app.post("/ingestion/start")
async def ingestion_start():
    task = app.state.ingestion_task
    if task is not None and not task.done():
        return {"status": "already_running"}
    app.state.ingestion_stop_event = asyncio.Event()
    app.state.ingestion_stop_event.clear()
    app.state.ingestion_task = asyncio.create_task(
        services.run_continuous_ingestion(app.state.ingestion_stop_event)
    )
    return {"status": "started"}


@app.post("/ingestion/stop")
async def ingestion_stop():
    task = app.state.ingestion_task
    if task is None or task.done():
        return {"status": "already_stopped"}
    app.state.ingestion_stop_event.set()
    await task
    app.state.ingestion_task = None
    return {"status": "stopped"}


# ---------------------------
# sources
# ---------------------------


@app.post(
    "/sources",
    response_model=schemas.SourceRead,
)
def create_source(
    payload: schemas.SourceCreate,
    db: Session = Depends(get_db),
):
    parser_aliases = {
        "wildberries": "wb",
        "wb_parser": "wb",
        "wildberries_parser": "wb",
    }
    raw_parser_type = (payload.parser_type or "").strip().lower()
    normalized_parser_type = parser_aliases.get(raw_parser_type, raw_parser_type or "html")

    exists = db.scalar(
        select(models.Source).where(
            models.Source.name == payload.name
        )
    )

    if exists:

        raise HTTPException(
            status_code=400,
            detail="source already exists",
        )

    source_data = payload.model_dump()
    source_data["parser_type"] = normalized_parser_type
    source = models.Source(**source_data)

    db.add(source)

    db.commit()

    db.refresh(source)

    return source


# ---------------------------
# manual review ingest
# ---------------------------


@app.post(
    "/reviews/ingest",
    response_model=schemas.ReviewRead,
)
def ingest_review(
    payload: schemas.ReviewIngest,
    db: Session = Depends(get_db),
):

    source = db.get(
        models.Source,
        payload.source_id,
    )

    if not source:

        raise HTTPException(
            status_code=404,
            detail="source not found",
        )

    review = models.Review(
        **payload.model_dump()
    )

    db.add(review)

    db.commit()

    db.refresh(review)

    services.create_knowledge_chunk(
        db,
        review,
        sentiment="neutral",
        summary=review.body[:200],
        tags="manual",
    )

    return review


# ---------------------------
# agents
# ---------------------------


@app.post(
    "/agents",
    response_model=schemas.AgentRead,
)
def create_agent(
    payload: schemas.AgentCreate,
    db: Session = Depends(get_db),
):

    exists = db.scalar(
        select(models.AgentProfile).where(
            models.AgentProfile.name
            == payload.name
        )
    )

    if exists:

        raise HTTPException(
            status_code=400,
            detail="agent already exists",
        )

    agent = models.AgentProfile(
        **payload.model_dump()
    )

    db.add(agent)

    db.commit()

    db.refresh(agent)

    return agent


# ---------------------------
# knowledge search
# ---------------------------


@app.get("/knowledge/search", response_model=schemas.KnowledgeSearchResponse)
async def search_knowledge(query: str = Query(min_length=2), top_k: int = 5):
    q_emb, scored = await semantic_search_chunks_scored(query, top_k=top_k)

    items = [
        schemas.KnowledgeSearchItem(
            chunk_id=chunk.id,
            review_id=chunk.review_id,
            similarity=round(sim, 4),
            product_name=chunk.review.product_name if chunk.review else None,
            summary=chunk.summary,
            sentiment=chunk.sentiment,
            tags=chunk.tags,
            review_text=chunk.review.body if chunk.review else None,
        )
        for chunk, sim in scored
    ]

    return schemas.KnowledgeSearchResponse(
        query=query,
        top_k=top_k,
        embedding_ok=q_emb is not None,
        items=items,
    )


# ---------------------------
# telegram chat export
# ---------------------------


def _resolve_telegram_export_path(export_path: str) -> Path:
    path = Path(export_path).expanduser().resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="export file not found")
    return path


@app.post("/telegram/export/parse", response_model=schemas.TelegramExportParseResponse)
def telegram_export_parse(payload: schemas.TelegramExportParseQuery):
    from app.parsers.telegram_export import parse_export_file

    path = _resolve_telegram_export_path(payload.export_path)
    data = parse_export_file(path, limit=payload.limit)
    return schemas.TelegramExportParseResponse(**data)


@app.post("/telegram/export/ingest")
def telegram_export_ingest(payload: schemas.TelegramExportIngest):
    path = _resolve_telegram_export_path(payload.export_path)
    limit = payload.limit if payload.limit not in (None, 0) else None

    if payload.run_in_background:
        job_id = telegram_jobs.start_ingest_job(
            path,
            limit=limit,
            heuristic_short_circuit=payload.heuristic_short_circuit,
        )
        return schemas.TelegramIngestJobResponse(
            job_id=job_id,
            status="queued",
            export_path=str(path),
            message="ingest_running_in_background",
        )

    try:
        return telegram_ingest.ingest_telegram_export_file(
            path,
            limit=limit,
            use_heuristic_short_circuit=payload.heuristic_short_circuit,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get(
    "/telegram/export/jobs/{job_id}",
    response_model=schemas.TelegramIngestJobStatusResponse,
)
def telegram_export_job_status(job_id: str):
    job = telegram_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return schemas.TelegramIngestJobStatusResponse(**job)


@app.post(
    "/telegram/export/search",
    response_model=schemas.TelegramSearchResponse,
)
async def telegram_export_search(payload: schemas.TelegramChatSearch):
    q_emb, scored = await semantic_search_telegram_analyses_scored(
        payload.query,
        top_k=payload.top_k,
        export_key=payload.export_key,
        market_research_only=payload.market_research_only,
        exclude_spam=payload.exclude_spam,
    )
    items: list[schemas.TelegramSearchItem] = []
    for row, sim in scored:
        items.append(
            schemas.TelegramSearchItem(
                row_id=row.id,
                similarity=round(float(sim), 4),
                telegram_message_id=row.telegram_message_id,
                message_date=row.message_date,
                author_name=row.author_name,
                summary=row.summary,
                topics=row.topics,
                spam_or_ad=row.spam_or_ad,
                market_research_interest=row.market_research_interest,
                body_preview=(row.body or "")[:400],
            )
        )
    return schemas.TelegramSearchResponse(
        query=payload.query,
        top_k=payload.top_k,
        embedding_ok=q_emb is not None,
        export_key_filter=payload.export_key,
        items=items,
    )


@app.post(
    "/telegram/export/topics",
    response_model=schemas.TelegramTopicsResponse,
)
def telegram_export_topics(
    payload: schemas.TelegramTopicsQuery,
    db: Session = Depends(get_db),
):
    data = telegram_ingest.aggregate_market_topics_data(db, export_key=payload.export_key)
    top = [schemas.TelegramTopicCount(topic=t, count=c) for t, c in data["top_topics"]]
    return schemas.TelegramTopicsResponse(
        export_key=payload.export_key,
        unique_topic_keys=data["unique_topic_keys"],
        messages_count=data["messages_count"],
        top_topics=top,
    )


# ---------------------------
# docdoc crawl + db
# ---------------------------


@app.post("/docdoc/crawl", response_model=schemas.DocdocCrawlResponse)
async def docdoc_crawl(payload: schemas.DocdocCrawlQuery):
    from app.parsers.docdoc.crawl_checkpoint import default_checkpoint_path
    from app.parsers.docdoc.fetch import DocDocFetchError

    max_svc = None if payload.max_services == 0 else payload.max_services
    max_cli = None if payload.max_clinics == 0 else payload.max_clinics
    checkpoint_path = default_checkpoint_path()

    if payload.run_in_background:
        job_id = docdoc_jobs.start_crawl_job(
            base_url=payload.base_url,
            max_services=max_svc,
            max_clinics=max_cli,
            max_doctor_profiles=payload.max_doctor_profiles,
            fetch_clinics=payload.fetch_clinics,
            full_reviews=payload.full_reviews,
            dual_review_pages=payload.dual_review_pages,
            discover_category_hubs=payload.discover_category_hubs,
            headless=payload.headless,
            save_to_db=payload.save_to_db,
            checkpoint_path=checkpoint_path,
        )
        return schemas.DocdocCrawlResponse(
            ok=True,
            job_id=job_id,
            checkpoint_path=str(checkpoint_path),
            stats={"message": "crawl_running_in_background"},
        )

    try:
        result = await asyncio.to_thread(
            crawl_docdoc,
            payload.base_url,
            max_services=max_svc,
            max_clinics=max_cli,
            max_doctor_profiles=payload.max_doctor_profiles,
            fetch_clinics=payload.fetch_clinics,
            full_reviews=payload.full_reviews,
            dual_review_pages=payload.dual_review_pages,
            discover_category_hubs=payload.discover_category_hubs,
            headless=payload.headless,
            checkpoint_path=checkpoint_path,
        )
    except DocDocFetchError as exc:
        from app.parsers.docdoc.crawl_checkpoint import load_crawl_checkpoint

        ck = load_crawl_checkpoint(checkpoint_path)
        detail = str(exc)
        if ck:
            detail += (
                f" | checkpoint: {checkpoint_path} status={ck.get('status')} "
                f"services={len(ck.get('services_parsed') or [])}"
            )
        raise HTTPException(status_code=503, detail=detail) from exc
    except Exception as exc:
        from app.parsers.docdoc.crawl_checkpoint import load_crawl_checkpoint

        ck = load_crawl_checkpoint(checkpoint_path)
        detail = str(exc)
        if ck:
            detail += f" | checkpoint: {checkpoint_path} status={ck.get('status')}"
        raise HTTPException(status_code=500, detail=detail) from exc

    if not result.get("ok"):
        return schemas.DocdocCrawlResponse(
            ok=False,
            error=result.get("error"),
            stats=result.get("stats"),
            checkpoint_path=str(checkpoint_path),
        )

    backup_path = Path(os.getenv("DOCDOC_CRAWL_BACKUP", "docdoc_crawl_last.json"))
    try:
        backup_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.warning("could not write crawl backup to %s", backup_path)

    db_inserted = None
    source_id = None
    source_name = None
    if payload.save_to_db:
        ing = docdoc_ingest.ingest_docdoc_crawl_result(result)
        db_inserted = ing.get("inserted")
        source_id = ing.get("source_id")
        source_name = ing.get("source_name")
    return schemas.DocdocCrawlResponse(
        ok=True,
        source_id=source_id,
        source_name=source_name,
        city_slug=result.get("city_slug"),
        stats=result.get("stats"),
        db_inserted=db_inserted,
        checkpoint_path=str(checkpoint_path),
    )


@app.get("/docdoc/crawl/jobs/{job_id}", response_model=schemas.DocdocCrawlJobStatusResponse)
def docdoc_crawl_job_status(job_id: str):
    job = docdoc_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return schemas.DocdocCrawlJobStatusResponse(**job)


@app.post("/docdoc/ingest-checkpoint")
def docdoc_ingest_checkpoint(payload: schemas.DocdocIngestCheckpointQuery):
    out = docdoc_jobs.ingest_checkpoint_file(
        payload.path,
        require_completed=not payload.allow_partial,
    )
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "ingest_failed"))
    ing = out.get("ingest") or {}
    return {
        "ok": True,
        "source_id": ing.get("source_id"),
        "inserted": ing.get("inserted"),
        "crawl_status": out.get("status"),
    }


@app.post(
    "/docdoc/rag/build",
    response_model=schemas.DocdocRagBuildResponse,
)
async def docdoc_rag_build(payload: schemas.DocdocRagBuildQuery):
    """
    Строит RAG-индекс DocDoc (отзывы, врачи, услуги) в таблице docdoc_chunks.
    Источник: БД (предпочтительно) или docdoc_crawl_last.json. Эмбеддинги — LM Studio.
    """
    result = await docdoc_rag.build_docdoc_rag_index_async(
        source=payload.source,
        crawl_path=payload.crawl_path,
        source_id=payload.source_id,
        city_slug=payload.city_slug,
        kinds=tuple(payload.kinds),
        skip_existing_embeddings=payload.skip_existing_embeddings,
        max_chunks=payload.max_chunks,
    )
    return schemas.DocdocRagBuildResponse(**result)


@app.post(
    "/docdoc/rag/search",
    response_model=schemas.DocdocRagSearchResponse,
)
async def docdoc_rag_search(payload: schemas.DocdocRagSearchQuery):
    """
    Гибридный (cosine + keyword) поиск по docdoc_chunks с фильтрами по городу,
    направлению, услуге, клинике, врачу.
    """
    result = await docdoc_rag.search_docdoc_rag_async(
        payload.query,
        top_k=payload.top_k,
        kinds=payload.kinds,
        city_slug=payload.city_slug,
        source_id=payload.source_id,
        service_name=payload.service_name,
        parent_service_name=payload.parent_service_name,
        clinic_alias=payload.clinic_alias,
        doctor_external_id=payload.doctor_external_id,
        candidate_k=payload.candidate_k,
        semantic_weight=payload.semantic_weight,
        lexical_weight=payload.lexical_weight,
    )
    return schemas.DocdocRagSearchResponse(**result)


@app.post(
    "/docdoc/reputation/analyze",
    response_model=schemas.DocdocReputationResponse,
)
async def docdoc_reputation_analyze(payload: schemas.DocdocReputationQuery):
    """
    Глубокий разбор одной сущности DocDoc (клиника / услуга / направление / врач):
    метрики, риск-отзывы, отчёт «репутационного аналитика», черновики ответов клиники.
    Поверх отзывов работает RAG из docdoc_chunks.
    """
    result = await asyncio.to_thread(
        docdoc_reputation.analyze_entity_reputation,
        entity_type=payload.entity_type,
        entity=payload.entity,
        source_id=payload.source_id,
        city_slug=payload.city_slug,
        crawl_path=payload.crawl_path,
        data_source=payload.data_source,
        use_rag=payload.use_rag,
        rag_top_k=payload.rag_top_k,
        rag_query=payload.rag_query,
        rag_kinds=payload.rag_kinds,
        reviews_in_prompt=payload.reviews_in_prompt,
        risk_reviews_count=payload.risk_reviews_count,
        use_llm=payload.use_llm,
        generate_reply_drafts=payload.generate_reply_drafts,
    )
    if not result.get("ok"):
        return schemas.DocdocReputationResponse(
            ok=False,
            error=result.get("error"),
            hint=result.get("hint"),
        )
    return schemas.DocdocReputationResponse(**result)


@app.post(
    "/docdoc/reputation/compare",
    response_model=schemas.DocdocReputationCompareResponse,
)
async def docdoc_reputation_compare(payload: schemas.DocdocReputationCompareQuery):
    """
    Сравнение 2–6 сущностей бок о бок, одним LLM-вызовом.
    Поддерживает однотипные сущности (entity_type + entities=[str, ...])
    и разнотипные mixed-режим (entities=[{"type":"clinic","value":"..."}, ...]).
    Каждая сущность получает собственные RAG-фрагменты и метрики.
    """
    entities_raw: list = []
    for e in payload.entities:
        if isinstance(e, schemas.DocdocCompareEntitySpec):
            entities_raw.append({"type": e.type, "value": e.value})
        else:
            entities_raw.append(e)
    scope_dict = (
        payload.scope.model_dump(exclude_none=True) if payload.scope is not None else None
    )
    result = await asyncio.to_thread(
        docdoc_reputation.compare_entities,
        entity_type=payload.entity_type,
        entities=entities_raw,
        scope=scope_dict,
        source_id=payload.source_id,
        city_slug=payload.city_slug,
        crawl_path=payload.crawl_path,
        data_source=payload.data_source,
        use_rag=payload.use_rag,
        rag_top_k=payload.rag_top_k,
        rag_query=payload.rag_query,
        rag_kinds=payload.rag_kinds,
        reviews_per_entity=payload.reviews_per_entity,
        use_llm=payload.use_llm,
    )
    if not result.get("ok"):
        return schemas.DocdocReputationCompareResponse(
            ok=False,
            error=result.get("error"),
            hint=result.get("hint"),
            not_found=result.get("not_found", []),
            found_entities=result.get("found_entities", []),
            scope_empty=result.get("scope_empty", []),
        )
    return schemas.DocdocReputationCompareResponse(**result)


@app.post(
    "/docdoc/chat",
    response_model=schemas.DocdocChatResponse,
)
async def docdoc_chat(payload: schemas.DocdocChatQuery):
    """
    Универсальная точка входа для пользовательского чата по DocDoc.
    Сначала определяет интент (Проанализируй / Сравни / Найди отзывы / fallback),
    извлекает сущности и зовёт нужный сервис.
    Поддерживает multi-turn: передайте session_id из предыдущего ответа,
    чтобы запрос «а теперь по клинике X» унаследовал тип сущности и intent.
    """
    result = await asyncio.to_thread(
        docdoc_chat_router.run_chat,
        payload.query,
        city_slug=payload.city_slug,
        source_id=payload.source_id,
        crawl_path=payload.crawl_path,
        use_llm=payload.use_llm,
        use_rag=payload.use_rag,
        session_id=payload.session_id,
    )
    return _build_chat_response(result)


def _build_chat_response(result: dict) -> schemas.DocdocChatResponse:
    intent_info = schemas.DocdocChatIntentInfo(**result["intent"])
    rep = result.get("reputation")
    cmp = result.get("compare")
    rag = result.get("rag")
    sess = result.get("session")
    return schemas.DocdocChatResponse(
        ok=bool(result.get("ok", True)),
        intent=intent_info,
        answer=result.get("answer", ""),
        reputation=schemas.DocdocReputationResponse(**rep) if isinstance(rep, dict) and rep.get("ok") else (
            schemas.DocdocReputationResponse(ok=False, error=(rep or {}).get("error"), hint=(rep or {}).get("hint"))
            if isinstance(rep, dict) else None
        ),
        compare=schemas.DocdocReputationCompareResponse(**cmp) if isinstance(cmp, dict) and cmp.get("ok") else (
            schemas.DocdocReputationCompareResponse(
                ok=False,
                error=(cmp or {}).get("error"),
                hint=(cmp or {}).get("hint"),
                not_found=(cmp or {}).get("not_found", []),
                found_entities=(cmp or {}).get("found_entities", []),
            ) if isinstance(cmp, dict) else None
        ),
        rag=schemas.DocdocRagSearchResponse(**rag) if isinstance(rag, dict) else None,
        session=schemas.DocdocChatSessionInfo(**sess) if isinstance(sess, dict) else None,
        hint=result.get("hint"),
        error=result.get("error"),
    )


@app.get(
    "/chat/session/{session_id}",
    response_model=schemas.DocdocChatSessionInfo,
)
def chat_session_get(session_id: str):
    """Получить состояние чат-сессии (контекст + историю реплик)."""
    sess = docdoc_chat_session.default_store.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return schemas.DocdocChatSessionInfo(**sess.to_dict())


@app.delete(
    "/chat/session/{session_id}",
    response_model=schemas.ChatSessionDeleteResponse,
)
def chat_session_delete(session_id: str):
    """Сбросить контекст чат-сессии (например, по кнопке «начать заново»)."""
    deleted = docdoc_chat_session.default_store.reset(session_id)
    return schemas.ChatSessionDeleteResponse(
        ok=True, session_id=session_id, deleted=deleted
    )


@app.get(
    "/chat/sessions",
    response_model=schemas.ChatSessionListResponse,
)
def chat_sessions_list():
    """Список активных session_id (для отладки)."""
    store = docdoc_chat_session.default_store
    with store._lock:
        ids = list(store._sessions.keys())
    return schemas.ChatSessionListResponse(ok=True, count=len(ids), session_ids=ids)


@app.post(
    "/chat/query",
    response_model=schemas.ChatOrchestratorResponse,
)
async def chat_query(payload: schemas.ChatOrchestratorQuery):
    """
    Топ-уровневая точка входа в чат: сама решает, кто отвечает —
    DocDoc-аналитика (репутация / сравнение / RAG по медицине)
    или общий MultiAgentRuntime (товары/telegram).
    Поддерживает multi-turn через session_id.
    """
    result = await chat_orchestrator.run_orchestrator_async(
        payload.query,
        session_id=payload.session_id,
        city_slug=payload.city_slug,
        source_id=payload.source_id,
        crawl_path=payload.crawl_path,
        top_k=payload.top_k,
        use_llm=payload.use_llm,
        use_rag=payload.use_rag,
        system_override=payload.system_override,
    )
    return _build_chat_orchestrator_response(result)


def _build_chat_orchestrator_response(result: dict) -> schemas.ChatOrchestratorResponse:
    top = result.get("top_route") or {}
    docdoc = result.get("docdoc")
    general = result.get("general")
    sess = result.get("session")
    docdoc_sub = None
    if isinstance(docdoc, dict):
        intent = docdoc.get("intent")
        rep = docdoc.get("reputation")
        cmp = docdoc.get("compare")
        rag = docdoc.get("rag")
        docdoc_sub = schemas.DocdocSubResponse(
            intent=schemas.DocdocChatIntentInfo(**intent) if isinstance(intent, dict) else None,
            reputation=schemas.DocdocReputationResponse(**rep) if isinstance(rep, dict) and rep.get("ok") else (
                schemas.DocdocReputationResponse(ok=False, error=(rep or {}).get("error"), hint=(rep or {}).get("hint"))
                if isinstance(rep, dict) else None
            ),
            compare=schemas.DocdocReputationCompareResponse(**cmp) if isinstance(cmp, dict) and cmp.get("ok") else (
                schemas.DocdocReputationCompareResponse(
                    ok=False,
                    error=(cmp or {}).get("error"),
                    hint=(cmp or {}).get("hint"),
                    not_found=(cmp or {}).get("not_found", []),
                    found_entities=(cmp or {}).get("found_entities", []),
                ) if isinstance(cmp, dict) else None
            ),
            rag=schemas.DocdocRagSearchResponse(**rag) if isinstance(rag, dict) else None,
            hint=docdoc.get("hint"),
        )
    general_sub = None
    if isinstance(general, dict):
        try:
            critic = general.get("critic") or {}
            general_sub = schemas.MultiAgentResponse(
                route=general.get("route", "product_lookup"),
                answer=general.get("answer", ""),
                confidence=float(critic.get("confidence", 0.0) or 0.0),
                critic_notes=str(critic.get("notes", "")),
                evidence=[
                    schemas.MultiAgentEvidence(**ev) for ev in (general.get("evidence") or []) if isinstance(ev, dict)
                ],
            )
        except Exception:
            general_sub = None
    return schemas.ChatOrchestratorResponse(
        ok=bool(result.get("ok", True)),
        top_route=schemas.TopRouteInfo(**top),
        answer=result.get("answer", ""),
        docdoc=docdoc_sub,
        general=general_sub,
        session=schemas.DocdocChatSessionInfo(**sess) if isinstance(sess, dict) else None,
    )


@app.post(
    "/docdoc/research/table",
    response_model=schemas.DocdocResearchTableResponse,
)
async def docdoc_research_table(payload: schemas.DocdocResearchTableQuery):
    """
    Структурный конкурентный / маркет-ресёрч по отзывам DocDoc: список объектов × поля → таблица.

    Объекты: клиники, услуги, категории (направления), врачи.
    Поля: метрики из отзывов + llm-интерпретация (жалобы, ЦА, реклама и т.д.).
    """
    result = await asyncio.to_thread(
        docdoc_structured_research.run_structured_research,
        entity_type=payload.entity_type,
        entities=payload.entities,
        field_keys=payload.fields,
        preset=payload.preset,
        limit=payload.limit,
        source_id=payload.source_id,
        city_slug=payload.city_slug,
        crawl_path=payload.crawl_path,
        reviews_per_entity=payload.reviews_per_entity,
        use_llm=payload.use_llm,
        match_each_entity=payload.match_each_entity,
        use_rag=payload.use_rag,
        rag_top_k=payload.rag_top_k,
        rag_query=payload.rag_query,
        rag_kinds=payload.rag_kinds,
    )
    if not result.get("ok"):
        return schemas.DocdocResearchTableResponse(
            ok=False,
            error=result.get("error"),
            hint=result.get("hint"),
        )
    return schemas.DocdocResearchTableResponse(**result)


@app.post(
    "/multiagent/query",
    response_model=schemas.MultiAgentResponse,
)
async def multiagent_query(payload: schemas.MultiAgentQuery):
    result = await agents.orchestrate(
        query=payload.query,
        top_k=payload.top_k,
    )

    critic = result["critic"]

    return {
        "route": result["route"],
        "answer": result["answer"],
        "confidence": critic["confidence"],
        "critic_notes": critic["notes"],
        "evidence": result["evidence"],
    }


@app.post(
    "/rag/query",
    response_model=schemas.RagResponse,
)
async def rag_query(payload: schemas.RagQuery):
    return await services.rag_answer(
        query=payload.query,
        top_k=payload.top_k,
    )


@app.post(
    "/insights/product",
    response_model=schemas.ProductInsightResponse,
)
async def product_insight(payload: schemas.ProductInsightQuery):
    return await agents.runtime.product_insight(
        product_name=payload.product_name,
        top_k=payload.top_k,
        source_id=payload.source_id,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )


@app.post(
    "/insights/dashboard",
    response_model=schemas.DashboardResponse,
)
def insights_dashboard(payload: schemas.DashboardQuery):
    return services.get_dashboard_insights(
        product_name=payload.product_name,
        source_id=payload.source_id,
        date_from=payload.date_from,
        date_to=payload.date_to,
        page=payload.page,
        page_size=payload.page_size,
    )


@app.post("/insights/dashboard/export")
def insights_dashboard_export(payload: schemas.DashboardQuery):
    report = services.get_dashboard_insights(
        product_name=payload.product_name,
        source_id=payload.source_id,
        date_from=payload.date_from,
        date_to=payload.date_to,
        page=payload.page,
        page_size=payload.page_size,
    )
    header = "run_id,product_name,source_id,confidence,created_at,summary"
    rows = [header]
    for item in report["items"]:
        summary = (item["summary"] or "").replace('"', "'").replace("\n", " ").strip()
        rows.append(
            f'{item["run_id"]},"{item["product_name"]}",{item["source_id"] or ""},{item["confidence"]},{item["created_at"]},"{summary}"'
        )
    csv_data = "\n".join(rows)
    return Response(content=csv_data, media_type="text/csv")


@app.post("/insights/dashboard/plot")
def insights_dashboard_plot(payload: schemas.DashboardQuery):
    series = services.get_dashboard_timeseries(
        product_name=payload.product_name,
        source_id=payload.source_id,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )
    try:
        png_data = observability.render_dashboard_plot(
            series,
            title="Insight runs and confidence trend",
        )
    except RuntimeError as exc:
        if str(exc) == "matplotlib_not_installed":
            raise HTTPException(
                status_code=503,
                detail="matplotlib is not installed. Install extra deps to enable plot export.",
            ) from exc
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="no data for selected filters")
    return Response(content=png_data, media_type="image/png")


@app.post(
    "/multiagent/query/async",
    response_model=schemas.MultiAgentJobAccepted,
)
async def multiagent_query_async(payload: schemas.MultiAgentQuery):
    job_id = await agents.runtime.submit(
        query=payload.query,
        top_k=payload.top_k,
    )
    return {
        "job_id": job_id,
        "status": "queued",
    }


@app.get(
    "/multiagent/jobs/{job_id}",
    response_model=schemas.MultiAgentJobStatus,
)
async def get_multiagent_job(job_id: str):
    job = agents.runtime.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    result_payload = None
    if job["result"] is not None:
        result_payload = {
            "route": job["result"]["route"],
            "answer": job["result"]["answer"],
            "confidence": job["result"]["critic"]["confidence"],
            "critic_notes": job["result"]["critic"]["notes"],
            "evidence": job["result"]["evidence"],
        }

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "error": job["error"],
        "result": result_payload,
    }


@app.get(
    "/reviews/{review_id}",
    response_model=schemas.ReviewRead,
)
def get_review(
    review_id: int,
    db: Session = Depends(get_db),
):
    review = db.scalar(
        select(models.Review).where(
            models.Review.id == review_id
        )
    )

    if not review:
        raise HTTPException(
            status_code=404,
            detail="review not found",
        )

    return review


@app.get(
    "/knowledge/{chunk_id}",
)
def get_knowledge_chunk(
    chunk_id: int,
    db: Session = Depends(get_db),
):
    chunk = db.get(models.KnowledgeChunk, chunk_id)

    if not chunk:
        raise HTTPException(
            status_code=404,
            detail="knowledge chunk not found",
        )

    return {
        "id": chunk.id,
        "review_id": chunk.review_id,
        "summary": chunk.summary,
        "sentiment": chunk.sentiment,
        "tags": chunk.tags,
        "embedding": chunk.embedding.tolist() if chunk.embedding is not None else None,
        "review_text": chunk.review.body if chunk.review else None,
    }
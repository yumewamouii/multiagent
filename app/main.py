import asyncio
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import agents
from app import models, schemas, services
from app.db import Base, engine, get_db


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


FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

app = FastAPI(
    title="Multi-agent Backend",
    version="0.5.0",
    lifespan=lifespan,
)

app.mount("/ui", StaticFiles(directory="app/frontend"), name="ui")


@app.get("/")
def frontend_home():
    return FileResponse("app/frontend/index.html")


@app.get("/dashboard")
def frontend_dashboard():
    return FileResponse("app/frontend/dashboard.html")


@app.get("/queries")
def frontend_queries():
    return FileResponse("app/frontend/queries.html")


# ---------------------------
# health
# ---------------------------


@app.get("/health")
def healthcheck():

    return {"status": "ok"}


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

    source = models.Source(
        **payload.model_dump()
    )

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


@app.get("/knowledge/search")
async def search_knowledge(query: str = Query(min_length=2), top_k: int = 5):
    results = await services.search_chunks(query, top_k=top_k)

    return [
        {
            "review_id": chunk.review_id,
            "product_name": chunk.review.product_name if chunk.review else None,
            "summary": chunk.summary,
            "sentiment": chunk.sentiment,
            "tags": chunk.tags,
            "review_text": chunk.review.body if chunk.review else None,
        }
        for chunk in results
    ]


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
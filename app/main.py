import asyncio

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import models, schemas, services
from app.db import Base, engine, get_db


app = FastAPI(
    title="Multi-agent Backend",
    version="0.5.0",
)


app.state.ingestion_task = None
app.state.ingestion_stop_event = asyncio.Event()


# ---------------------------
# DB init
# ---------------------------


@app.on_event("startup")
def init_db() -> None:

    if engine.dialect.name == "postgresql":

        with engine.begin() as connection:

            connection.execute(
                text(
                    "CREATE EXTENSION IF NOT EXISTS vector"
                )
            )

    Base.metadata.create_all(bind=engine)


# ---------------------------
# background ingestion
# ---------------------------


@app.on_event("startup")
async def start_background_ingestion():

    app.state.ingestion_stop_event.clear()

    app.state.ingestion_task = asyncio.create_task(
        services.run_continuous_ingestion(
            app.state.ingestion_stop_event
        )
    )

@app.on_event("shutdown")
async def stop_background_ingestion():

    task = app.state.ingestion_task

    if task is None:
        return

    app.state.ingestion_stop_event.set()

    await task

    app.state.ingestion_task = None


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
    # вызываем вашу асинхронную функцию поиска
    results = await services.search_chunks(query, top_k=top_k)

    # возвращаем удобный для клиента формат
    return [
        {
            "review_id": chunk.review_id,
            "summary": chunk.summary,
            "sentiment": chunk.sentiment,
            "tags": chunk.tags,
            "review text": chunk.review.body # если нужно вернуть
        }
        for chunk in results
    ]
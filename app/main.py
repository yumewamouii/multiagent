import asyncio

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import models, schemas, services
from app.db import Base, engine, get_db

app = FastAPI(title="Multi-agent Backend", version="0.4.1")

app.state.ingestion_task = None
app.state.ingestion_stop_event = asyncio.Event()


@app.on_event("startup")
def init_db() -> None:
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)


@app.on_event("startup")
async def start_background_ingestion() -> None:
    app.state.ingestion_stop_event.clear()
    app.state.ingestion_task = asyncio.create_task(
        services.run_continuous_ingestion(app.state.ingestion_stop_event)
    )


@app.on_event("shutdown")
async def stop_background_ingestion() -> None:
    task = app.state.ingestion_task
    if task is None:
        return

    app.state.ingestion_stop_event.set()
    await task
    app.state.ingestion_task = None


@app.get("/health")
def healthcheck():
    return {"status": "ok"}


@app.post("/sources", response_model=schemas.SourceRead)
def create_source(payload: schemas.SourceCreate, db: Session = Depends(get_db)):
    exists = db.scalar(select(models.Source).where(models.Source.name == payload.name))
    if exists:
        raise HTTPException(status_code=400, detail="source already exists")
    source = models.Source(**payload.model_dump())
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@app.post("/reviews/ingest", response_model=schemas.ReviewRead)
def ingest_review(payload: schemas.ReviewIngest, db: Session = Depends(get_db)):
    source = db.get(models.Source, payload.source_id)
    if not source:
        raise HTTPException(status_code=404, detail="source not found")

    review = models.Review(**payload.model_dump())
    db.add(review)
    db.commit()
    db.refresh(review)

    services.create_knowledge_chunk(db, review)
    return review


@app.post("/reviews/ingest-html", response_model=schemas.ReviewRead)
def ingest_review_html(payload: schemas.ReviewHtmlIngest, db: Session = Depends(get_db)):
    source = db.get(models.Source, payload.source_id)
    if not source:
        raise HTTPException(status_code=404, detail="source not found")

    ai_result = services.parse_html_with_gigachat(payload.html_page)
    review = models.Review(
        source_id=payload.source_id,
        external_id=payload.external_id,
        product_name=payload.product_name,
        author=payload.author,
        rating=float(ai_result["rating"]),
        body=str(ai_result["review_text"]),
    )
    db.add(review)
    db.commit()
    db.refresh(review)

    services.create_knowledge_chunk(
        db,
        review,
        sentiment=str(ai_result["sentiment"]),
        summary=str(ai_result["summary"]),
        tags=str(ai_result["tags"]),
    )
    return review


@app.post("/agents", response_model=schemas.AgentRead)
def create_agent(payload: schemas.AgentCreate, db: Session = Depends(get_db)):
    exists = db.scalar(select(models.AgentProfile).where(models.AgentProfile.name == payload.name))
    if exists:
        raise HTTPException(status_code=400, detail="agent already exists")

    agent = models.AgentProfile(**payload.model_dump())
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


@app.get("/knowledge/search", response_model=list[schemas.KnowledgeSearchResult])
def search_knowledge(query: str = Query(min_length=2), db: Session = Depends(get_db)):
    rows = services.search_knowledge(db, query)
    return [
        schemas.KnowledgeSearchResult(
            review_id=review.id,
            product_name=review.product_name,
            summary=chunk.summary,
            sentiment=chunk.sentiment,
            tags=chunk.tags,
        )
        for chunk, review in rows
    ]

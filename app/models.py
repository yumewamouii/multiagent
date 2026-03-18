from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(256), nullable=False)
    parser_type: Mapped[str] = mapped_column(String(64), nullable=False, default="html")

    reviews: Mapped[list["Review"]] = relationship(back_populates="source")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    author: Mapped[str] = mapped_column(String(128), nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    source: Mapped["Source"] = relationship(back_populates="reviews")
    knowledge_chunks: Mapped[list["KnowledgeChunk"]] = relationship(back_populates="review")


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment: Mapped[str] = mapped_column(String(32), nullable=False)
    tags: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

    review: Mapped["Review"] = relationship(back_populates="knowledge_chunks")


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="gigachat")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="GigaChat")
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    toolset: Mapped[str] = mapped_column(String(256), default="", nullable=False)

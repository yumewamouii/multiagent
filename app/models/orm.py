from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


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
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)

    review: Mapped["Review"] = relationship(back_populates="knowledge_chunks")


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="lmstudio")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="local-model")
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    toolset: Mapped[str] = mapped_column(String(256), default="", nullable=False)


class InsightRun(Base):
    __tablename__ = "insight_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    date_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    date_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class MCPEvent(Base):
    __tablename__ = "mcp_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("insight_runs.id"), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    to_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    intent: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DocdocService(Base):
    __tablename__ = "docdoc_services"
    __table_args__ = (UniqueConstraint("source_id", "external_service_id", name="uq_docdoc_service"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    external_service_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    page_url: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    parent_service_name: Mapped[str] = mapped_column(String(256), default="", nullable=False, index=True)
    category_direction: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    description_plain: Mapped[str] = mapped_column(Text, default="", nullable=False)
    city_slug: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    reviews_count_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DocdocClinic(Base):
    __tablename__ = "docdoc_clinics"
    __table_args__ = (UniqueConstraint("source_id", "clinic_alias", name="uq_docdoc_clinic"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    external_clinic_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    clinic_alias: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    page_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    full_address: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    city_slug: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DocdocDoctor(Base):
    __tablename__ = "docdoc_doctors"
    __table_args__ = (UniqueConstraint("source_id", "external_doctor_id", name="uq_docdoc_doctor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    external_doctor_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    doctor_alias: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    profile_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    speciality: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    total_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviews_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    address: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    service_external_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    service_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    parent_service_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    city_slug: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DocdocReview(Base):
    __tablename__ = "docdoc_reviews"
    __table_args__ = (UniqueConstraint("source_id", "external_review_id", name="uq_docdoc_review"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    external_review_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    service_external_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    clinic_alias: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    doctor_external_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    patient_public_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    doctor_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    clinic_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    service_name: Mapped[str] = mapped_column(String(512), default="", nullable=False, index=True)
    parent_service_name: Mapped[str] = mapped_column(String(256), default="", nullable=False, index=True)
    category_direction: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rating_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    rating_clinic: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_created: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    source_page_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DocdocChunk(Base):
    """Семантические чанки RAG по DocDoc (отзывы, врачи, услуги)."""

    __tablename__ = "docdoc_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "kind", "ref_external_id", name="uq_docdoc_chunk"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    ref_external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    city_slug: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    service_external_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    service_name: Mapped[str] = mapped_column(String(512), default="", nullable=False, index=True)
    parent_service_name: Mapped[str] = mapped_column(String(256), default="", nullable=False, index=True)
    clinic_alias: Mapped[str] = mapped_column(String(128), default="", nullable=False, index=True)
    clinic_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    doctor_external_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    doctor_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    rating_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_page_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TelegramChatAnalysis(Base):
    """Разбор текстовых сообщений экспорта Telegram: спам/реклама, темы исследований рынка, эмбеддинг."""

    __tablename__ = "telegram_chat_analyses"
    __table_args__ = (UniqueConstraint("export_key", "telegram_message_id", name="uq_tg_export_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    export_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    export_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    export_chat_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    author_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    author_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    spam_or_ad: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    spam_reason: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    market_research_interest: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    topics: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    analysis_source: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

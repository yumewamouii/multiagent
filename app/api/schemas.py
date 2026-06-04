from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceCreate(BaseModel):
    name: str
    base_url: str
    parser_type: str = "html"


class SourceRead(SourceCreate):
    id: int

    model_config = ConfigDict(from_attributes=True)


class ReviewIngest(BaseModel):
    source_id: int
    external_id: str
    product_name: str
    author: str
    rating: float = Field(ge=0, le=5)
    body: str


class ReviewHtmlIngest(BaseModel):
    source_id: int
    external_id: str
    product_name: str
    author: str
    html_page: str


class ReviewRead(ReviewIngest):
    id: int
    collected_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentCreate(BaseModel):
    name: str
    role: str
    provider: str = "lmstudio"
    model_name: str = "local-model"
    prompt_template: str
    toolset: str = ""


class AgentRead(AgentCreate):
    id: int

    model_config = ConfigDict(from_attributes=True)


class KnowledgeSearchResult(BaseModel):
    review_id: int
    product_name: str
    summary: str
    sentiment: str
    tags: str


class MultiAgentQuery(BaseModel):
    query: str = Field(min_length=2)
    top_k: int = Field(default=5, ge=1, le=20)


class MultiAgentEvidence(BaseModel):
    review_id: int
    product_name: str
    summary: str
    sentiment: str
    tags: str


class MultiAgentResponse(BaseModel):
    route: str
    answer: str
    confidence: float
    critic_notes: str
    evidence: list[MultiAgentEvidence]


class MultiAgentJobAccepted(BaseModel):
    job_id: str
    status: str


class MultiAgentJobStatus(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    error: str | None = None
    result: MultiAgentResponse | None = None


class KnowledgeSearchItem(BaseModel):
    chunk_id: int
    review_id: int
    similarity: float = Field(description="Cosine similarity запроса и эмбеддинга чанка, 0..1")
    product_name: str | None = None
    summary: str
    sentiment: str
    tags: str
    review_text: str | None = None


class KnowledgeSearchResponse(BaseModel):
    query: str
    top_k: int
    embedding_ok: bool
    items: list[KnowledgeSearchItem]


class RagQuery(BaseModel):
    query: str = Field(min_length=2)
    top_k: int = Field(default=5, ge=1, le=20)


class RagCitation(BaseModel):
    rank: int
    chunk_id: int | None = None
    review_id: int
    product_name: str
    summary: str
    sentiment: str
    tags: str
    source_id: int | None = None
    collected_at: str | None = None
    semantic_similarity: float | None = None
    rerank_score: float | None = None


class RagResponse(BaseModel):
    query: str
    answer: str
    citations: list[RagCitation]
    metrics: dict[str, int]


class MCPMessage(BaseModel):
    message_id: str
    from_agent: str
    to_agent: str
    intent: str
    payload: dict
    created_at: str


class ProductInsightQuery(BaseModel):
    product_name: str = Field(min_length=2)
    top_k: int = Field(default=8, ge=1, le=30)
    source_id: int | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class ProductInsightResponse(BaseModel):
    run_id: int | None = None
    product_name: str
    route: str
    summary: str
    rag_answer: str
    citations: list[RagCitation]
    metrics: dict[str, int]
    sentiment_breakdown: dict[str, int]
    top_tags: list[str]
    critic: dict
    roles: list[str]
    business_roles: list[str]
    tools: list[str]
    mcp_flow: list[MCPMessage]


class DashboardQuery(BaseModel):
    product_name: str | None = None
    source_id: int | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class DashboardItem(BaseModel):
    run_id: int
    product_name: str
    source_id: int | None = None
    summary: str
    confidence: float
    created_at: str


class DashboardResponse(BaseModel):
    total_runs: int
    page: int
    page_size: int
    total_pages: int
    avg_confidence: float
    kpi: dict[str, float]
    items: list[DashboardItem]


class TelegramExportIngest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "export_path": "C:\\Users\\you\\Documents\\GitHub\\multiagent\\result.json",
                    "limit": 50,
                    "heuristic_short_circuit": True,
                }
            ]
        }
    )

    export_path: str = Field(
        min_length=1,
        description="Абсолютный путь к JSON экспорта на машине, где запущен API (не путь клиента)",
    )
    limit: int | None = Field(
        default=0,
        ge=0,
        description="0 или null — все текстовые сообщения; иначе макс. число для обработки",
    )
    heuristic_short_circuit: bool = Field(
        default=True,
        description="Пропускать LLM/эмбеддинг при срабатывании эвристики спама",
    )
    run_in_background: bool = Field(
        default=False,
        description="true — job_id сразу, ingest в фоне (для больших result.json)",
    )


class TelegramExportParseQuery(BaseModel):
    export_path: str = Field(min_length=1)
    limit: int | None = Field(default=0, ge=0, description="0 = без лимита на превью парсинга")


class TelegramExportParseResponse(BaseModel):
    ok: bool
    export_key: str
    chat_id: int | None
    chat_name: str
    messages_parsed: int
    stats: dict[str, Any]
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Нормализованные сообщения (может быть урезано limit)",
    )


class TelegramIngestJobResponse(BaseModel):
    job_id: str
    status: str
    export_path: str
    message: str | None = None


class TelegramIngestJobStatusResponse(BaseModel):
    job_id: str
    status: str
    export_path: str | None = None
    updated_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class TelegramChatSearch(BaseModel):
    query: str = Field(min_length=2)
    top_k: int = Field(default=8, ge=1, le=50)
    export_key: str | None = None
    market_research_only: bool = True
    exclude_spam: bool = True


class TelegramSearchItem(BaseModel):
    row_id: int
    similarity: float
    telegram_message_id: int
    message_date: datetime | None
    author_name: str
    summary: str
    topics: str
    spam_or_ad: bool
    market_research_interest: bool
    body_preview: str


class TelegramSearchResponse(BaseModel):
    query: str
    top_k: int
    embedding_ok: bool
    export_key_filter: str | None
    items: list[TelegramSearchItem]


class TelegramTopicCount(BaseModel):
    topic: str
    count: int


class TelegramTopicsResponse(BaseModel):
    export_key: str | None
    unique_topic_keys: int
    messages_count: int
    top_topics: list[TelegramTopicCount]


class TelegramTopicsQuery(BaseModel):
    export_key: str | None = None


class DocdocCrawlQuery(BaseModel):
    base_url: str = Field(default="https://irk.docdoc.ru/", min_length=10)
    max_services: int = Field(default=10, ge=0, description="0 = все найденные услуги")
    max_clinics: int = Field(default=10, ge=0)
    max_doctor_profiles: int = Field(default=0, ge=0, description="0 = не открывать /doctor/...")
    fetch_clinics: bool = True
    full_reviews: bool = True
    dual_review_pages: bool = True
    discover_category_hubs: bool = Field(
        default=True,
        description="Собрать услуги с /service/stomatologiya и др. (~1100+), не только с главной (~177)",
    )
    save_to_db: bool = True
    headless: bool = True
    run_in_background: bool = Field(
        default=False,
        description="true — сразу job_id, краул в фоне (для 1000+ услуг, иначе HTTP обрывается)",
    )


class DocdocCrawlJobResponse(BaseModel):
    job_id: str
    status: str
    checkpoint_path: str
    message: str | None = None


class DocdocCrawlJobStatusResponse(BaseModel):
    job_id: str
    status: str
    checkpoint_path: str | None = None
    backup_path: str | None = None
    updated_at: str | None = None
    error: str | None = None
    result_stats: dict[str, Any] | None = None
    db_inserted: dict[str, int] | None = None
    city_slug: str | None = None


class DocdocIngestCheckpointQuery(BaseModel):
    path: str = Field(default="docdoc_crawl_checkpoint.json")
    allow_partial: bool = Field(
        default=True,
        description="Сохранить в БД даже если краул не дошёл до конца",
    )


class DocdocCrawlResponse(BaseModel):
    ok: bool
    source_id: int | None = None
    source_name: str | None = None
    city_slug: str | None = None
    stats: dict[str, Any] | None = None
    db_inserted: dict[str, int] | None = None
    error: str | None = None
    checkpoint_path: str | None = None
    job_id: str | None = None


class DocdocResearchTableQuery(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "entity_type": "clinic",
                    "limit": 10,
                    "preset": "clinic_competitors",
                    "entities": [
                        "Клиника Союз",
                        "Clean Clinic",
                    ],
                },
                {
                    "entity_type": "service",
                    "limit": 5,
                    "preset": "service_competitors",
                    "entities": ["Тонзиллор", "промывание миндалин"],
                    "crawl_path": "docdoc_crawl_last.json",
                },
            ]
        }
    )

    entity_type: Literal["clinic", "service", "category", "doctor"] = Field(
        default="clinic",
        description="Что сравниваем: клиники, услуги, категории (направления) или врачей",
    )
    entities: list[str] | None = Field(
        default=None,
        description="Список имён/подстрок для фильтра. Пусто — топ-N по числу отзывов",
    )
    limit: int = Field(default=10, ge=1, le=50)
    preset: str | None = Field(
        default="clinic_competitors",
        description="clinic_competitors | service_competitors | category_landscape",
    )
    fields: list[str] | None = Field(
        default=None,
        description="Ключи полей (metric + llm). Перекрывает preset, если задан",
    )
    source_id: int | None = None
    city_slug: str | None = Field(default=None, description="Например irk")
    crawl_path: str | None = Field(
        default=None,
        description="JSON краула вместо БД (docdoc_crawl_last.json)",
    )
    reviews_per_entity: int = Field(default=12, ge=3, le=30)
    use_llm: bool = Field(default=True, description="Заполнять llm-поля через локальную модель")
    match_each_entity: bool = Field(
        default=True,
        description="По одной строке таблицы на каждый элемент entities (лучшее совпадение в каталоге)",
    )
    use_rag: bool = Field(
        default=True,
        description="Подмешивать релевантные фрагменты из docdoc_chunks в LLM-промпт",
    )
    rag_top_k: int = Field(
        default=6,
        ge=0,
        le=30,
        description="Сколько чанков подмешивать на сущность (0 — выключить RAG)",
    )
    rag_query: str | None = Field(
        default=None,
        description="Переопределить запрос RAG. По умолчанию строится из preset/полей",
    )
    rag_kinds: list[ChunkKindLiteral] | None = Field(
        default=None,
        description="Какие виды чанков подмешивать (review/doctor/service). По умолчанию review (+ doctor/service если поля этого требуют).",
    )


class DocdocResearchColumn(BaseModel):
    key: str
    label: str
    kind: Literal["metric", "llm"]


class DocdocResearchTableRow(BaseModel):
    entity_id: str
    entity_name: str
    reviews_count: int
    cells: dict[str, Any]


class DocdocResearchRagInfo(BaseModel):
    used: bool
    top_k: int
    entities_with_snippets: int
    total_snippets: int


class DocdocResearchTableResponse(BaseModel):
    ok: bool
    entity_type: str | None = None
    preset: str | None = None
    data_source: str | None = None
    generated_at: str | None = None
    columns: list[DocdocResearchColumn] = Field(default_factory=list)
    rows: list[DocdocResearchTableRow] = Field(default_factory=list)
    rag: DocdocResearchRagInfo | None = None
    notes: str | None = None
    error: str | None = None
    hint: str | None = None


ChunkKindLiteral = Literal["review", "doctor", "service"]
ReputationEntityType = Literal["clinic", "service", "category", "doctor"]


class DocdocRagBuildQuery(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"source": "auto", "kinds": ["review", "doctor", "service"]},
                {"source": "json", "crawl_path": "docdoc_crawl_last.json", "kinds": ["review"]},
            ]
        }
    )

    source: Literal["db", "json", "auto"] = Field(
        default="auto",
        description="db | json | auto (БД, при пустой выборке fallback на JSON)",
    )
    crawl_path: str | None = Field(
        default=None,
        description="Используется для source=json/auto, по умолчанию docdoc_crawl_last.json",
    )
    source_id: int | None = None
    city_slug: str | None = Field(default=None, description="Например irk")
    kinds: list[ChunkKindLiteral] = Field(
        default_factory=lambda: ["review", "doctor", "service"],
        description="Что индексировать",
    )
    skip_existing_embeddings: bool = Field(
        default=True,
        description="Не пересчитывать эмбеддинги при повторном запуске",
    )
    max_chunks: int | None = Field(default=None, description="Жёсткий лимит на число чанков (для тестов)")


class DocdocRagBuildResponse(BaseModel):
    ok: bool
    source_used: str | None = None
    source_id: int | None = None
    city_slug: str | None = None
    counts: dict[str, int] | None = None
    error: str | None = None


class DocdocRagSearchQuery(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "длинные очереди и отношение администратора",
                    "kinds": ["review"],
                    "city_slug": "irk",
                    "top_k": 10,
                },
                {
                    "query": "врач, который хорошо объясняет и принимает детей",
                    "kinds": ["doctor", "review"],
                    "top_k": 8,
                },
            ]
        }
    )

    query: str = Field(min_length=2)
    kinds: list[ChunkKindLiteral] | None = Field(
        default=None,
        description="Подмножество: review/doctor/service. Пусто — все",
    )
    top_k: int = Field(default=10, ge=1, le=50)
    candidate_k: int | None = Field(default=None, ge=5, le=200)
    city_slug: str | None = None
    source_id: int | None = None
    service_name: str | None = Field(default=None, description="ILIKE по имени услуги")
    parent_service_name: str | None = Field(default=None, description="ILIKE по направлению")
    clinic_alias: str | None = None
    doctor_external_id: int | None = None
    semantic_weight: float | None = Field(default=None, ge=0, le=1)
    lexical_weight: float | None = Field(default=None, ge=0, le=1)


class DocdocRagSearchItem(BaseModel):
    chunk_id: int
    kind: str
    ref_external_id: str
    title: str
    snippet: str
    service_name: str | None = None
    parent_service_name: str | None = None
    clinic_name: str | None = None
    clinic_alias: str | None = None
    doctor_name: str | None = None
    rating_value: float | None = None
    source_page_url: str | None = None
    score: float
    semantic_similarity: float
    lexical_overlap: float


class DocdocRagSearchResponse(BaseModel):
    ok: bool
    query: str | None = None
    top_k: int | None = None
    embedding_ok: bool | None = None
    candidate_count: int | None = None
    items: list[DocdocRagSearchItem] = Field(default_factory=list)
    error: str | None = None


class DocdocReputationQuery(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "entity_type": "service",
                    "entity": "Промывание миндалин Тонзиллор",
                    "city_slug": "irk",
                    "use_rag": True,
                    "rag_top_k": 12,
                    "generate_reply_drafts": True,
                },
                {
                    "entity_type": "clinic",
                    "entity": "Клиника Союз",
                    "city_slug": "irk",
                    "use_rag": True,
                },
                {
                    "entity_type": "doctor",
                    "entity": "Иванов Иван Иванович",
                    "use_rag": True,
                    "rag_kinds": ["review", "doctor"],
                },
            ]
        }
    )

    entity_type: ReputationEntityType = Field(
        default="clinic",
        description="Что разбираем",
    )
    entity: str = Field(
        min_length=1,
        description="Имя/подстрока (для clinic — alias или название, для service — имя/название направления, для doctor — ФИО)",
    )
    source_id: int | None = None
    city_slug: str | None = None
    crawl_path: str | None = Field(
        default=None,
        description="JSON краула вместо БД (или fallback)",
    )
    data_source: Literal["db", "json", "auto"] = Field(default="auto")
    use_rag: bool = Field(default=True)
    rag_top_k: int = Field(default=12, ge=0, le=50)
    rag_query: str | None = None
    rag_kinds: list[ChunkKindLiteral] | None = Field(
        default=None,
        description="По умолчанию review (+ doctor/service по сигналу полей)",
    )
    reviews_in_prompt: int = Field(default=18, ge=3, le=60)
    risk_reviews_count: int = Field(default=5, ge=1, le=20)
    use_llm: bool = Field(default=True)
    generate_reply_drafts: bool = Field(default=True)


class DocdocReputationMetrics(BaseModel):
    reviews_count: int
    avg_rating: float | None = None
    median_rating: float | None = None
    p10_rating: float | None = None
    negative_share_pct: float | None = None
    unanswered_share_pct: float | None = None
    negative_unanswered_count: int | None = None
    doctors_mentioned: int | None = None
    latest_review: str | None = None


class DocdocReputationResponseStatus(BaseModel):
    total: int
    answered: int
    unanswered: int
    answered_share_pct: float | None = None


class DocdocReputationRiskReview(BaseModel):
    review_id: int | None = None
    rating: float | None = None
    answered: bool
    text: str
    doctor_name: str | None = None
    clinic_name: str | None = None
    service_name: str | None = None
    source_page_url: str | None = None


class DocdocReputationReport(BaseModel):
    executive_summary: str = ""
    what_patients_value: list[str] = Field(default_factory=list)
    top_complaints: list[str] = Field(default_factory=list)
    service_improvements: list[str] = Field(default_factory=list)
    landing_page_gaps: list[str] = Field(default_factory=list)
    ad_angle: str = ""
    target_audience: str = ""
    risk_topics: list[str] = Field(default_factory=list)


class DocdocReputationReplyDraft(BaseModel):
    review_id: int | None = None
    tone: str | None = None
    draft_reply: str = ""
    talking_points: list[str] = Field(default_factory=list)


class DocdocReputationRagInfo(BaseModel):
    used: bool
    top_k: int
    snippets_total: int
    snippets_negative: int
    default_query: str | None = None
    kinds: list[str] | None = None


class DocdocReputationResponse(BaseModel):
    ok: bool
    entity_type: str | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    data_source: str | None = None
    generated_at: str | None = None
    metrics: DocdocReputationMetrics | None = None
    response_status: DocdocReputationResponseStatus | None = None
    risk_reviews: list[DocdocReputationRiskReview] = Field(default_factory=list)
    report: DocdocReputationReport | None = None
    reply_drafts: list[DocdocReputationReplyDraft] = Field(default_factory=list)
    rag: DocdocReputationRagInfo | None = None
    llm_used: bool | None = None
    report_source: str | None = None
    llm_error: str | None = None
    notes: str | None = None
    error: str | None = None
    hint: str | None = None


class DocdocCompareEntitySpec(BaseModel):
    type: ReputationEntityType
    value: str = Field(min_length=1)


class DocdocCompareScope(BaseModel):
    """Контекст сравнения. Например, scope.service='Тонзиллор' при сравнении клиник
    оставит у каждой только отзывы по этой услуге, делая USP точечными."""
    service: str | None = None
    category: str | None = None
    clinic: str | None = None
    doctor: str | None = None


class DocdocReputationCompareQuery(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "entity_type": "clinic",
                    "entities": ["Клиника Союз", "Авиценна"],
                    "city_slug": "irk",
                    "use_rag": True,
                },
                {
                    "entity_type": "service",
                    "entities": ["Промывание миндалин Тонзиллор", "УЗИ щитовидной железы"],
                },
                {
                    "entities": [
                        {"type": "clinic", "value": "Клиника Союз"},
                        {"type": "service", "value": "Промывание миндалин Тонзиллор"},
                        {"type": "doctor", "value": "Иванов И.И."}
                    ],
                    "city_slug": "irk"
                },
                {
                    "entity_type": "clinic",
                    "entities": ["Клиника Союз", "Авиценна"],
                    "scope": {"service": "Промывание миндалин Тонзиллор"},
                    "city_slug": "irk"
                },
            ]
        }
    )

    entity_type: ReputationEntityType | None = Field(
        default=None,
        description="Общий тип, если все entities — строки. Не нужен в mixed-режиме",
    )
    entities: list[str | DocdocCompareEntitySpec] = Field(min_length=2, max_length=6)
    source_id: int | None = None
    city_slug: str | None = None
    crawl_path: str | None = None
    data_source: Literal["db", "json", "auto"] = Field(default="auto")
    use_rag: bool = Field(default=True)
    rag_top_k: int = Field(default=6, ge=0, le=30)
    rag_query: str | None = None
    rag_kinds: list[ChunkKindLiteral] | None = None
    reviews_per_entity: int = Field(default=10, ge=2, le=30)
    use_llm: bool = Field(default=True)
    scope: DocdocCompareScope | None = Field(
        default=None,
        description="Фильтр отзывов по контексту (например, по услуге для сравнения клиник)",
    )


class DocdocReputationCompareItem(BaseModel):
    entity_id: str
    entity_name: str | None = None
    entity_type: str | None = None
    metrics: DocdocReputationMetrics | None = None
    response_status: DocdocReputationResponseStatus | None = None
    rag_snippets_count: int = 0


class DocdocReputationComparePerEntity(BaseModel):
    entity_id: str
    entity_name: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    unique_selling_points: list[str] = Field(default_factory=list)


class DocdocReputationCompareWinners(BaseModel):
    avg_rating: str | None = None
    answer_rate: str | None = None
    review_volume: str | None = None


class DocdocReputationCompareBlock(BaseModel):
    summary: str = ""
    per_entity: list[DocdocReputationComparePerEntity] = Field(default_factory=list)
    shared_complaints: list[str] = Field(default_factory=list)
    ad_angle: str = ""
    winner_by_metric: DocdocReputationCompareWinners = Field(
        default_factory=DocdocReputationCompareWinners
    )


class DocdocReputationCompareResponse(BaseModel):
    ok: bool
    entity_type: str | None = None
    is_mixed: bool | None = None
    data_source: str | None = None
    generated_at: str | None = None
    items: list[DocdocReputationCompareItem] = Field(default_factory=list)
    compare: DocdocReputationCompareBlock | None = None
    metrics_winners: DocdocReputationCompareWinners | None = None
    not_found: list[DocdocCompareEntitySpec | str] = Field(default_factory=list)
    found_entities: list[str] = Field(default_factory=list)
    scope: dict[str, str] | None = None
    scope_empty: list[DocdocCompareEntitySpec] = Field(default_factory=list)
    llm_used: bool | None = None
    compare_source: str | None = None
    llm_error: str | None = None
    error: str | None = None
    hint: str | None = None


class DocdocChatQuery(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "Проанализируй отзывы по услуге 'Промывание миндалин Тонзиллор'. Что раздражает пациентов и что использовать в рекламе?"},
                {"query": "Сравни клинику Союз и Авиценна, что лучше"},
                {"query": "Что говорят про врача Иванов Иван Иванович?"},
                {
                    "query": "А теперь по клинике Авиценна",
                    "session_id": "<id from previous response>"
                },
            ]
        }
    )

    query: str = Field(min_length=2)
    session_id: str | None = Field(
        default=None,
        description="ID разговора. Если не передан, новая сессия будет создана и id вернётся в ответе.",
    )
    city_slug: str | None = None
    source_id: int | None = None
    crawl_path: str | None = None
    use_llm: bool = Field(default=True)
    use_rag: bool = Field(default=True)


class DocdocChatIntentInfo(BaseModel):
    intent: Literal["reputation_analyze", "reputation_compare", "rag_search", "fallback"]
    entity_type: str | None = None
    entities: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    rationale: str | None = None


class DocdocChatTurn(BaseModel):
    role: Literal["user", "bot"]
    content: str
    ts: str
    intent: str | None = None
    entity_type: str | None = None
    entities: list[str] = Field(default_factory=list)


class DocdocChatSessionInfo(BaseModel):
    session_id: str
    created_at: str
    updated_at: str
    last_intent: str | None = None
    last_entity_type: str | None = None
    last_entities: list[str] = Field(default_factory=list)
    last_city_slug: str | None = None
    history: list[DocdocChatTurn] = Field(default_factory=list)


class ChatSessionDeleteResponse(BaseModel):
    ok: bool
    session_id: str
    deleted: bool


class ChatSessionListResponse(BaseModel):
    ok: bool
    count: int
    session_ids: list[str] = Field(default_factory=list)


class DocdocChatResponse(BaseModel):
    ok: bool
    intent: DocdocChatIntentInfo
    answer: str = ""
    reputation: DocdocReputationResponse | None = None
    compare: DocdocReputationCompareResponse | None = None
    rag: DocdocRagSearchResponse | None = None
    session: DocdocChatSessionInfo | None = None
    error: str | None = None
    hint: str | None = None


class TopRouteInfo(BaseModel):
    system: Literal["docdoc", "general"]
    confidence: float = 0.0
    rationale: str | None = None


class DocdocSubResponse(BaseModel):
    intent: DocdocChatIntentInfo | None = None
    reputation: DocdocReputationResponse | None = None
    compare: DocdocReputationCompareResponse | None = None
    rag: DocdocRagSearchResponse | None = None
    hint: str | None = None


class ChatOrchestratorQuery(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "Проанализируй отзывы по услуге «Промывание миндалин Тонзиллор»"},
                {"query": "Сравни клинику Союз и Авиценна"},
                {"query": "Что говорят про iPhone в телеграм-канале?"},
                {"query": "А теперь по клинике Авиценна", "session_id": "<uuid>"},
            ]
        }
    )

    query: str = Field(min_length=2)
    session_id: str | None = None
    city_slug: str | None = None
    source_id: int | None = None
    crawl_path: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    use_llm: bool = Field(default=True)
    use_rag: bool = Field(default=True)
    system_override: Literal["docdoc", "general"] | None = Field(
        default=None,
        description="Принудительно выбрать систему (skip top-router)",
    )


class ChatOrchestratorResponse(BaseModel):
    ok: bool
    top_route: TopRouteInfo
    answer: str = ""
    docdoc: DocdocSubResponse | None = None
    general: MultiAgentResponse | None = None
    session: DocdocChatSessionInfo | None = None

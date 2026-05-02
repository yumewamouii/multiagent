from datetime import datetime

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
    provider: str = "gigachat"
    model_name: str = "GigaChat"
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


class RagQuery(BaseModel):
    query: str = Field(min_length=2)
    top_k: int = Field(default=5, ge=1, le=20)


class RagCitation(BaseModel):
    rank: int
    review_id: int
    product_name: str
    summary: str
    sentiment: str
    tags: str
    source_id: int | None = None
    collected_at: str | None = None


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

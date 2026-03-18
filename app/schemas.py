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

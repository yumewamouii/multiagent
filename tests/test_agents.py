import asyncio
from types import SimpleNamespace

from app import agents


def test_orchestrate_returns_expected_shape(monkeypatch):
    async def fake_search_chunks(query: str, top_k: int = 5):
        review = SimpleNamespace(product_name="Тестовый товар", body="Текст")
        chunk = SimpleNamespace(
            review_id=42,
            summary="Короткая выжимка",
            sentiment="positive",
            tags="удобство",
            review=review,
        )
        return [chunk]

    monkeypatch.setattr("app.agents.services.search_chunks", fake_search_chunks)

    result = asyncio.run(agents.orchestrate("что по товару", top_k=3))

    assert result["route"] == "product_lookup"
    assert result["critic"]["passed"] is True
    assert result["evidence"][0]["review_id"] == 42


def test_route_query_chooses_comparison():
    assert agents.route_query("что лучше: x vs y?") == "comparison"


def test_runtime_submit_creates_queued_job():
    runtime = agents.MultiAgentRuntime()
    job_id = asyncio.run(runtime.submit("test query", top_k=2))
    job = runtime.get_job(job_id)
    assert job is not None
    assert job["status"] == "queued"
    assert job["top_k"] == 2


def test_product_insight_uses_tools_and_mcp_flow():
    runtime = agents.MultiAgentRuntime()
    runtime._persist_insight_run = lambda **kwargs: None

    async def fake_rag(query: str, top_k: int = 8, source_id=None, date_from=None, date_to=None):
        return {
            "query": query,
            "answer": "LLM answer",
            "citations": [
                {
                    "rank": 1,
                    "review_id": 11,
                    "product_name": "Товар",
                    "summary": "Норм",
                    "sentiment": "positive",
                    "tags": "качество, цена",
                }
            ],
            "metrics": {"latency_ms": 10, "retrieved_candidates": 1, "vector_candidates": 1, "keyword_candidates": 0},
        }

    runtime.tools.register("rag_query", fake_rag)
    result = asyncio.run(runtime.product_insight("Товар", top_k=5))

    assert result["product_name"] == "Товар"
    assert result["sentiment_breakdown"]["positive"] == 1
    assert "качество" in result["top_tags"]
    assert len(result["mcp_flow"]) >= 4
    assert "market_analyst" in result["business_roles"]

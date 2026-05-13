import uuid

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_healthcheck():
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_ingest_pipeline_and_search():
    source = client.post(
        '/sources',
        json={
            'name': f'otzovik-test-{uuid.uuid4().hex[:8]}',
            'base_url': 'https://otzovik.com',
            'parser_type': 'html',
        },
    )
    assert source.status_code == 200
    source_id = source.json()['id']

    review = client.post(
        '/reviews/ingest',
        json={
            'source_id': source_id,
            'external_id': 'ext-1',
            'product_name': 'Тестовый чайник',
            'author': 'anna',
            'rating': 5,
            'body': 'Отличный чайник, очень хороший и удобный.',
        },
    )
    assert review.status_code == 200

    search = client.get('/knowledge/search', params={'query': 'чайник'})
    assert search.status_code == 200
    data = search.json()
    assert 'items' in data
    assert len(data['items']) >= 1
    assert data['items'][0]['product_name'] == 'Тестовый чайник'
    assert 'similarity' in data['items'][0]


def test_multiagent_query_contract(monkeypatch):
    async def fake_orchestrate(query: str, top_k: int = 5):
        return {
            "route": "product_lookup",
            "answer": f"Answer for {query}",
            "critic": {"confidence": 0.8, "notes": "ok"},
            "evidence": [
                {
                    "review_id": 1,
                    "product_name": "Тестовый чайник",
                    "summary": "Отличный",
                    "sentiment": "positive",
                    "tags": "качество",
                }
            ],
        }

    monkeypatch.setattr("app.main.agents.orchestrate", fake_orchestrate)

    response = client.post(
        "/multiagent/query",
        json={"query": "расскажи про чайник", "top_k": 3},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "product_lookup"
    assert payload["confidence"] == 0.8
    assert payload["critic_notes"] == "ok"
    assert payload["evidence"][0]["product_name"] == "Тестовый чайник"


def test_multiagent_async_submit_contract(monkeypatch):
    async def fake_submit(query: str, top_k: int = 5):
        assert query == "расскажи про чайник"
        assert top_k == 3
        return "job-123"

    monkeypatch.setattr("app.main.agents.runtime.submit", fake_submit)

    response = client.post(
        "/multiagent/query/async",
        json={"query": "расскажи про чайник", "top_k": 3},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == "job-123"
    assert payload["status"] == "queued"


def test_multiagent_job_status_contract(monkeypatch):
    fake_job = {
        "job_id": "job-321",
        "status": "completed",
        "created_at": "2026-05-03T00:00:00+00:00",
        "updated_at": "2026-05-03T00:00:10+00:00",
        "error": None,
        "result": {
            "route": "product_lookup",
            "critic": {"confidence": 0.9, "notes": "ok"},
            "answer": "test",
            "evidence": [],
        },
    }

    monkeypatch.setattr("app.main.agents.runtime.get_job", lambda _: fake_job)

    response = client.get("/multiagent/jobs/job-321")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["result"]["confidence"] == 0.9


def test_rag_query_contract(monkeypatch):
    async def fake_rag_answer(query: str, top_k: int = 5):
        assert query == "какие плюсы?"
        assert top_k == 2
        return {
            "query": query,
            "answer": "Плюсы: качество и удобство. [1]",
            "citations": [
                {
                    "rank": 1,
                    "review_id": 7,
                    "product_name": "Тестовый чайник",
                    "summary": "Хорошее качество",
                    "sentiment": "positive",
                    "tags": "качество",
                }
            ],
            "metrics": {
                "latency_ms": 12,
                "retrieved_candidates": 3,
                "vector_candidates": 2,
                "keyword_candidates": 1,
            },
        }

    monkeypatch.setattr("app.main.services.rag_answer", fake_rag_answer)
    response = client.post("/rag/query", json={"query": "какие плюсы?", "top_k": 2})
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "какие плюсы?"
    assert payload["citations"][0]["review_id"] == 7
    assert payload["metrics"]["latency_ms"] == 12


def test_product_insight_contract(monkeypatch):
    async def fake_product_insight(
        product_name: str,
        top_k: int = 8,
        source_id=None,
        date_from=None,
        date_to=None,
    ):
        assert product_name == "Чайник X"
        assert top_k == 6
        return {
            "run_id": 1,
            "product_name": product_name,
            "route": "product_lookup",
            "summary": "Краткий инсайт",
            "rag_answer": "Подробный ответ",
            "citations": [
                {
                    "rank": 1,
                    "review_id": 100,
                    "product_name": product_name,
                    "summary": "Хороший",
                    "sentiment": "positive",
                    "tags": "качество",
                }
            ],
            "metrics": {
                "latency_ms": 25,
                "retrieved_candidates": 5,
                "vector_candidates": 3,
                "keyword_candidates": 2,
            },
            "sentiment_breakdown": {"positive": 3, "neutral": 1, "negative": 1},
            "top_tags": ["качество", "цена"],
            "critic": {"passed": True, "confidence": 0.9, "notes": "ok"},
            "roles": ["router", "worker", "critic", "summarizer"],
            "business_roles": ["market_analyst", "campaign_advisor"],
            "tools": ["rag_query", "sentiment_breakdown", "top_tags"],
            "mcp_flow": [
                {
                    "message_id": "m-1",
                    "from_agent": "orchestrator",
                    "to_agent": "router",
                    "intent": "insight_request",
                    "payload": {"product_name": product_name},
                    "created_at": "2026-05-03T00:00:00+00:00",
                }
            ],
        }

    monkeypatch.setattr("app.main.agents.runtime.product_insight", fake_product_insight)
    response = client.post("/insights/product", json={"product_name": "Чайник X", "top_k": 6})
    assert response.status_code == 200
    payload = response.json()
    assert payload["product_name"] == "Чайник X"
    assert payload["sentiment_breakdown"]["positive"] == 3
    assert payload["mcp_flow"][0]["to_agent"] == "router"


def test_dashboard_contract(monkeypatch):
    def fake_dashboard(**kwargs):
        assert kwargs["page"] == 2
        assert kwargs["page_size"] == 5
        return {
            "total_runs": 1,
            "page": 2,
            "page_size": 5,
            "total_pages": 1,
            "avg_confidence": 0.88,
            "kpi": {
                "review_count": 10.0,
                "avg_rating": 4.2,
                "negative_ratio": 0.1,
                "positive_ratio": 0.7,
            },
            "items": [
                {
                    "run_id": 10,
                    "product_name": "Чайник X",
                    "source_id": 2,
                    "summary": "Инсайт",
                    "confidence": 0.88,
                    "created_at": "2026-05-03T00:00:00+00:00",
                }
            ],
        }

    monkeypatch.setattr("app.main.services.get_dashboard_insights", fake_dashboard)
    response = client.post("/insights/dashboard", json={"product_name": "Чайник", "page": 2, "page_size": 5})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_runs"] == 1
    assert payload["page"] == 2
    assert payload["kpi"]["avg_rating"] == 4.2
    assert payload["items"][0]["run_id"] == 10


def test_dashboard_export_contract(monkeypatch):
    def fake_dashboard(**kwargs):
        return {
            "total_runs": 1,
            "page": 1,
            "page_size": 20,
            "total_pages": 1,
            "avg_confidence": 0.88,
            "kpi": {
                "review_count": 1.0,
                "avg_rating": 5.0,
                "negative_ratio": 0.0,
                "positive_ratio": 1.0,
            },
            "items": [
                {
                    "run_id": 10,
                    "product_name": "Чайник X",
                    "source_id": 2,
                    "summary": "Инсайт",
                    "confidence": 0.88,
                    "created_at": "2026-05-03T00:00:00+00:00",
                }
            ],
        }

    monkeypatch.setattr("app.main.services.get_dashboard_insights", fake_dashboard)
    response = client.post("/insights/dashboard/export", json={"product_name": "Чайник"})
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "run_id,product_name" in response.text

import json
from unittest.mock import patch

from app.services.docdoc_structured_research import (
    _filters_for_entity,
    compute_metrics,
    group_reviews_from_crawl,
    resolve_fields,
    run_structured_research,
)


def _sample_crawl():
    return {
        "ok": True,
        "services": [],
        "reviews": [
            {
                "review_id": 1,
                "text": "Отличный врач, всё объяснил",
                "answer": "Спасибо",
                "rating_value": 10,
                "rating_clinic": 10,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "УЗИ",
                "parent_service_name": "Диагностика",
                "doctor_name": "Иванов",
                "created": "2025-06-01",
            },
            {
                "review_id": 2,
                "text": "Долго ждали, цена высокая",
                "answer": "",
                "rating_value": 4,
                "rating_clinic": 5,
                "clinic_name": "Клиника Б",
                "clinic_alias": "clinic_b",
                "service_name": "УЗИ",
                "parent_service_name": "Диагностика",
                "doctor_name": "Петров",
                "created": "2025-05-01",
            },
            {
                "review_id": 3,
                "text": "Всё быстро и понятно",
                "answer": "",
                "rating_value": 9,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "МРТ",
                "parent_service_name": "Диагностика",
                "created": "2025-07-01",
            },
        ],
    }


def test_compute_metrics_negative_and_unanswered():
    reviews = _sample_crawl()["reviews"]
    m = compute_metrics(reviews[:2])
    assert m["reviews_count"] == 2
    assert m["unanswered_share_pct"] == 50.0
    assert m["negative_share_pct"] == 50.0


def test_seed_services_without_global_reviews(tmp_path):
    crawl = {
        "ok": True,
        "services": [
            {
                "ok": True,
                "page_url": "https://irk.docdoc.ru/service/lor/Promivanie_mindalin_apparatom_Tonzillor",
                "service": {
                    "name": "Промывание миндалин аппаратом Тонзиллор",
                    "parent_service_name": "ЛОР",
                },
                "reviews": [],
            }
        ],
        "reviews": [],
    }
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")
    out = run_structured_research(
        entity_type="service",
        entities=["тонзиллор"],
        field_keys=["reviews_count"],
        crawl_path=str(path),
        use_llm=False,
    )
    assert out["ok"] is True
    assert len(out["rows"]) == 1
    assert out["rows"][0]["reviews_count"] == 0


def test_group_reviews_by_clinic():
    bundles = group_reviews_from_crawl(
        _sample_crawl(),
        "clinic",
        entities=None,
        limit=10,
    )
    assert len(bundles) == 2
    names = {b.entity_name for b in bundles}
    assert "Клиника А" in names
    assert "Клиника Б" in names


def test_resolve_fields_preset():
    fields = resolve_fields(None, preset="service_competitors")
    keys = [f.key for f in fields]
    assert "reviews_count" in keys
    assert "ad_angle" in keys


def test_run_structured_research_metrics_only(tmp_path):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")
    out = run_structured_research(
        entity_type="clinic",
        entities=["Клиника"],
        field_keys=["reviews_count", "avg_rating", "unanswered_share_pct"],
        preset=None,
        crawl_path=str(path),
        use_llm=False,
    )
    assert out["ok"] is True
    assert len(out["rows"]) >= 1
    row = out["rows"][0]
    assert row["cells"]["reviews_count"] >= 1


def test_run_structured_research_with_llm_mock(tmp_path):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")
    llm_response = """[
      {"entity_id": "clinic_a", "top_praises": "врач объясняет", "top_complaints": "ожидание", "ad_angle": "быстро и понятно"}
    ]"""

    with patch(
        "app.services.docdoc_structured_research.chat_completion",
        return_value=llm_response,
    ):
        out = run_structured_research(
            entity_type="clinic",
            entities=None,
            field_keys=["reviews_count", "top_praises", "ad_angle"],
            preset=None,
            limit=5,
            crawl_path=str(path),
            use_llm=True,
            use_rag=False,
        )
    assert out["ok"] is True
    row_a = next(r for r in out["rows"] if r["entity_id"] == "clinic_a")
    assert "врач" in row_a["cells"]["top_praises"].lower()


def test_filters_for_entity_clinic_uses_alias():
    f = _filters_for_entity({"clinic_alias": "clinic_a", "clinic_name": "Клиника А"}, "clinic")
    assert f == {"clinic_alias": "clinic_a"}


def test_filters_for_entity_doctor_prefers_external_id():
    f = _filters_for_entity({"doctor_id": 42, "doctor_name": "Иванов"}, "doctor")
    assert f == {"doctor_external_id": 42}
    f2 = _filters_for_entity({"doctor_id": None, "doctor_name": "Иванов"}, "doctor")
    assert f2 == {"doctor_name_like": "Иванов"}


def test_filters_for_entity_service_combines_name_parent():
    f = _filters_for_entity(
        {"service_name": "УЗИ", "parent_service_name": "Диагностика"},
        "service",
    )
    assert f == {"service_name": "УЗИ", "parent_service_name": "Диагностика"}


def test_run_structured_research_passes_rag_to_prompt(tmp_path):
    """RAG-снippets должны попадать в LLM-промпт и в поле rag ответа."""
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")

    captured_prompts: list[str] = []

    def fake_chat_completion(*, system_prompt: str, user_prompt: str, **kwargs):
        captured_prompts.append(user_prompt)
        return '[{"entity_id": "clinic_a", "top_complaints": "длинные очереди и цена"}]'

    fake_rag_calls: list[dict] = []

    def fake_search(query, **kwargs):
        fake_rag_calls.append({"query": query, **kwargs})
        if kwargs.get("clinic_alias") == "clinic_a":
            return {
                "ok": True,
                "items": [
                    {
                        "snippet": "Очередь была час, недовольны",
                        "title": "Клиника А — УЗИ",
                        "score": 0.91,
                        "rating_value": 4,
                    }
                ],
            }
        return {"ok": True, "items": []}

    with patch(
        "app.services.docdoc_structured_research.chat_completion",
        side_effect=fake_chat_completion,
    ):
        out = run_structured_research(
            entity_type="clinic",
            entities=None,
            field_keys=["reviews_count", "top_complaints"],
            preset=None,
            limit=5,
            crawl_path=str(path),
            use_llm=True,
            use_rag=True,
            rag_top_k=3,
            rag_search_fn=fake_search,
        )

    assert out["ok"] is True
    assert out["rag"]["used"] is True
    assert out["rag"]["entities_with_snippets"] == 1
    assert out["rag"]["total_snippets"] == 1
    assert any("Очередь была час" in p for p in captured_prompts)
    assert any("Релевантные фрагменты" in p for p in captured_prompts)
    # фильтр уехал в search правильным алиасом
    assert any(call.get("clinic_alias") == "clinic_a" for call in fake_rag_calls)
    # услуги из других клиник не должны были попасть в этот вызов RAG
    other = next(call for call in fake_rag_calls if call.get("clinic_alias") == "clinic_b")
    assert other["clinic_alias"] == "clinic_b"


def test_run_structured_research_rag_disabled(tmp_path):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")

    rag_called = []

    def fake_search(query, **kwargs):
        rag_called.append(query)
        return {"ok": True, "items": []}

    with patch(
        "app.services.docdoc_structured_research.chat_completion",
        return_value='[{"entity_id": "clinic_a", "top_complaints": "n/a"}]',
    ):
        out = run_structured_research(
            entity_type="clinic",
            field_keys=["top_complaints"],
            preset=None,
            crawl_path=str(path),
            use_llm=True,
            use_rag=False,
            rag_search_fn=fake_search,
        )
    assert out["ok"] is True
    assert out["rag"]["used"] is False
    assert rag_called == []

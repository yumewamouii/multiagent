import json

import pytest

from app.services.docdoc_reputation import (
    _detailed_metrics,
    _normalize_report,
    _parse_replies_payload,
    _pick_risk_reviews,
    _summarize_response_status,
    analyze_entity_reputation,
)
from app.services.docdoc_structured_research import _auto_rag_kinds


def _sample_crawl():
    return {
        "ok": True,
        "services": [],
        "reviews": [
            {
                "review_id": 1,
                "text": "Долго ждали приёма, очередь огромная",
                "answer": "",
                "rating_value": 4,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "УЗИ",
                "parent_service_name": "Диагностика",
                "doctor_name": "Иванов",
                "created": "2025-05-01",
                "source_page_url": "https://example.com/1",
            },
            {
                "review_id": 2,
                "text": "Врач замечательный, объяснил всё",
                "answer": "Спасибо за отзыв",
                "rating_value": 10,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "УЗИ",
                "parent_service_name": "Диагностика",
                "doctor_name": "Иванов",
                "created": "2025-06-01",
                "source_page_url": "https://example.com/2",
            },
            {
                "review_id": 3,
                "text": "Цена высокая для такой услуги, не понравилось отношение",
                "answer": "",
                "rating_value": 5,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "МРТ",
                "parent_service_name": "Диагностика",
                "created": "2025-07-01",
                "source_page_url": "https://example.com/3",
            },
            {
                "review_id": 4,
                "text": "Всё на высшем уровне",
                "answer": "",
                "rating_value": 9,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "МРТ",
                "parent_service_name": "Диагностика",
                "created": "2025-07-15",
                "source_page_url": "https://example.com/4",
            },
        ],
    }


def _sample_report_json():
    return json.dumps(
        {
            "executive_summary": "Клиника на 7/10. Сильны коммуникацией врачей, проблемы — очереди и цена.",
            "what_patients_value": ["объяснения врача", "профессионализм"],
            "top_complaints": ["длинные очереди", "высокая цена"],
            "service_improvements": ["оптимизировать слоты", "пересмотреть прайс"],
            "landing_page_gaps": ["добавить длительность процедуры"],
            "ad_angle": "Понятно и по делу — врач, который объясняет.",
            "target_audience": "Взрослые, ценящие компетентность и время",
            "risk_topics": ["очередь", "цена"],
        },
        ensure_ascii=False,
    )


def _sample_replies_json():
    return json.dumps(
        [
            {
                "review_id": 1,
                "tone": "empathetic",
                "draft_reply": "Сожалеем о длительном ожидании. Передадим обратную связь в регистратуру и вернёмся к вам.",
                "talking_points": ["извиниться", "разобраться с очередью"],
            },
            {
                "review_id": 3,
                "tone": "empathetic",
                "draft_reply": "Спасибо за обратную связь по цене и сервису. Постараемся улучшить впечатление от визита.",
                "talking_points": ["обсудить прайс", "улучшить отношение"],
            },
        ],
        ensure_ascii=False,
    )


def test_detailed_metrics_includes_negative_unanswered():
    crawl = _sample_crawl()
    m = _detailed_metrics(crawl["reviews"])
    assert m["reviews_count"] == 4
    assert m["unanswered_share_pct"] == 75.0
    assert m["negative_unanswered_count"] == 2  # rid=1 (4) и rid=3 (5), оба без ответа
    assert m["median_rating"] is not None


def test_response_status_counts():
    s = _summarize_response_status(_sample_crawl()["reviews"])
    assert s == {"total": 4, "answered": 1, "unanswered": 3, "answered_share_pct": 25.0}


def test_pick_risk_reviews_prioritizes_unanswered_low_rating():
    risk = _pick_risk_reviews(_sample_crawl()["reviews"], k=2)
    ids = [r["review_id"] for r in risk]
    assert ids == [1, 3]  # самые низкие и без ответа


def test_normalize_report_handles_missing_keys():
    out = _normalize_report({})
    assert out["executive_summary"] == ""
    assert out["what_patients_value"] == []
    assert out["top_complaints"] == []


def test_parse_replies_payload_strips_markdown():
    raw = "```json\n[{\"review_id\": 1, \"draft_reply\": \"x\"}]\n```"
    out = _parse_replies_payload(raw)
    assert len(out) == 1
    assert out[0]["review_id"] == 1


def test_auto_rag_kinds_adds_doctor_and_service():
    assert _auto_rag_kinds(["top_complaints"]) == ["review"]
    assert "doctor" in _auto_rag_kinds(["best_doctor"])
    assert "service" in _auto_rag_kinds(["service_description", "ad_angle"])


def test_analyze_entity_reputation_full_pipeline(tmp_path):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")

    captured_prompts: list[str] = []
    rag_calls: list[dict] = []

    def fake_chat(*, system_prompt: str, user_prompt: str, **kwargs):
        captured_prompts.append(user_prompt)
        if "Список риск-отзывов" in user_prompt:
            return _sample_replies_json()
        return _sample_report_json()

    def fake_rag(query, **kwargs):
        rag_calls.append({"query": query, **kwargs})
        return {
            "ok": True,
            "items": [
                {
                    "chunk_id": 100 + len(rag_calls),
                    "snippet": "Очередь, ждали час",
                    "title": "Клиника А — УЗИ",
                    "score": 0.9,
                    "rating_value": 4,
                }
            ],
        }

    out = analyze_entity_reputation(
        entity_type="clinic",
        entity="clinic_a",
        crawl_path=str(path),
        data_source="json",
        use_rag=True,
        rag_top_k=4,
        chat_completion_fn=fake_chat,
        rag_search_fn=fake_rag,
    )

    assert out["ok"] is True
    assert out["entity_id"] == "clinic_a"
    assert out["data_source"] in ("json", "json_fallback")
    # отчёт распарсен
    assert out["report"]["executive_summary"]
    assert "длинные очереди" in out["report"]["top_complaints"]
    # черновики ответов на риск-отзывы
    assert any(d.get("review_id") == 1 for d in out["reply_drafts"])
    # метрики и риск-отзывы возвращены
    assert out["metrics"]["reviews_count"] == 4
    assert out["risk_reviews"]
    # RAG вызвался дважды (общий + негатив)
    assert len(rag_calls) == 2
    assert any("Релевантные фрагменты RAG" in p for p in captured_prompts)
    # фильтры пробросились
    assert all(call.get("clinic_alias") == "clinic_a" for call in rag_calls)
    assert out["rag"]["used"] is True
    assert out["rag"]["snippets_total"] >= 1
    assert out["report_source"] == "llm"
    assert out["llm_used"] is True


def test_analyze_entity_reputation_heuristic_when_llm_fails(tmp_path):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")

    def bad_chat(**kwargs):
        raise RuntimeError("connection refused")

    out = analyze_entity_reputation(
        entity_type="clinic",
        entity="clinic_a",
        crawl_path=str(path),
        data_source="json",
        use_rag=False,
        use_llm=True,
        generate_reply_drafts=False,
        chat_completion_fn=bad_chat,
    )
    assert out["ok"] is True
    assert out["report_source"] == "heuristic"
    assert out["llm_used"] is False
    assert out["report"]["executive_summary"]
    complaints = out["report"]["top_complaints"]
    assert any("очеред" in c or "ожидан" in c for c in complaints)
    assert any("объясн" in c or "компетент" in c for c in out["report"]["what_patients_value"])
    for field in ("what_patients_value", "top_complaints", "risk_topics"):
        for item in out["report"][field]:
            assert not item.startswith("Я ")
            assert "…" not in item
    assert out["llm_error"]


def test_analyze_entity_reputation_no_match(tmp_path):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")
    out = analyze_entity_reputation(
        entity_type="clinic",
        entity="нет_такой_клиники_xyz",
        crawl_path=str(path),
        data_source="json",
        use_rag=False,
        use_llm=False,
    )
    assert out["ok"] is False
    assert out["error"] == "entity_not_found"


def test_analyze_entity_reputation_rag_off_skips_search(tmp_path):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")

    rag_calls: list[str] = []

    def fake_rag(query, **kwargs):
        rag_calls.append(query)
        return {"ok": True, "items": []}

    out = analyze_entity_reputation(
        entity_type="clinic",
        entity="clinic_a",
        crawl_path=str(path),
        data_source="json",
        use_rag=False,
        use_llm=False,
        chat_completion_fn=lambda **kw: _sample_report_json(),
        rag_search_fn=fake_rag,
    )
    assert out["ok"] is True
    assert rag_calls == []
    assert out["rag"]["used"] is False


@pytest.mark.parametrize("entity_type, entity, expect_id", [
    ("service", "УЗИ", "Диагностика/УЗИ"),
    ("category", "Диагностика", "Диагностика"),
])
def test_analyze_entity_reputation_works_for_other_types(tmp_path, entity_type, entity, expect_id):
    crawl = _sample_crawl()
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(crawl), encoding="utf-8")
    out = analyze_entity_reputation(
        entity_type=entity_type,
        entity=entity,
        crawl_path=str(path),
        data_source="json",
        use_rag=False,
        use_llm=False,
    )
    assert out["ok"] is True
    assert out["entity_id"] == expect_id

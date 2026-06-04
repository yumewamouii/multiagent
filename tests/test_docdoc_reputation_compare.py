import json

from app.services.docdoc_reputation import (
    _normalize_compare,
    _winners_from_metrics,
    compare_entities,
)


def _sample_crawl():
    return {
        "ok": True,
        "services": [],
        "reviews": [
            # Клиника А — много отзывов, средние оценки, низкий процент ответов
            *[
                {
                    "review_id": 100 + i,
                    "text": f"Отзыв клиника А #{i}, очередь была долгой" if i % 2 else f"Отзыв клиника А #{i}, врач хороший",
                    "answer": "" if i % 3 else "Спасибо",
                    "rating_value": 6 if i % 2 else 9,
                    "clinic_name": "Клиника А",
                    "clinic_alias": "clinic_a",
                    "service_name": "УЗИ",
                    "parent_service_name": "Диагностика",
                    "created": f"2025-05-0{(i % 9) + 1}",
                }
                for i in range(6)
            ],
            # Клиника B — меньше отзывов, выше рейтинг, выше процент ответов
            *[
                {
                    "review_id": 200 + i,
                    "text": f"Отзыв клиника Б #{i}, всё отлично",
                    "answer": "Благодарим за отзыв",
                    "rating_value": 10,
                    "clinic_name": "Клиника Б",
                    "clinic_alias": "clinic_b",
                    "service_name": "УЗИ",
                    "parent_service_name": "Диагностика",
                    "created": f"2025-05-0{(i % 9) + 1}",
                }
                for i in range(3)
            ],
        ],
    }


def _compare_llm_payload():
    return json.dumps(
        {
            "summary": "Клиника А лидирует по объёму, клиника Б — по сервису.",
            "per_entity": [
                {
                    "entity_id": "clinic_a",
                    "entity_name": "Клиника А",
                    "strengths": ["широкий поток пациентов"],
                    "weaknesses": ["длинные очереди"],
                    "unique_selling_points": ["опытные врачи"],
                },
                {
                    "entity_id": "clinic_b",
                    "entity_name": "Клиника Б",
                    "strengths": ["высокий рейтинг", "ответы на отзывы"],
                    "weaknesses": ["мало отзывов"],
                    "unique_selling_points": ["клиентский сервис"],
                },
            ],
            "shared_complaints": [],
            "ad_angle": "Клиника А — для тех, кому нужен опыт, Б — кому важен сервис.",
            "winner_by_metric": {
                "avg_rating": "clinic_b",
                "answer_rate": "clinic_b",
                "review_volume": "clinic_a",
            },
        },
        ensure_ascii=False,
    )


def test_winners_from_metrics_pure():
    items = [
        {
            "bundle": type("B", (), {"entity_id": "a"})(),
            "metrics": {"avg_rating": 7.5, "reviews_count": 10},
            "response_status": {"answered_share_pct": 30.0},
        },
        {
            "bundle": type("B", (), {"entity_id": "b"})(),
            "metrics": {"avg_rating": 9.5, "reviews_count": 4},
            "response_status": {"answered_share_pct": 80.0},
        },
    ]
    w = _winners_from_metrics(items)
    assert w == {"avg_rating": "b", "answer_rate": "b", "review_volume": "a"}


def test_normalize_compare_handles_missing_fields():
    out = _normalize_compare({"summary": "ok"}, ["a", "b"])
    assert out["summary"] == "ok"
    assert len(out["per_entity"]) == 2
    assert all(p["entity_id"] in {"a", "b"} for p in out["per_entity"])
    assert out["winner_by_metric"] == {"avg_rating": None, "answer_rate": None, "review_volume": None}


def test_compare_entities_full(tmp_path):
    crawl = _sample_crawl()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")

    captured: list[str] = []
    rag_calls: list[dict] = []

    def fake_chat(*, system_prompt, user_prompt, **kwargs):
        captured.append(user_prompt)
        return _compare_llm_payload()

    def fake_rag(query, **kwargs):
        rag_calls.append({"query": query, **kwargs})
        return {"ok": True, "items": [{"chunk_id": 1, "snippet": "пример", "title": kwargs.get("clinic_alias"), "score": 0.9}]}

    out = compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "clinic_b"],
        crawl_path=str(p),
        data_source="json",
        use_rag=True,
        rag_top_k=3,
        chat_completion_fn=fake_chat,
        rag_search_fn=fake_rag,
    )
    assert out["ok"] is True
    ids = [it["entity_id"] for it in out["items"]]
    assert ids == ["clinic_a", "clinic_b"]
    # один промпт, обе клиники в нём
    assert len(captured) == 1
    assert "clinic_a" in captured[0] and "clinic_b" in captured[0]
    # RAG вызывался по разу на сущность с правильными фильтрами
    assert len(rag_calls) == 2
    aliases = [c["clinic_alias"] for c in rag_calls]
    assert sorted(aliases) == ["clinic_a", "clinic_b"]
    # compare-блок распарсился
    cb = out["compare"]
    assert cb["summary"]
    assert len(cb["per_entity"]) == 2
    assert cb["winner_by_metric"]["avg_rating"] == "Клиника Б"
    assert out["compare_source"] == "llm"
    # фактические победители тоже есть
    assert out["metrics_winners"]["review_volume"] == "clinic_a"
    assert out["metrics_winners"]["answer_rate"] == "clinic_b"


def test_compare_entities_needs_two():
    out = compare_entities(entities=["x"], use_llm=False, use_rag=False)
    assert out["ok"] is False
    assert out["error"] == "need_at_least_two_entities"


def test_compare_entities_too_many():
    out = compare_entities(entities=[f"x{i}" for i in range(7)], use_llm=False, use_rag=False)
    assert out["ok"] is False
    assert out["error"] == "too_many_entities"


def test_compare_entities_not_enough_matches(tmp_path):
    crawl = _sample_crawl()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")
    out = compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "несуществующая_клиника_zzz"],
        crawl_path=str(p),
        data_source="json",
        use_llm=False,
        use_rag=False,
    )
    assert out["ok"] is False
    assert out["error"] == "not_enough_matches"
    assert "clinic_a" in out["found_entities"]
    assert any(
        nf == "несуществующая_клиника_zzz"
        or (isinstance(nf, dict) and nf.get("value") == "несуществующая_клиника_zzz")
        for nf in out["not_found"]
    )


def test_compare_entities_metrics_only_no_llm(tmp_path):
    crawl = _sample_crawl()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")
    out = compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "clinic_b"],
        crawl_path=str(p),
        data_source="json",
        use_rag=False,
        use_llm=False,
    )
    assert out["ok"] is True
    assert out["llm_used"] is False
    assert out["compare_source"] == "heuristic"
    cb = out["compare"]
    assert cb["summary"]
    assert cb["per_entity"][0]["strengths"]
    assert cb["ad_angle"]
    for pe in cb["per_entity"]:
        for item in (pe.get("strengths") or []) + (pe.get("weaknesses") or []):
            assert not item.startswith("Отзыв ")
            assert "…" not in item
    # метрики-победители — id; в compare — русские имена
    assert out["metrics_winners"]["review_volume"] == "clinic_a"
    assert cb["winner_by_metric"]["avg_rating"] in {"Клиника А", "Клиника Б"}
    assert "_" not in (cb["winner_by_metric"]["review_volume"] or "")


def test_compare_entities_heuristic_when_llm_fails(tmp_path):
    crawl = _sample_crawl()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")

    def bad_chat(**kwargs):
        raise RuntimeError("connection refused")

    out = compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "clinic_b"],
        crawl_path=str(p),
        data_source="json",
        use_rag=False,
        use_llm=True,
        chat_completion_fn=bad_chat,
    )
    assert out["ok"] is True
    assert out["compare_source"] == "heuristic"
    assert out["llm_error"]
    assert out["compare"]["per_entity"][0]["strengths"]
    assert out["compare"]["winner_by_metric"]["review_volume"] == "Клиника А"

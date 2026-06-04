import json

from app.services.docdoc_reputation import _normalize_compare_entities, compare_entities


def _sample_crawl():
    return {
        "ok": True,
        "services": [
            {
                "service_name": "Промывание миндалин",
                "parent_service_name": "ЛОР",
                "url": "/services/promyvanie",
            }
        ],
        "reviews": [
            {
                "review_id": 1,
                "text": "Клиника А хорошая",
                "answer": "Спасибо",
                "rating_value": 9,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "УЗИ",
                "parent_service_name": "Диагностика",
                "doctor_name": "Иванов",
                "created": "2025-05-01",
            },
            {
                "review_id": 2,
                "text": "Промывание прошло без боли",
                "answer": "",
                "rating_value": 8,
                "clinic_name": "Клиника Б",
                "clinic_alias": "clinic_b",
                "service_name": "Промывание миндалин",
                "parent_service_name": "ЛОР",
                "created": "2025-05-02",
            },
            {
                "review_id": 3,
                "text": "Доктор Иванов внимательный",
                "answer": "",
                "rating_value": 10,
                "clinic_name": "Клиника А",
                "clinic_alias": "clinic_a",
                "service_name": "УЗИ",
                "parent_service_name": "Диагностика",
                "doctor_name": "Иванов",
                "created": "2025-05-03",
            },
        ],
    }


def test_normalize_compare_entities_string_requires_type():
    specs, err = _normalize_compare_entities(["a", "b"], None)
    assert specs == []
    assert err and err["error"] == "entity_type_required_for_string_entities"


def test_normalize_compare_entities_with_dicts_skips_invalid():
    specs, err = _normalize_compare_entities(
        [
            {"type": "clinic", "value": "Союз"},
            {"type": "service", "value": "УЗИ"},
            {"type": "doctor", "value": ""},
        ],
        None,
    )
    assert err is None
    assert specs == [("clinic", "Союз"), ("service", "УЗИ")]


def test_normalize_compare_entities_string_uses_common_type():
    specs, err = _normalize_compare_entities(["A", "B"], "service")
    assert err is None
    assert specs == [("service", "A"), ("service", "B")]


def test_compare_entities_mixed_types(tmp_path):
    crawl = _sample_crawl()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")

    captured: list[str] = []

    def fake_chat(*, system_prompt, user_prompt, **kwargs):
        captured.append(user_prompt)
        return json.dumps(
            {
                "summary": "Разные срезы: клиника, услуга, врач — у каждого своя сила",
                "per_entity": [
                    {"entity_id": "clinic_a", "entity_name": "Клиника А", "strengths": ["опыт"], "weaknesses": []},
                    {"entity_id": "ЛОР/Промывание миндалин", "entity_name": "Промывание миндалин", "strengths": ["безболезненно"], "weaknesses": []},
                    {"entity_id": "Иванов", "entity_name": "Иванов", "strengths": ["внимательный"], "weaknesses": []},
                ],
                "shared_complaints": [],
                "ad_angle": "клиника — опыт, услуга — без боли, врач — внимание",
                "winner_by_metric": {"avg_rating": "Иванов", "answer_rate": None, "review_volume": None},
            },
            ensure_ascii=False,
        )

    out = compare_entities(
        entity_type=None,
        entities=[
            {"type": "clinic", "value": "clinic_a"},
            {"type": "service", "value": "Промывание миндалин"},
            {"type": "doctor", "value": "Иванов"},
        ],
        crawl_path=str(p),
        data_source="json",
        use_rag=False,
        chat_completion_fn=fake_chat,
    )
    assert out["ok"] is True
    assert out["is_mixed"] is True
    types = sorted(it["entity_type"] for it in out["items"])
    assert types == ["clinic", "doctor", "service"]
    # промпт получил упоминание разных типов
    assert "Тип объектов: разнотипные" in captured[0]
    assert "Тип: clinic" in captured[0]
    assert "Тип: service" in captured[0]
    assert "Тип: doctor" in captured[0]
    # entity_type у общего ответа должен быть None в mixed-режиме
    assert out["entity_type"] is None


def test_compare_entities_string_list_remains_single_type(tmp_path):
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
    assert out["is_mixed"] is False
    assert out["entity_type"] == "clinic"


def test_compare_entities_missing_type_for_strings_returns_error():
    out = compare_entities(
        entity_type=None,
        entities=["x", "y"],
        use_rag=False,
        use_llm=False,
    )
    assert out["ok"] is False
    assert out["error"] == "entity_type_required_for_string_entities"


def _crawl_with_service_scope():
    """Клиника А и Б, но обе обслуживают услугу 'Промывание миндалин' и УЗИ."""
    rev_a_promyv = [
        {
            "review_id": 100 + i,
            "text": f"Клиника А промывание #{i}: всё прошло гладко",
            "answer": "" if i % 2 else "Спасибо",
            "rating_value": 9,
            "clinic_name": "Клиника А",
            "clinic_alias": "clinic_a",
            "service_name": "Промывание миндалин Тонзиллор",
            "parent_service_name": "ЛОР",
            "created": "2025-05-01",
        }
        for i in range(3)
    ]
    rev_a_uzi = [
        {
            "review_id": 200 + i,
            "text": f"Клиника А узи #{i}",
            "answer": "Спасибо",
            "rating_value": 8,
            "clinic_name": "Клиника А",
            "clinic_alias": "clinic_a",
            "service_name": "УЗИ щитовидной железы",
            "parent_service_name": "Диагностика",
            "created": "2025-05-02",
        }
        for i in range(4)
    ]
    rev_b_promyv = [
        {
            "review_id": 300 + i,
            "text": f"Клиника Б промывание #{i}: было больно",
            "answer": "",
            "rating_value": 5,
            "clinic_name": "Клиника Б",
            "clinic_alias": "clinic_b",
            "service_name": "Промывание миндалин Тонзиллор",
            "parent_service_name": "ЛОР",
            "created": "2025-05-03",
        }
        for i in range(2)
    ]
    rev_b_other = [
        {
            "review_id": 400 + i,
            "text": f"Клиника Б другое #{i}",
            "answer": "",
            "rating_value": 10,
            "clinic_name": "Клиника Б",
            "clinic_alias": "clinic_b",
            "service_name": "Гастроскопия",
            "parent_service_name": "Диагностика",
            "created": "2025-05-04",
        }
        for i in range(3)
    ]
    return {
        "ok": True,
        "services": [],
        "reviews": rev_a_promyv + rev_a_uzi + rev_b_promyv + rev_b_other,
    }


def test_compare_scope_filters_reviews_by_service(tmp_path):
    crawl = _crawl_with_service_scope()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")

    out = compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "clinic_b"],
        crawl_path=str(p),
        data_source="json",
        use_rag=False,
        use_llm=False,
        scope={"service": "Промывание миндалин"},
    )
    assert out["ok"] is True
    assert out["scope"] == {"service": "Промывание миндалин"}
    by_id = {it["entity_id"]: it for it in out["items"]}
    # У А было 7 (3 промывание + 4 узи), теперь только 3
    a_metrics = by_id["clinic_a"]["metrics"]
    assert a_metrics["reviews_count"] == 3
    assert a_metrics["reviews_before_scope"] == 7
    # У Б было 5 (2 промывание + 3 другие), теперь 2
    b_metrics = by_id["clinic_b"]["metrics"]
    assert b_metrics["reviews_count"] == 2
    assert b_metrics["reviews_before_scope"] == 5


def test_compare_scope_passes_to_prompt(tmp_path):
    crawl = _crawl_with_service_scope()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")
    captured: list[str] = []

    def fake_chat(*, system_prompt, user_prompt, **kwargs):
        captured.append(user_prompt)
        return json.dumps({
            "summary": "ok", "per_entity": [], "shared_complaints": [], "ad_angle": "",
            "winner_by_metric": {}
        }, ensure_ascii=False)

    compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "clinic_b"],
        crawl_path=str(p),
        data_source="json",
        use_rag=False,
        use_llm=True,
        scope={"service": "Промывание миндалин"},
        chat_completion_fn=fake_chat,
    )
    assert captured
    assert "scope" in captured[0].lower()
    assert "Промывание миндалин" in captured[0]


def test_compare_scope_too_strict_returns_error(tmp_path):
    crawl = _crawl_with_service_scope()
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")

    out = compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "clinic_b"],
        crawl_path=str(p),
        data_source="json",
        use_rag=False,
        use_llm=False,
        scope={"service": "несуществующая_услуга_xyz"},
    )
    assert out["ok"] is False
    assert out["error"] == "scope_filtered_out"
    assert len(out["scope_empty"]) == 2
    assert all(it["type"] == "clinic" for it in out["scope_empty"])
    assert "scope" in (out.get("hint") or "").lower()


def test_compare_scope_partial_match_keeps_remaining(tmp_path):
    """Если у одной клиники остались отзывы, у другой нет — но >=2 entities остались, не падаем."""
    crawl = _crawl_with_service_scope()
    # добавим клинику C с одним промыванием
    crawl["reviews"].append(
        {
            "review_id": 999,
            "text": "Клиника C промывание",
            "answer": "",
            "rating_value": 9,
            "clinic_name": "Клиника C",
            "clinic_alias": "clinic_c",
            "service_name": "Промывание миндалин Тонзиллор",
            "parent_service_name": "ЛОР",
            "created": "2025-05-05",
        }
    )
    p = tmp_path / "crawl.json"
    p.write_text(json.dumps(crawl), encoding="utf-8")

    out = compare_entities(
        entity_type="clinic",
        entities=["clinic_a", "clinic_b", "clinic_c"],
        crawl_path=str(p),
        data_source="json",
        use_rag=False,
        use_llm=False,
        scope={"service": "Промывание миндалин"},
    )
    assert out["ok"] is True
    assert len(out["items"]) == 3

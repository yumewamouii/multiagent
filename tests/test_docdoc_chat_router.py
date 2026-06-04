from app.services.docdoc_chat_router import (
    _format_compare_answer,
    _format_rag_answer,
    _format_reputation_answer,
    _heuristic_intent,
    detect_intent,
    run_chat,
)


def test_heuristic_analyze_with_quoted_service():
    out = _heuristic_intent(
        "Проанализируй отзывы по услуге «Промывание миндалин Тонзиллор». Что раздражает пациентов?"
    )
    assert out["intent"] == "reputation_analyze"
    assert out["entity_type"] == "service"
    assert "Промывание миндалин Тонзиллор" in out["entities"]


def test_heuristic_analyze_after_keyword():
    out = _heuristic_intent("Сделай разбор по клинике Союз")
    assert out["intent"] == "reputation_analyze"
    assert out["entity_type"] == "clinic"
    assert any("Союз" in e for e in out["entities"])


def test_heuristic_compare_pair():
    out = _heuristic_intent("Сравни клинику Союз и Авиценна, что лучше")
    assert out["intent"] == "reputation_compare"
    assert out["entity_type"] == "clinic"
    assert len(out["entities"]) == 2


def test_heuristic_compare_quoted():
    out = _heuristic_intent('Сравни «Союз» и «Авиценна»')
    assert out["intent"] == "reputation_compare"
    assert out["entities"] == ["Союз", "Авиценна"]


def test_heuristic_rag_search():
    out = _heuristic_intent("Что говорят про клинику Союз")
    assert out["intent"] == "rag_search"
    assert out["entity_type"] == "clinic"


def test_heuristic_fallback():
    out = _heuristic_intent("Сколько в базе всего отзывов?")
    assert out["intent"] == "fallback"


def test_detect_intent_uses_llm_when_low_confidence():
    calls: list[str] = []

    def fake_chat(*, system_prompt, user_prompt, **kwargs):
        calls.append(user_prompt)
        return (
            '{"intent": "reputation_analyze", "entity_type": "doctor",'
            ' "entities": ["Иванов И.И."], "confidence": 0.92, "rationale": "doctor mention"}'
        )

    out = detect_intent("Расскажи про Иванова Ивана Ивановича", chat_completion_fn=fake_chat)
    assert calls, "LLM должен быть вызван при низкой уверенности"
    assert out["intent"] == "reputation_analyze"
    assert out["entity_type"] == "doctor"
    assert "Иванов И.И." in out["entities"]


def test_detect_intent_skips_llm_on_high_confidence():
    calls: list[str] = []

    def fake_chat(*args, **kwargs):
        calls.append("called")
        return "{}"

    out = detect_intent(
        'Сравни «Союз» и «Авиценна»',
        chat_completion_fn=fake_chat,
    )
    # эвристика уже уверена и нашла сущности — LLM не зовём
    assert calls == []
    assert out["intent"] == "reputation_compare"


def test_run_chat_routes_to_analyze():
    captured = {}

    def fake_analyze(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "entity_id": "promyvanie",
            "entity_name": "Промывание миндалин",
            "metrics": {"reviews_count": 12, "avg_rating": 8.4, "negative_share_pct": 22.0, "unanswered_share_pct": 50.0},
            "report": {
                "executive_summary": "Услуга в среднем нравится.",
                "what_patients_value": ["компетентность"],
                "top_complaints": ["цена"],
                "service_improvements": [],
                "landing_page_gaps": ["добавить длительность"],
                "ad_angle": "Понятно и быстро",
                "target_audience": "взрослые",
                "risk_topics": [],
            },
        }

    out = run_chat(
        'Проанализируй отзывы по услуге «Промывание миндалин Тонзиллор»',
        analyze_fn=fake_analyze,
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=lambda *a, **k: {"ok": True, "items": []},
        use_llm=False,
    )
    assert out["intent"]["intent"] == "reputation_analyze"
    assert captured["entity_type"] == "service"
    assert captured["entity"] == "Промывание миндалин Тонзиллор"
    assert "Промывание миндалин" in out["answer"]
    assert "Жалобы" in out["answer"]


def test_run_chat_routes_to_compare():
    captured = {}

    def fake_compare(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "items": [],
            "compare": {
                "summary": "Почти равны",
                "per_entity": [
                    {"entity_id": "Союз", "entity_name": "Союз", "strengths": ["опыт"], "weaknesses": []},
                    {"entity_id": "Авиценна", "entity_name": "Авиценна", "strengths": ["сервис"], "weaknesses": []},
                ],
                "shared_complaints": ["парковка"],
                "ad_angle": "Союз — опыт, Авиценна — сервис",
                "winner_by_metric": {"avg_rating": "Авиценна", "answer_rate": None, "review_volume": "Союз"},
            },
        }

    out = run_chat(
        'Сравни «Союз» и «Авиценна»',
        analyze_fn=lambda **k: {"ok": True},
        compare_fn=fake_compare,
        rag_search_fn=lambda *a, **k: {"ok": True, "items": []},
        use_llm=False,
    )
    assert out["intent"]["intent"] == "reputation_compare"
    assert captured["entities"] == ["Союз", "Авиценна"]
    assert "Почти равны" in out["answer"]
    assert "Общие жалобы" in out["answer"]


def test_run_chat_falls_back_to_rag():
    rag_calls: list[str] = []

    def fake_rag(query, **kwargs):
        rag_calls.append(query)
        return {
            "ok": True,
            "items": [
                {"chunk_id": 1, "title": "Отзыв 1", "snippet": "снippet 1", "score": 0.9}
            ],
        }

    out = run_chat(
        "Сколько отзывов в базе и какие популярные темы?",
        analyze_fn=lambda **k: {"ok": True},
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=fake_rag,
        use_llm=False,
    )
    assert out["intent"]["intent"] in {"fallback", "rag_search"}
    assert rag_calls
    assert "Отзыв 1" in out["answer"]


def test_run_chat_analyze_without_entity_returns_hint():
    out = run_chat(
        "Проанализируй отзывы",
        analyze_fn=lambda **k: {"ok": True},
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=lambda *a, **k: {"ok": True, "items": []},
        use_llm=False,
    )
    assert out["intent"]["intent"] == "reputation_analyze"
    assert out["hint"] == "no_entity"
    assert "Уточните" in out["answer"]


def test_run_chat_compare_without_two_entities_returns_hint():
    out = run_chat(
        "Сравни одну клинику",
        analyze_fn=lambda **k: {"ok": True},
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=lambda *a, **k: {"ok": True, "items": []},
        use_llm=False,
    )
    assert out["intent"]["intent"] == "reputation_compare"
    assert out["hint"] == "need_two_entities"


def test_format_helpers_handle_errors():
    txt = _format_reputation_answer({"ok": False, "error": "entity_not_found", "hint": "uточните"})
    assert "entity_not_found" in txt
    txt = _format_compare_answer({"ok": False, "error": "x"})
    assert "x" in txt
    txt = _format_rag_answer({"ok": True, "items": []}, "что-то")
    assert "ничего не нашлось" in txt

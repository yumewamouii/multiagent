"""Тесты для docdoc_rag без обращения к БД и LM Studio."""

from app.services.docdoc_rag import (
    _keyword_overlap,
    doctor_to_chunk,
    iter_drafts_from_crawl,
    review_to_chunk,
    service_parsed_to_chunk,
)


def test_review_to_chunk_basic():
    rev = {
        "review_id": 100,
        "text": "Долго ждали приёма, но врач объяснил всё подробно",
        "answer": "Спасибо за отзыв",
        "rating_value": 8,
        "service_name": "УЗИ",
        "parent_service_name": "Диагностика",
        "clinic_name": "Клиника Союз",
        "clinic_alias": "klinika_soyuz",
        "doctor_id": 555,
        "doctor_name": "Иванов И.И.",
        "source_page_url": "https://irk.docdoc.ru/service/diagnostika/uzi",
    }
    ch = review_to_chunk(rev)
    assert ch is not None
    assert ch.kind == "review"
    assert ch.ref_external_id == "100"
    assert "[Ответ клиники]" in ch.body
    assert ch.tags == "answered"
    assert ch.service_name == "УЗИ"
    assert ch.parent_service_name == "Диагностика"
    assert ch.clinic_alias == "klinika_soyuz"
    assert ch.doctor_external_id == 555


def test_review_to_chunk_no_answer_marks_unanswered():
    ch = review_to_chunk({"review_id": 1, "text": "ok", "answer": ""})
    assert ch is not None
    assert ch.tags == "no_answer"


def test_review_to_chunk_drops_empty():
    assert review_to_chunk({"review_id": 1, "text": ""}) is None
    assert review_to_chunk({"review_id": 0, "text": "abc"}) is None


def test_doctor_to_chunk_assembles_body():
    doc = {
        "doctor_id": 42,
        "name": "Петров П.П.",
        "speciality": "Стоматолог",
        "address": "Иркутск, ул. Ленина 1",
        "total_rating": 9.5,
        "reviews_count": 12,
        "price": 1500,
        "service_external_id": 4878,
        "service_name": "Чистка зубов",
        "parent_service_name": "Стоматология",
        "profile_url": "https://irk.docdoc.ru/doctor/42",
    }
    ch = doctor_to_chunk(doc)
    assert ch is not None
    assert ch.kind == "doctor"
    assert "Стоматолог" in ch.title
    assert "Стоматология — Чистка зубов" in ch.body
    assert ch.rating_value == 9.5
    assert ch.doctor_external_id == 42


def test_service_parsed_to_chunk_uses_breadcrumbs_when_empty_parent():
    parsed = {
        "ok": True,
        "page_url": "https://irk.docdoc.ru/service/lor/promyvanie",
        "service": {
            "id": 4878,
            "name": "Промывание миндалин аппаратом Тонзиллор",
            "parent_service_name": "",
            "description_plain": "Процедура очистки лакун…",
            "avg_price": 1200,
        },
        "breadcrumbs": [{"name": "ЛОР"}],
    }
    ch = service_parsed_to_chunk(parsed)
    assert ch is not None
    assert ch.kind == "service"
    assert "Тонзиллор" in ch.title
    assert "Средняя цена: 1200" in ch.body
    assert "ЛОР" in ch.body


def test_iter_drafts_filters_by_kinds():
    crawl = {
        "services": [
            {
                "ok": True,
                "page_url": "u",
                "service": {"id": 1, "name": "A", "parent_service_name": "X"},
                "doctors": [
                    {"doctor_id": 10, "name": "Doc", "speciality": "S"},
                ],
                "reviews": [
                    {"review_id": 1, "text": "hi"},
                ],
            }
        ],
        "reviews": [],
    }
    only_reviews = list(iter_drafts_from_crawl(crawl, kinds=["review"]))
    assert {d.kind for d in only_reviews} == {"review"}
    only_doctors = list(iter_drafts_from_crawl(crawl, kinds=["doctor"]))
    assert {d.kind for d in only_doctors} == {"doctor"}
    all_kinds = list(iter_drafts_from_crawl(crawl, kinds=["review", "doctor", "service"]))
    kinds_seen = {d.kind for d in all_kinds}
    assert kinds_seen == {"review", "doctor", "service"}


def test_iter_drafts_dedupes_doctors():
    crawl = {
        "services": [
            {
                "ok": True,
                "service": {"id": 1, "name": "A"},
                "doctors": [{"doctor_id": 10, "name": "Doc1"}],
            },
            {
                "ok": True,
                "service": {"id": 2, "name": "B"},
                "doctors": [{"doctor_id": 10, "name": "Doc1"}],
            },
        ],
        "reviews": [],
    }
    doctors = list(iter_drafts_from_crawl(crawl, kinds=["doctor"]))
    assert len(doctors) == 1


def test_keyword_overlap_basic():
    assert _keyword_overlap("длинные очереди", "очереди и ожидание") > 0
    assert _keyword_overlap("", "anything") == 0
    assert _keyword_overlap("стоматолог", "хирург и анестезиолог") == 0

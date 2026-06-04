from app.parsers import telegram_export as te
from app.services import telegram_ingest as ti


def test_extract_plain_string():
    msg = {"type": "message", "text": "  hello  "}
    assert te.extract_message_plain_text(msg) == "hello"


def test_extract_plain_from_fragments():
    msg = {
        "type": "message",
        "text": ["a ", {"type": "link", "text": "https://x.y"}, " b"],
    }
    assert te.extract_message_plain_text(msg) == "a https://x.y b"


def test_extract_caption():
    msg = {
        "type": "message",
        "photo": "(File not included...)",
        "caption": "Подпись к фото",
    }
    assert te.extract_message_plain_text(msg) == "Подпись к фото"


def test_extract_poll():
    msg = {
        "type": "message",
        "poll": {
            "question": "Ваш регион?",
            "answers": [{"text": "Москва"}, {"text": "Иркутск"}],
        },
    }
    text = te.extract_message_plain_text(msg)
    assert "Ваш регион" in text
    assert "Иркутск" in text


def test_normalize_reply():
    msg = {
        "type": "message",
        "id": 62,
        "from": "User",
        "from_id": "user1",
        "reply_to_message_id": 28,
        "text": "С удовольствием!",
    }
    norm = te.normalize_message(msg)
    assert norm is not None
    assert norm.message_id == 62
    assert norm.reply_to_message_id == 28


def test_iter_skips_non_message():
    export = {
        "messages": [
            {"type": "service", "text": "x"},
            {"type": "message", "text": ""},
            {"type": "message", "text": "ok"},
        ]
    }
    out = list(te.iter_export_text_messages(export))
    assert len(out) == 1
    assert out[0][1] == "ok"


def test_export_unique_key():
    assert te.export_unique_key(1448120171, "Name") == "1448120171"
    assert te.export_unique_key(None, "My Chat!").startswith("name:")


def test_heuristic_spam_event_invite():
    text = "Коллеги, приглашаем на вебинар https://zoom.us/j/123"
    spam, _reason = ti.heuristic_spam_or_ad(text)
    assert spam is True


def test_heuristic_allows_discussion():
    long_q = (
        "Коллеги, как вы оцениваете спрос на услуги в вашем регионе? "
        "Интересно сравнить с конкурентами по цене лазерной эпиляции."
    )
    spam, _reason = ti.heuristic_spam_or_ad(long_q)
    assert spam is False


def test_effective_limit_zero_means_unlimited():
    assert ti._effective_limit(0) is None
    assert ti._effective_limit(None) is None
    assert ti._effective_limit(10) == 10

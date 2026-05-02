from app import services


def test_extract_json_returns_dict():
    payload = 'prefix {"rating": 5, "sentiment": "positive"} suffix'
    result = services.extract_json(payload)
    assert result == {"rating": 5, "sentiment": "positive"}


def test_summarize_review():
    assert services.summarize_review("abcdef", max_chars=3) == "abc"


def test_is_valid_review_text():
    assert services.is_valid_review_text("Хороший товар")
    assert not services.is_valid_review_text("-")

from app import services


def test_parse_html_with_gigachat_shape():
    result = services.parse_html_with_gigachat('<html><body><div>Отличный товар</div></body></html>')
    assert 'review_text' in result
    assert 'rating' in result
    assert 'sentiment' in result
    assert 'summary' in result
    assert 'tags' in result


def test_summarize_review():
    assert services.summarize_review('abcdef', max_chars=3) == 'abc'


def test_fetch_html_pages_invalid_url_returns_empty_list():
    assert services.fetch_html_pages(['http://127.0.0.1:9/unreachable']) == []

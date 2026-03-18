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
            'name': 'otzovik-test',
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
    assert len(data) >= 1
    assert data[0]['product_name'] == 'Тестовый чайник'

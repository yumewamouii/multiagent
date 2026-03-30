# multiagent


## Что уже реализовано

- FastAPI backend на Python.
- Фоновый асинхронный парсер, который стартует вместе с `main.py` и непрерывно собирает отзывы.
- Основная СУБД: **PostgreSQL**.
- Векторное хранилище: **pgvector (VectorPG)** для эмбеддингов базы знаний.
- Доменная модель:
  - `sources` — источники отзывов (wildberries и пр.);
  - `reviews` — отзывы;
  - `knowledge_chunks` — фрагменты базы знаний (`summary/sentiment/tags + embedding`);
  - `agent_profiles` — профили AI-агентов (включая `provider=gigachat`, `model_name`).
- Ingestion-цепочка: парсер -> GigaChat (извлечение `review_text/rating/sentiment/summary/tags`) -> запись в `reviews` и `knowledge_chunks`.
- Непрерывное пополнение базы знаний в фоне (параллельно API-эндпоинтам).


## Переменные окружения

- `DATABASE_URL` — строка подключения к PostgreSQL.
  - по умолчанию: `postgresql+psycopg://postgres:postgres@localhost:5432/multiagent`
- `GIGACHAT_API_KEY` — ключ для интеграции с API GigaChat.


## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

При запуске backend пытается включить расширение `vector` в PostgreSQL:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

## Примеры API

### 1) Создать источник

```bash
curl -X POST http://127.0.0.1:8000/sources \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "otzovik",
    "base_url": "https://otzovik.com",
    "parser_type": "html"
  }'
```

### 2) Поиск по базе знаний

```bash
curl 'http://127.0.0.1:8000/knowledge/search?query=пылесос'
```

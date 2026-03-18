# multiagent

MVP backend для мультиагентной системы на Python, которая собирает отзывы с нескольких источников (первый — **otzovik.com**) и готовит знания для AI-агентов на базе **GigaChat**.

## Что уже реализовано

- FastAPI backend на Python.
- Фоновый асинхронный воркер парсинга, который стартует вместе с `main.py` и непрерывно собирает отзывы.
- Основная СУБД: **PostgreSQL**.
- Векторное хранилище: **pgvector (VectorPG)** для эмбеддингов базы знаний.
- Доменная модель:
  - `sources` — источники отзывов (otzovik и др.);
  - `reviews` — отзывы, извлечённые из HTML через GigaChat;
  - `knowledge_chunks` — фрагменты базы знаний (`summary/sentiment/tags + embedding`), полученные через GigaChat;
  - `agent_profiles` — профили AI-агентов (включая `provider=gigachat`, `model_name`).
- Ingestion-цепочка: HTML страницы -> GigaChat (извлечение `review_text/rating/sentiment/summary/tags`) -> запись в `reviews` и `knowledge_chunks`.
- Непрерывное пополнение базы знаний в фоне (параллельно API-эндпоинтам).

## Важный принцип обработки

По вашей постановке:
- в `backend` не считаются sentiment/оценка/текст отзыва локальными эвристиками;
- в `backend` на вход подаётся HTML;
- дальше структуру отзыва извлекает **GigaChat**.

Сейчас вызов GigaChat оформлен в функции-заглушке `parse_html_with_gigachat` (точка интеграции), которую можно заменить на реальный SDK/API вызов без изменения остального пайплайна.

Для получения HTML добавлена функция `fetch_html_page(url)`: она отправляет `GET` запрос к странице и возвращает HTML для дальнейшей передачи в GigaChat.

## Переменные окружения

- `DATABASE_URL` — строка подключения к PostgreSQL.
  - по умолчанию: `postgresql+psycopg://postgres:postgres@localhost:5432/multiagent`
- `GIGACHAT_API_KEY` — ключ для интеграции с API GigaChat.
- `PARSER_POLL_INTERVAL_SEC` — период опроса источников фоновым воркером (по умолчанию `60`).

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
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

### 2) Ingest отзыва из HTML (через GigaChat)

```bash
curl -X POST http://127.0.0.1:8000/reviews/ingest-html \
  -H 'Content-Type: application/json' \
  -d '{
    "source_id": 1,
    "external_id": "rev-html-001",
    "product_name": "Робот-пылесос X",
    "author": "ivan",
    "html_page": "<html><body><div class=\"review\">Хороший пылесос, рекомендую</div></body></html>"
  }'
```

### 3) Поиск по базе знаний

```bash
curl 'http://127.0.0.1:8000/knowledge/search?query=пылесос'
```

## Фоновый парсинг

После старта `uvicorn app.main:app` поднимается фоновая задача, которая:

1. читает список источников из `sources`;
2. получает HTML-страницы отзывов у источника;
3. отправляет HTML в GigaChat для извлечения структурированных полей;
4. добавляет только новые отзывы (`source_id + external_id`);
5. автоматически создаёт `knowledge_chunks`, пополняя БЗ.

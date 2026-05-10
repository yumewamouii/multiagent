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
  - `agent_profiles` — профили AI-агентов (`provider`, `model_name`, `prompt_template`).
- Ingestion-цепочка: парсер -> локальная LLM (извлечение `review_text/rating/sentiment/summary/tags`) -> запись в `reviews` и `knowledge_chunks`.
- Непрерывное пополнение базы знаний в фоне (параллельно API-эндпоинтам).
- Multi-agent LLM-оркестрация с ролями `router/worker/critic/summarizer`:
  - у каждого агента отдельный системный промпт;
  - собирается контекст из retrieved evidence;
  - поддерживается локальный провайдер LM Studio (OpenAI-compatible API).


## Переменные окружения

- `DATABASE_URL` — строка подключения к PostgreSQL.
  - по умолчанию: `postgresql+psycopg://postgres:postgres@localhost:5432/multiagent`
- `ENABLE_BACKGROUND_INGESTION` — включает непрерывный ingestion (`true`/`false`, по умолчанию `false`).
- `LLM_PROVIDER` — провайдер LLM (используйте `lmstudio`).
- `LMSTUDIO_BASE_URL` — URL LM Studio API (по умолчанию `http://127.0.0.1:1234/v1`).
- `LMSTUDIO_MODEL` — id загруженной в LM Studio модели (например, `qwen2.5-7b-instruct`).
- `LMSTUDIO_EMBEDDING_MODEL` — embedding-модель в LM Studio (по умолчанию `text-embedding-qwen3-embedding-4b`).
- `LMSTUDIO_API_KEY` — bearer-токен для LM Studio (можно оставить `lm-studio`).
- `LMSTUDIO_TIMEOUT_SEC` — таймаут вызова локальной модели.
- `ENABLE_LLM_ROUTER` — включает LLM-маршрутизацию (`true`/`false`, по умолчанию `true`).
- `EMBEDDING_VECTOR_SIZE` — размер вектора для записи в БД (по умолчанию `384`, должен совпадать с размером `pgvector` колонки).

Проверка фонового парсера:
- `GET /ingestion/status` — состояние фонового цикла, последний error, число источников;
- `POST /ingestion/start` — запустить без перезапуска backend;
- `POST /ingestion/stop` — остановить задачу ingestion.


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

### 3) Multi-agent ответ (router/worker/critic/summarizer)

```bash
curl -X POST http://127.0.0.1:8000/multiagent/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "какие плюсы и минусы у чайника?",
    "top_k": 5
  }'
```

### 4) Async multi-agent очередь

```bash
curl -X POST http://127.0.0.1:8000/multiagent/query/async \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "сравни два товара",
    "top_k": 5
  }'
```

```bash
curl http://127.0.0.1:8000/multiagent/jobs/<job_id>
```

### 5) RAG-запрос (retrieve + rerank + generate)

```bash
curl -X POST http://127.0.0.1:8000/rag/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "какие сильные и слабые стороны товара?",
    "top_k": 5
  }'
```

Ответ включает:
- `answer` — grounded ответ;
- `citations` — источники, использованные в генерации;
- `metrics` — latency и объем retrieval-кандидатов.

### 6) Product Insights для маркетологов/аналитиков

```bash
curl -X POST http://127.0.0.1:8000/insights/product \
  -H 'Content-Type: application/json' \
  -d '{
    "product_name": "Чайник X",
    "top_k": 8,
    "source_id": 1
  }'
```

Этот flow возвращает:
- агрегированные инсайты по отзывам товара;
- `sentiment_breakdown` и `top_tags` для аналитики;
- `roles` и `tools`, задействованные в запуске;
- `business_roles` (`market_analyst`, `campaign_advisor`);
- `mcp_flow` с сообщениями между агентами (Router -> Tools -> Critic -> Summarizer);
- сохранение запуска и MCP-событий в БД (`insight_runs`, `mcp_events`).

### 7) Dashboard для маркетологов

```bash
curl -X POST http://127.0.0.1:8000/insights/dashboard \
  -H 'Content-Type: application/json' \
  -d '{
    "product_name": "Чайник",
    "source_id": 1,
    "page": 1,
    "page_size": 20
  }'
```

```bash
curl -X POST http://127.0.0.1:8000/insights/dashboard/export \
  -H 'Content-Type: application/json' \
  -d '{
    "product_name": "Чайник"
  }'
```

Dashboard теперь возвращает:
- пагинацию: `page`, `page_size`, `total_pages`;
- KPI для маркетинга: `review_count`, `avg_rating`, `negative_ratio`, `positive_ratio`.

### 8) PNG-график по тренду инсайтов

```bash
curl -X POST http://127.0.0.1:8000/insights/dashboard/plot \
  -H 'Content-Type: application/json' \
  -d '{
    "product_name": "Чайник",
    "source_id": 1
  }' --output dashboard.png
```

Вернет график по дням:
- число запусков инсайтов;
- средняя `confidence`.

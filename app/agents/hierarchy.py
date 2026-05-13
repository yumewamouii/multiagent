import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypedDict
from uuid import uuid4

import app.models.orm as models
import app.services.operations as services
from app.core.db import SessionLocal
from app.core.llm import chat_completion, parse_json_response
from app.utils.observability import log_insight_to_mlflow

try:
    from langgraph.graph import END, START, StateGraph
except Exception:  # pragma: no cover
    END = "END"
    START = "START"
    StateGraph = None


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class WorkerResult:
    route: str
    chunks: list


def _mcp_payload_to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, WorkerResult):
        return {"route": obj.route, "chunk_count": len(obj.chunks)}
    if isinstance(obj, dict):
        return {str(k): _mcp_payload_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_mcp_payload_to_jsonable(v) for v in obj]
    if isinstance(obj, set):
        return [_mcp_payload_to_jsonable(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    return str(obj)


@dataclass
class MCPMessage:
    message_id: str
    from_agent: str
    to_agent: str
    intent: str
    payload: dict
    created_at: str


class MCPHub:
    def __init__(self) -> None:
        self.messages: list[MCPMessage] = []

    def send(self, from_agent: str, to_agent: str, intent: str, payload: dict) -> MCPMessage:
        message = MCPMessage(
            message_id=str(uuid.uuid4()),
            from_agent=from_agent,
            to_agent=to_agent,
            intent=intent,
            payload=payload,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.messages.append(message)
        return message

    def dump(self) -> list[dict]:
        return [
            {
                "message_id": msg.message_id,
                "from_agent": msg.from_agent,
                "to_agent": msg.to_agent,
                "intent": msg.intent,
                "payload": msg.payload,
                "created_at": msg.created_at,
            }
            for msg in self.messages
        ]


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.owners: dict[str, str] = {}

    def register(self, name: str, fn: Any, owner: str) -> None:
        self.tools[name] = fn
        self.owners[name] = owner

    async def run(self, name: str, **kwargs):
        if name not in self.tools:
            raise KeyError(f"tool_not_found: {name}")
        result = self.tools[name](**kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result


class AgentState(TypedDict, total=False):
    query: str
    top_k: int
    route: str
    worker_result: WorkerResult
    critic_result: dict[str, Any]
    answer: str
    react_log: list[dict[str, Any]]


class BaseReActAgent:
    role = "base"

    def __init__(self, mcp: MCPHub, tools: ToolRegistry):
        self.mcp = mcp
        self.tools = tools

    def _think(self, thought: str, state: AgentState) -> None:
        react_log = state.setdefault("react_log", [])
        react_log.append(
            {"agent": self.role, "phase": "think", "message": thought, "ts": datetime.now(UTC).isoformat()}
        )

    def _observe(self, observation: str, state: AgentState) -> None:
        react_log = state.setdefault("react_log", [])
        react_log.append(
            {"agent": self.role, "phase": "observe", "message": observation, "ts": datetime.now(UTC).isoformat()}
        )

    async def _act(self, to_agent: str, tool_name: str, state: AgentState, **kwargs):
        self.mcp.send(
            self.role,
            to_agent,
            "tool_call",
            {"tool": tool_name, "args": _mcp_payload_to_jsonable(kwargs)},
        )
        result = await self.tools.run(tool_name, **kwargs)
        self._observe(f"tool={tool_name} completed", state)
        return result


class RouterAgent(BaseReActAgent):
    role = "router"

    async def run(self, state: AgentState) -> AgentState:
        query = state["query"]
        self._think("Определяю маршрут запроса.", state)
        route = await self._act("tool.router", "classify_route", state, query=query)
        self._think("Проверяю извлеченные сущности для уточнения.", state)
        entities = await self._act("tool.router", "extract_entities", state, query=query)
        self.mcp.send(self.role, "coordinator", "route_selected", {"route": route, "entities": entities})
        state["route"] = route
        return state


class ResearchAgent(BaseReActAgent):
    role = "researcher"

    async def run(self, state: AgentState) -> AgentState:
        query = state["query"]
        route = state.get("route", "product_lookup")
        top_k = state.get("top_k", 5)

        self._think("Запускаю гибридный поиск через RAG.", state)
        rag_payload = await self._act(
            "tool.rag",
            "rag_query",
            state,
            query=query,
            top_k=top_k,
        )
        self._think("Дополняю evidence векторным поиском.", state)
        vector_chunks = await self._act("tool.vector", "vector_search", state, query=query, top_k=top_k)
        self._think("Сверяю с keyword-поиском.", state)
        keyword_chunks = await self._act("tool.keyword", "keyword_search", state, query=query, top_k=top_k)

        combined = []
        combined.extend(vector_chunks)
        seen = {chunk.review_id for chunk in combined}
        for chunk in keyword_chunks:
            if chunk.review_id not in seen:
                combined.append(chunk)
                seen.add(chunk.review_id)

        if not combined and rag_payload.get("citations"):
            fallback_chunks = await self._act(
                "tool.vector",
                "vector_search",
                state,
                query=query,
                top_k=max(1, min(3, top_k)),
            )
            combined.extend(fallback_chunks)

        worker_result = WorkerResult(route=route, chunks=combined[:top_k])
        self.mcp.send(self.role, "coordinator", "evidence_ready", {"items": len(worker_result.chunks)})
        state["worker_result"] = worker_result
        return state


class CriticAgent(BaseReActAgent):
    role = "critic"

    async def run(self, state: AgentState) -> AgentState:
        query = state["query"]
        worker_result = state["worker_result"]

        self._think("Оцениваю полноту покрытия evidence.", state)
        coverage = await self._act(
            "tool.critic",
            "score_coverage",
            state,
            query=query,
            worker_result=worker_result,
        )
        self._think("Проверяю grounding ответа по evidence.", state)
        grounding = await self._act(
            "tool.critic",
            "check_grounding",
            state,
            query=query,
            worker_result=worker_result,
        )

        confidence = max(0.0, min(1.0, (coverage + grounding) / 2))
        critic_result = {
            "passed": bool(worker_result.chunks),
            "confidence": round(confidence, 2),
            "notes": "ok" if worker_result.chunks else "no_relevant_chunks",
        }
        self.mcp.send(self.role, "coordinator", "quality_report", critic_result)
        state["critic_result"] = critic_result
        return state


class SummarizerAgent(BaseReActAgent):
    role = "summarizer"

    async def run(self, state: AgentState) -> AgentState:
        query = state["query"]
        worker_result = state["worker_result"]
        critic_result = state["critic_result"]

        self._think("Собираю структурированные тезисы для финального ответа.", state)
        bullets = await self._act(
            "tool.summary",
            "build_bullets",
            state,
            worker_result=worker_result,
        )
        self._think("Формирую финальный grounded ответ.", state)
        answer = await self._act(
            "tool.summary",
            "compose_answer",
            state,
            query=query,
            route=worker_result.route,
            critic_result=critic_result,
            bullets=bullets,
        )
        self.mcp.send(self.role, "coordinator", "final_answer", {"answer_preview": answer[:180]})
        state["answer"] = answer
        return state


class MultiAgentRuntime:
    def __init__(self, queue_size: int = 100):
        self.mcp = MCPHub()
        self.tools = ToolRegistry()
        self._register_default_tools()

        self.router = RouterAgent(self.mcp, self.tools)
        self.researcher = ResearchAgent(self.mcp, self.tools)
        self.critic = CriticAgent(self.mcp, self.tools)
        self.summarizer = SummarizerAgent(self.mcp, self.tools)

        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.jobs: dict[str, dict] = {}
        self._runner_task: asyncio.Task | None = None
        self._stopped = True

    async def start(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            return
        self._stopped = False
        self._runner_task = asyncio.create_task(self._run_queue())

    async def stop(self) -> None:
        self._stopped = True
        if self._runner_task is not None:
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            self._runner_task = None

    async def orchestrate(self, query: str, top_k: int = 5) -> dict:
        self.mcp.messages = []
        self.mcp.send("coordinator", "router", "task_delegation", {"query": query, "top_k": top_k})
        initial_state: AgentState = {"query": query, "top_k": top_k, "react_log": []}

        if StateGraph is not None:
            graph = StateGraph(AgentState)
            graph.add_node("router", self.router.run)
            graph.add_node("researcher", self.researcher.run)
            graph.add_node("critic", self.critic.run)
            graph.add_node("summarizer", self.summarizer.run)
            graph.add_edge(START, "router")
            graph.add_edge("router", "researcher")
            graph.add_edge("researcher", "critic")
            graph.add_edge("critic", "summarizer")
            graph.add_edge("summarizer", END)
            compiled = graph.compile()
            state = await compiled.ainvoke(initial_state)
        else:  # pragma: no cover
            state = await self.router.run(initial_state)
            state = await self.researcher.run(state)
            state = await self.critic.run(state)
            state = await self.summarizer.run(state)

        worker_result = state["worker_result"]
        critic_result = state["critic_result"]
        answer = state["answer"]

        return {
            "route": state.get("route", "product_lookup"),
            "critic": critic_result,
            "answer": answer,
            "evidence": self._to_evidence(worker_result),
            "mcp_flow": self.mcp.dump(),
            "roles": ["coordinator", self.router.role, self.researcher.role, self.critic.role, self.summarizer.role],
            "tools": sorted(self.tools.tools.keys()),
            "react_trace": state.get("react_log", []),
        }

    async def product_insight(
        self,
        product_name: str,
        top_k: int = 8,
        source_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict:
        query = f"insight по товару {product_name}"
        result = await self.orchestrate(query=query, top_k=top_k)

        rag = await self.tools.run(
            "rag_query",
            query=product_name,
            top_k=top_k,
            source_id=source_id,
            date_from=date_from,
            date_to=date_to,
        )
        sentiment = await self.tools.run("sentiment_breakdown", citations=rag.get("citations", []))
        top_tags = await self.tools.run("top_tags", citations=rag.get("citations", []))

        summary = (
            f"Товар: {product_name}. Позитив: {sentiment['positive']}, "
            f"нейтраль: {sentiment['neutral']}, негатив: {sentiment['negative']}. "
            f"Частые теги: {', '.join(top_tags) if top_tags else 'нет данных'}."
        )
        payload = {
            "product_name": product_name,
            "route": result["route"],
            "summary": summary,
            "rag_answer": rag.get("answer", ""),
            "citations": rag.get("citations", []),
            "metrics": rag.get("metrics", {}),
            "sentiment_breakdown": sentiment,
            "top_tags": top_tags,
            "critic": result["critic"],
            "mcp_flow": self.mcp.dump(),
            "roles": result["roles"],
            "business_roles": ["market_analyst", "campaign_advisor"],
            "tools": sorted(self.tools.tools.keys()),
            "react_trace": result.get("react_trace", []),
        }
        self._persist_insight_run(payload, source_id, date_from, date_to)
        return payload

    async def submit(self, query: str, top_k: int = 5) -> str:
        job_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        self.jobs[job_id] = {
            "job_id": job_id,
            "query": query,
            "top_k": top_k,
            "status": JobStatus.queued.value,
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
        }
        await self.queue.put(job_id)
        return job_id

    def get_job(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)

    def _register_default_tools(self) -> None:
        self.tools.register("classify_route", self._tool_classify_route, owner="router")
        self.tools.register("extract_entities", self._tool_extract_entities, owner="router")
        self.tools.register("rag_query", services.rag_answer, owner="researcher")
        self.tools.register("vector_search", services.search_chunks, owner="researcher")
        self.tools.register("keyword_search", services.search_chunks_keyword, owner="researcher")
        self.tools.register("score_coverage", self._tool_score_coverage, owner="critic")
        self.tools.register("check_grounding", self._tool_check_grounding, owner="critic")
        self.tools.register("build_bullets", self._tool_build_bullets, owner="summarizer")
        self.tools.register("compose_answer", self._tool_compose_answer, owner="summarizer")
        self.tools.register("sentiment_breakdown", self._tool_sentiment_breakdown, owner="summarizer")
        self.tools.register("top_tags", self._tool_top_tags, owner="summarizer")

    @staticmethod
    def _tool_classify_route(query: str) -> str:
        lowered = query.lower()
        if os.getenv("ENABLE_LLM_ROUTER", "true").lower() == "true":
            try:
                raw = chat_completion(
                    system_prompt=(
                        "Ты агент-маршрутизатор. Выбери ОДИН маршрут: "
                        "comparison, pros_cons, sentiment, product_lookup. "
                        "Верни JSON: {\"route\":\"...\"}."
                    ),
                    user_prompt=f"Запрос: {query}",
                    temperature=0.0,
                    max_tokens=80,
                )
                payload = parse_json_response(raw) or {}
                route = (payload.get("route") or "").strip()
                if route in {"comparison", "pros_cons", "sentiment", "product_lookup"}:
                    return route
            except Exception:
                pass
        if any(token in lowered for token in ("сравни", "лучше", "хуже", "vs")):
            return "comparison"
        if any(token in lowered for token in ("плюс", "минус", "достоин", "недостат")):
            return "pros_cons"
        if any(token in lowered for token in ("настроен", "эмоци", "sentiment", "тон")):
            return "sentiment"
        return "product_lookup"

    @staticmethod
    def _tool_extract_entities(query: str) -> dict:
        tokens = [part.strip(".,!?()[]{}\"'") for part in query.split()]
        products = [token for token in tokens if len(token) > 3 and token[0].isupper()]
        return {"products": products[:3]}

    @staticmethod
    def _tool_score_coverage(query: str, worker_result: WorkerResult) -> float:
        if not worker_result.chunks:
            return 0.1
        useful = 0
        for chunk in worker_result.chunks:
            summary = (chunk.summary or "").strip()
            if summary:
                useful += 1
        return min(1.0, 0.4 + (useful / max(1, len(worker_result.chunks))) * 0.6)

    @staticmethod
    def _tool_check_grounding(query: str, worker_result: WorkerResult) -> float:
        if not worker_result.chunks:
            return 0.15
        query_tokens = {token for token in query.lower().split() if len(token) > 2}
        if not query_tokens:
            return 0.7
        overlap = 0.0
        for chunk in worker_result.chunks:
            corpus = f"{chunk.summary} {chunk.tags}".lower()
            tokens = set(corpus.split())
            overlap += len(tokens.intersection(query_tokens)) / len(query_tokens)
        overlap = overlap / len(worker_result.chunks)
        return max(0.2, min(1.0, overlap))

    @staticmethod
    def _tool_build_bullets(worker_result: WorkerResult) -> list[str]:
        bullets = []
        for chunk in worker_result.chunks[:3]:
            sentiment = chunk.sentiment or "нейтральное"
            summary = (chunk.summary or "").strip() or "нет краткого описания"
            product = chunk.review.product_name if chunk.review else "неизвестный товар"
            bullets.append(f"- {product}: {summary} (sentiment: {sentiment})")
        return bullets

    @staticmethod
    def _tool_compose_answer(query: str, route: str, critic_result: dict, bullets: list[str]) -> str:
        if not bullets:
            return (
                "По текущей базе знаний не найдено релевантных отзывов. "
                "Уточните запрос или добавьте больше данных в ingestion."
            )
        details = "\n".join(bullets)
        try:
            text = chat_completion(
                system_prompt=(
                    "Ты summarizer-агент. Дай ответ по evidence, без галлюцинаций. "
                    "Формат: 1 абзац и 3 буллета."
                ),
                user_prompt=(
                    f"Запрос: {query}\n"
                    f"Маршрут: {route}\n"
                    f"Оценка критика: {critic_result}\n"
                    f"Evidence:\n{details}"
                ),
                temperature=0.2,
                max_tokens=420,
            )
            if text.strip():
                return text.strip()
        except Exception:
            pass
        return f"Маршрут: {route}. Уверенность критика: {critic_result['confidence']}.\n{details}"

    @staticmethod
    def _tool_sentiment_breakdown(citations: list[dict]) -> dict:
        breakdown = {"positive": 0, "neutral": 0, "negative": 0}
        for citation in citations:
            sent = (citation.get("sentiment") or "").lower()
            if "pos" in sent or "позит" in sent:
                breakdown["positive"] += 1
            elif "neg" in sent or "негат" in sent:
                breakdown["negative"] += 1
            else:
                breakdown["neutral"] += 1
        return breakdown

    @staticmethod
    def _tool_top_tags(citations: list[dict], limit: int = 5) -> list[str]:
        freq: dict[str, int] = {}
        for citation in citations:
            tags = citation.get("tags") or ""
            for tag in [part.strip().lower() for part in tags.split(",") if part.strip()]:
                freq[tag] = freq.get(tag, 0) + 1
        ordered = sorted(freq.items(), key=lambda item: item[1], reverse=True)
        return [name for name, _ in ordered[:limit]]

    def _persist_insight_run(
        self,
        result: dict,
        source_id: int | None,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> None:
        db = SessionLocal()
        try:
            run = models.InsightRun(
                product_name=result["product_name"],
                source_id=source_id,
                date_from=date_from,
                date_to=date_to,
                summary=result["summary"],
                confidence=float(result["critic"]["confidence"]),
            )
            db.add(run)
            db.commit()
            db.refresh(run)

            for message in result["mcp_flow"]:
                safe_payload = _mcp_payload_to_jsonable(message["payload"])
                event = models.MCPEvent(
                    run_id=run.id,
                    message_id=message["message_id"],
                    from_agent=message["from_agent"],
                    to_agent=message["to_agent"],
                    intent=message["intent"],
                    payload_json=json.dumps(safe_payload, ensure_ascii=False),
                )
                db.add(event)
            db.commit()
            result["run_id"] = run.id
            log_insight_to_mlflow(
                run_id=run.id,
                product_name=result["product_name"],
                source_id=source_id,
                confidence=float(result["critic"]["confidence"]),
                citations_count=len(result.get("citations", [])),
                top_tags_count=len(result.get("top_tags", [])),
                route=result.get("route", "unknown"),
            )
        finally:
            db.close()

    async def _run_queue(self) -> None:
        while not self._stopped:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            if job is None:
                self.queue.task_done()
                continue
            try:
                job["status"] = JobStatus.running.value
                job["updated_at"] = datetime.now(UTC).isoformat()
                result = await self.orchestrate(job["query"], top_k=job["top_k"])
                job["status"] = JobStatus.completed.value
                job["result"] = result
            except Exception as exc:
                job["status"] = JobStatus.failed.value
                job["error"] = str(exc)
            finally:
                job["updated_at"] = datetime.now(UTC).isoformat()
                self.queue.task_done()

    @staticmethod
    def _to_evidence(worker_result: WorkerResult) -> list[dict]:
        evidence = []
        for chunk in worker_result.chunks:
            evidence.append(
                {
                    "review_id": chunk.review_id,
                    "product_name": chunk.review.product_name if chunk.review else "",
                    "summary": chunk.summary,
                    "sentiment": chunk.sentiment,
                    "tags": chunk.tags,
                }
            )
        return evidence


runtime = MultiAgentRuntime()


def route_query(query: str) -> str:
    return runtime._tool_classify_route(query)


async def orchestrate(query: str, top_k: int = 5) -> dict:
    return await runtime.orchestrate(query=query, top_k=top_k)

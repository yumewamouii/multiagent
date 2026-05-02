import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import json
import uuid
from uuid import uuid4

from app import models
from app.db import SessionLocal
from app import services


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class WorkerResult:
    route: str
    chunks: list


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
        self.tools: dict[str, callable] = {}

    def register(self, name: str, fn) -> None:
        self.tools[name] = fn

    async def run(self, name: str, **kwargs):
        if name not in self.tools:
            raise KeyError(f"tool_not_found: {name}")
        result = self.tools[name](**kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result


class RouterAgent:
    role = "router"

    def choose_route(self, query: str) -> str:
        lowered = query.lower()

        if any(token in lowered for token in ("сравни", "лучше", "хуже", "vs")):
            return "comparison"
        if any(token in lowered for token in ("плюс", "минус", "достоин", "недостат")):
            return "pros_cons"
        if any(token in lowered for token in ("настроен", "эмоци", "sentiment", "тон")):
            return "sentiment"

        return "product_lookup"


class WorkerAgent:
    role = "worker"

    def __init__(self, retries: int = 1, timeout_sec: float = 30.0):
        self.retries = retries
        self.timeout_sec = timeout_sec

    async def run(self, query: str, route: str, top_k: int) -> WorkerResult:
        last_error = None
        for _ in range(self.retries + 1):
            try:
                chunks = await asyncio.wait_for(
                    services.search_chunks(query, top_k=top_k),
                    timeout=self.timeout_sec,
                )
                return WorkerResult(route=route, chunks=chunks)
            except Exception as exc:  # pragma: no cover - defensive path
                last_error = exc
        raise RuntimeError(f"worker_failed: {last_error}")


class CriticAgent:
    role = "critic"

    def run(self, worker_result: WorkerResult) -> dict:
        if not worker_result.chunks:
            return {
                "passed": False,
                "confidence": 0.2,
                "notes": "no_relevant_chunks",
            }

        non_empty_summaries = [
            chunk for chunk in worker_result.chunks if (chunk.summary or "").strip()
        ]
        coverage_ratio = len(non_empty_summaries) / len(worker_result.chunks)

        confidence = 0.55 + (0.35 * coverage_ratio)
        confidence = min(confidence, 0.95)

        return {
            "passed": True,
            "confidence": round(confidence, 2),
            "notes": "ok",
        }


class SummarizerAgent:
    role = "summarizer"

    def run(self, query: str, worker_result: WorkerResult, critic_result: dict) -> str:
        if not worker_result.chunks:
            return (
                "По текущей базе знаний не найдено релевантных отзывов. "
                "Уточните запрос или добавьте больше данных в ingestion."
            )

        bullet_points = []
        for chunk in worker_result.chunks[:3]:
            sentiment = chunk.sentiment or "нейтральное"
            summary = (chunk.summary or "").strip() or "нет краткого описания"
            product = chunk.review.product_name if chunk.review else "неизвестный товар"
            bullet_points.append(f"- {product}: {summary} (sentiment: {sentiment})")

        details = "\n".join(bullet_points)
        return (
            f"Маршрут: {worker_result.route}. "
            f"Уверенность критика: {critic_result['confidence']}.\n"
            f"Найденные сигналы по запросу '{query}':\n{details}"
        )


class MultiAgentRuntime:
    def __init__(self, queue_size: int = 100):
        self.mcp = MCPHub()
        self.router = RouterAgent()
        self.worker = WorkerAgent()
        self.critic = CriticAgent()
        self.summarizer = SummarizerAgent()
        self.tools = ToolRegistry()
        self._register_default_tools()

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
        self.mcp.send(
            "orchestrator",
            self.router.role,
            "route_request",
            {"query": query},
        )
        route = self.router.choose_route(query)
        self.mcp.send(
            self.router.role,
            self.worker.role,
            "route_selected",
            {"route": route, "top_k": top_k},
        )
        worker_result = await self.worker.run(query=query, route=route, top_k=top_k)
        self.mcp.send(
            self.worker.role,
            self.critic.role,
            "evidence_ready",
            {"items": len(worker_result.chunks)},
        )
        critic_result = self.critic.run(worker_result=worker_result)
        self.mcp.send(
            self.critic.role,
            self.summarizer.role,
            "quality_report",
            critic_result,
        )
        answer = self.summarizer.run(
            query=query,
            worker_result=worker_result,
            critic_result=critic_result,
        )
        self.mcp.send(
            self.summarizer.role,
            "orchestrator",
            "final_answer",
            {"answer_preview": answer[:180]},
        )
        return {
            "route": route,
            "critic": critic_result,
            "answer": answer,
            "evidence": self._to_evidence(worker_result),
            "mcp_flow": self.mcp.dump(),
            "roles": [self.router.role, self.worker.role, self.critic.role, self.summarizer.role],
            "tools": sorted(self.tools.tools.keys()),
        }

    async def product_insight(
        self,
        product_name: str,
        top_k: int = 8,
        source_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict:
        self.mcp.messages = []
        self.mcp.send("orchestrator", self.router.role, "insight_request", {"product_name": product_name})
        route = self.router.choose_route(f"insight {product_name}")

        self.mcp.send(self.router.role, "tool.rag", "tool_call", {"tool": "rag_query"})
        rag = await self.tools.run(
            "rag_query",
            query=product_name,
            top_k=top_k,
            source_id=source_id,
            date_from=date_from,
            date_to=date_to,
        )

        self.mcp.send("tool.rag", "tool.sentiment", "tool_call", {"tool": "sentiment_breakdown"})
        sentiment = await self.tools.run("sentiment_breakdown", citations=rag.get("citations", []))

        self.mcp.send("tool.sentiment", "tool.tags", "tool_call", {"tool": "top_tags"})
        top_tags = await self.tools.run("top_tags", citations=rag.get("citations", []))

        self.mcp.send("tool.tags", self.critic.role, "insight_quality_check", {"citations": len(rag.get("citations", []))})
        critic = {
            "passed": len(rag.get("citations", [])) > 0,
            "confidence": 0.85 if rag.get("citations") else 0.25,
            "notes": "ok" if rag.get("citations") else "no_evidence",
        }

        summary = (
            f"Товар: {product_name}. Позитив: {sentiment['positive']}, "
            f"нейтраль: {sentiment['neutral']}, негатив: {sentiment['negative']}. "
            f"Частые теги: {', '.join(top_tags) if top_tags else 'нет данных'}."
        )
        self.mcp.send(self.summarizer.role, "orchestrator", "insight_ready", {"summary_preview": summary[:180]})

        result = {
            "product_name": product_name,
            "route": route,
            "summary": summary,
            "rag_answer": rag.get("answer", ""),
            "citations": rag.get("citations", []),
            "metrics": rag.get("metrics", {}),
            "sentiment_breakdown": sentiment,
            "top_tags": top_tags,
            "critic": critic,
            "mcp_flow": self.mcp.dump(),
            "roles": [self.router.role, self.worker.role, self.critic.role, self.summarizer.role],
            "business_roles": ["market_analyst", "campaign_advisor"],
            "tools": sorted(self.tools.tools.keys()),
        }
        self._persist_insight_run(
            result=result,
            source_id=source_id,
            date_from=date_from,
            date_to=date_to,
        )
        return result

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
        self.tools.register("rag_query", services.rag_answer)
        self.tools.register("sentiment_breakdown", self._tool_sentiment_breakdown)
        self.tools.register("top_tags", self._tool_top_tags)

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
                event = models.MCPEvent(
                    run_id=run.id,
                    message_id=message["message_id"],
                    from_agent=message["from_agent"],
                    to_agent=message["to_agent"],
                    intent=message["intent"],
                    payload_json=json.dumps(message["payload"], ensure_ascii=False),
                )
                db.add(event)
            db.commit()
            result["run_id"] = run.id
        finally:
            db.close()

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
    return runtime.router.choose_route(query)


async def orchestrate(query: str, top_k: int = 5) -> dict:
    return await runtime.orchestrate(query=query, top_k=top_k)

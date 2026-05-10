import io
import logging
import os
from typing import Any


log = logging.getLogger(__name__)


def render_dashboard_plot(
    series: list[dict[str, float | str | int]],
    *,
    title: str = "Dashboard insights",
) -> bytes:
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("matplotlib_not_installed") from exc

    if not series:
        raise ValueError("no_data_for_plot")

    dates = [str(item["date"]) for item in series]
    runs = [int(item["runs"]) for item in series]
    avg_confidence = [float(item["avg_confidence"]) for item in series]

    fig, ax_left = plt.subplots(figsize=(10, 4))
    ax_right = ax_left.twinx()

    ax_left.plot(dates, runs, marker="o", color="#2563eb", label="Runs/day")
    ax_right.plot(dates, avg_confidence, marker="s", color="#16a34a", label="Avg confidence")

    ax_left.set_xlabel("Date")
    ax_left.set_ylabel("Runs")
    ax_right.set_ylabel("Avg confidence")
    ax_left.set_title(title)
    ax_left.tick_params(axis="x", rotation=30)
    ax_left.grid(alpha=0.2)

    lines_left, labels_left = ax_left.get_legend_handles_labels()
    lines_right, labels_right = ax_right.get_legend_handles_labels()
    ax_left.legend(lines_left + lines_right, labels_left + labels_right, loc="upper left")

    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=120)
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


def log_insight_to_mlflow(
    *,
    run_id: int,
    product_name: str,
    source_id: int | None,
    confidence: float,
    citations_count: int,
    top_tags_count: int,
    route: str,
) -> None:
    if os.getenv("ENABLE_MLFLOW", "false").lower() != "true":
        return

    try:
        import mlflow
    except Exception as exc:  # pragma: no cover - optional dependency
        log.warning("MLflow is enabled but package is missing: %s", exc)
        return

    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)

    experiment_name = os.getenv("MLFLOW_EXPERIMENT_NAME", "multiagent-insights")
    mlflow.set_experiment(experiment_name)

    with mlflow.start_run(run_name=f"insight-{run_id}"):
        mlflow.log_params(
            {
                "run_id": run_id,
                "product_name": product_name,
                "source_id": source_id if source_id is not None else "none",
                "route": route,
            }
        )
        mlflow.log_metrics(
            {
                "confidence": float(confidence),
                "citations_count": float(citations_count),
                "top_tags_count": float(top_tags_count),
            }
        )

"""Liveness, readiness and metrics.

`/healthz` answers "is this process running" and touches nothing. `/readyz`
answers "can this process do its job", which means the database and the object
store must both respond. Conflating the two makes an orchestrator restart a
healthy process because a dependency blinked.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import func, select, text

from iep import __version__
from iep.config import get_settings
from iep.db.models import Finding, ProcessingJob
from iep.db.session import session_scope
from iep.observability import metrics

router = APIRouter(tags=["system"])


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/readyz", summary="Readiness probe")
def readyz(response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}

    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"unavailable: {type(exc).__name__}"

    settings = get_settings()
    try:
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        probe = settings.storage_root / ".readyz"
        probe.write_bytes(b"ok")
        probe.unlink()
        checks["object_store"] = "ok"
    except Exception as exc:
        checks["object_store"] = f"unavailable: {type(exc).__name__}"

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "not_ready", "checks": checks}


@router.get("/metrics", summary="Prometheus text metrics", response_class=Response)
def prometheus_metrics() -> Response:
    gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
    try:
        with session_scope() as session:
            job_rows = session.execute(
                select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)
            ).all()
            gauges["iep_jobs_total"] = {
                ((("status", str(status)),)): float(count) for status, count in job_rows
            }
            finding_rows = session.execute(
                select(Finding.severity, Finding.status, func.count()).group_by(
                    Finding.severity, Finding.status
                )
            ).all()
            gauges["iep_findings_total"] = {
                ((("severity", str(sev)), ("status", str(st)))): float(count)
                for sev, st, count in finding_rows
            }
    except Exception:
        gauges["iep_metrics_db_error"] = {(): 1.0}

    return Response(content=metrics.render(gauges), media_type="text/plain; version=0.0.4")

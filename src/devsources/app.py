"""Development-only stand-ins for the corporate systems the pipeline reads.

This is a separate application from `iep`. It exists so the connector and the
scraper can be exercised end to end against something that paginates, rate
limits, fails intermittently and serves real HTML - without putting a single
test-support endpoint inside the product API, where it could be reached in a
deployment.

It is started only by the `devsources` compose service and by the developer
scripts. `iep` never imports it.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

from corpus.dataset import (
    CALL_CODE,
    CALL_MAX_FUNDING,
    CALL_PERIOD_END,
    CALL_PERIOD_START,
    DOSSIERS,
    REGISTRY_PEOPLE,
)
from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

CONTRACT_VERSION = "registry/v1"
EXPECTED_TOKEN = "local-development-token"  # noqa: S105 - a fixture, not a secret

app = FastAPI(
    title="Development source simulator",
    description=(
        "Not part of the product. Simulates a corporate registry, a public page "
        "and a hosted language model."
    ),
    version="1.0.0",
)

# Deterministic misbehaviour so the connector's retry path is exercised on a
# known schedule rather than by chance.
_request_counter = itertools.count(1)
_FAIL_ON = {3}
_RATE_LIMIT_ON = {5}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/personnel")
def personnel(
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_token(authorization)

    count = next(_request_counter)
    if count in _FAIL_ON:
        raise HTTPException(status_code=503, detail="simulated upstream unavailability")
    if count in _RATE_LIMIT_ON:
        response.headers["Retry-After"] = "1"
        raise HTTPException(status_code=429, detail="simulated rate limit")

    start = (page - 1) * page_size
    window = REGISTRY_PEOPLE[start : start + page_size]
    return {
        "contract_version": CONTRACT_VERSION,
        "page": page,
        "page_size": page_size,
        "total": len(REGISTRY_PEOPLE),
        "has_more": start + page_size < len(REGISTRY_PEOPLE),
        "items": [
            {
                "employee_id": person.employee_id,
                "full_name": person.full_name,
                "role": person.role,
                "hourly_rate_eur": str(person.hourly_rate_eur),
                "contract_start": person.contract_start.isoformat(),
                "contract_end": person.contract_end.isoformat() if person.contract_end else None,
            }
            for person in window
        ],
    }


@app.get("/api/v1/projects/{reference}")
def project(reference: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_token(authorization)
    for spec in DOSSIERS:
        if spec.reference == reference:
            return {
                "contract_version": CONTRACT_VERSION,
                "reference": spec.reference,
                "call_code": CALL_CODE,
                "registered_title": spec.title,
                "period_start": spec.period_start.isoformat(),
                "period_end": spec.period_end.isoformat(),
                "status": "REGISTERED",
            }
    raise HTTPException(status_code=404, detail="unknown project reference")


@app.get("/public/convocatoria.html", response_class=HTMLResponse)
def call_page() -> str:
    """The public page the scraper reads. Deliberately ordinary markup."""
    return f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><title>Convocatoria {CALL_CODE}</title></head>
<body>
  <main>
    <h1 id="call-title">Convocatoria de ayudas a la innovacion {CALL_CODE}</h1>
    <dl class="call-detail">
      <dt>Codigo</dt><dd data-field="call-code">{CALL_CODE}</dd>
      <dt>Inicio del periodo elegible</dt>
      <dd data-field="eligible-from">{CALL_PERIOD_START.isoformat()}</dd>
      <dt>Fin del periodo elegible</dt>
      <dd data-field="eligible-to">{CALL_PERIOD_END.isoformat()}</dd>
      <dt>Importe maximo financiable</dt>
      <dd data-field="max-funding">{_spanish(CALL_MAX_FUNDING)} EUR</dd>
      <dt>Estado</dt><dd data-field="status">Abierta</dd>
    </dl>
    <p class="disclaimer">Pagina sintetica de desarrollo.
       No representa ninguna convocatoria real.</p>
  </main>
</body>
</html>
"""


@app.get("/public/convocatoria-cambiada.html", response_class=HTMLResponse)
def call_page_restructured() -> str:
    """The same page after somebody redesigned it.

    Used to show that the scraper reports a structure change instead of
    silently returning nothing.
    """
    return f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><title>Convocatoria</title></head>
<body>
  <section class="ficha">
    <h2>Convocatoria {CALL_CODE}</h2>
    <table><tr><th>Periodo</th><td>{CALL_PERIOD_START} / {CALL_PERIOD_END}</td></tr></table>
  </section>
</body>
</html>
"""


@app.get("/public/convocatoria-rota.html", response_class=HTMLResponse)
def call_page_malformed() -> str:
    return (
        "<html><body><dl><dd data-field='eligible-from'>2025-01-01"
        "<dd data-field='max-funding'><p>sin cerrar</body>"
    )


def _spanish(amount: Decimal) -> str:
    """Amounts on a Spanish page use dots for thousands and a comma decimal.

    The reader is deliberately strict about this: guessing whether "1.234"
    means one thousand or one-point-two-three-four is how a parser produces a
    thousand-fold error without ever failing.
    """
    return f"{amount:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def _require_token(authorization: str | None) -> None:
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# ---------------------------------------------------------------------------
# A stand-in for a hosted language model.
# ---------------------------------------------------------------------------
# This is NOT a model. It is a local endpoint that speaks enough of the Messages
# API for the official SDK to talk to it, so the whole hosted-provider path -
# request shaping with a structured-output schema, response validation, the
# grounding check, retries, and token and cost accounting - can be exercised
# without a key and without spending anything.
#
# It answers by reading the document with the deterministic provider, so the
# reply is derived from the document in front of it rather than being canned.
# What it cannot demonstrate is a model's judgement on prose a rule cannot
# parse; that is the whole reason the hosted adapter exists, and it needs a real
# key. `docs/llm-demo.md` says so and gives the procedure.

_llm_calls = itertools.count(1)
# Every third call answers with something the schema rejects, so the adapter's
# validation and retry path is visible rather than only asserted in a test.
_MALFORMED_ON = 3


@app.post("/v1/messages")
def simulated_messages(payload: dict[str, Any]) -> dict[str, Any]:
    from corpus.simulated_model import answer

    call = next(_llm_calls)
    document = _document_text(payload)
    body = answer(document, malformed=call % _MALFORMED_ON == 0)
    return {
        "id": f"msg_simulated_{call:06d}",
        "type": "message",
        "role": "assistant",
        "model": payload.get("model", "local-simulator"),
        "content": [{"type": "text", "text": body}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            # Characters over four is the usual working approximation; this is
            # a simulator, and the number is only ever used for an estimate.
            "input_tokens": max(1, len(document) // 4),
            "output_tokens": max(1, len(body) // 4),
        },
    }


def _document_text(payload: dict[str, Any]) -> str:
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str) and "<untrusted_document>" in content:
            return content.split("<untrusted_document>", 1)[1].split("</untrusted_document>")[0]
    return ""

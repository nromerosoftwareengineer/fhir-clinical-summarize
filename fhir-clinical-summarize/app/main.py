import logging
import pathlib
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import extract, summarize
from app.config import settings
from app.fhir_client import RESULT_PARAMS, FhirError, FhirNotFound, fhir
from app.logging_config import setup_logging
from app.models import Packet, PatientPage, PatientSummary

UI_DIR = pathlib.Path(__file__).resolve().parent.parent / "ui"

# Query parameters this service handles itself rather than forwarding to FHIR.
# limit/offset are exposed instead of FHIR's _count/_offset so there is exactly
# one way to page.
RESERVED_PARAMS = frozenset({"limit", "offset"})

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info(
        "starting",
        extra={"fhir_base_url": settings.fhir_base_url, "model": settings.ollama_model,
               "fhir_auth": bool(settings.fhir_auth_scope)},
    )
    yield
    fhir.close()
    summarize.close()


app = FastAPI(title="FHIR Clinical Summarize", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness. Deliberately checks nothing external.

    An orchestrator restarts a container that fails its liveness probe, and
    restarting this process does nothing to fix an unreachable FHIR server or a
    cold model. Dependency checks belong in /ready.
    """
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: can this instance actually serve a packet right now?

    Returns 503 with per-dependency detail when it cannot, so a load balancer
    stops routing here and an operator can see which dependency is at fault.
    """
    checks: dict[str, str] = {}

    try:
        fhir.supported_search_params("Patient")
        checks["fhir"] = "ok"
    except FhirError as exc:
        checks["fhir"] = f"unavailable: {exc}"

    # The model is checked but not required: write_summary() degrades to an empty
    # summary, and a packet with facts and sources is still worth serving.
    try:
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5.0)
        response.raise_for_status()
        names = [model["name"] for model in response.json().get("models", [])]
        checks["model"] = "ok" if settings.ollama_model in names else (
            f"loaded models do not include {settings.ollama_model}"
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        checks["model"] = f"unavailable: {exc!r}"

    healthy = checks["fhir"] == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "not ready", "checks": checks},
    )


@app.get("/config")
def config() -> dict[str, str]:
    """Non-secret settings the dashboard needs at runtime.

    The browser needs a FHIR base URL it can actually reach to link citations to.
    In Azure the service often talks to FHIR over private networking, on an
    address no browser resolves, so this is a separate setting rather than the
    one the service itself uses.
    """
    return {"fhir_base_url": settings.browser_fhir_url, "model": settings.ollama_model}


@app.get("/patients", response_model=PatientPage)
def search_patients(
    request: Request,
    limit: int = Query(20, ge=1, le=200, description="patients per page"),
    offset: int = Query(0, ge=0, description="how many matches to skip"),
) -> PatientPage:
    """Search patients by any search parameter the FHIR server supports.

    Parameters are forwarded to GET /Patient after being checked against the
    server's CapabilityStatement, so an unsupported one fails here with the list
    of valid names instead of producing a confusing error downstream.

    Examples:
        /patients?family=Carroll471
        /patients?gender=female&birthdate=ge1980-01-01
        /patients?identifier=http://hospital.smarthealthit.org|792be1e3-...
        /patients?name=Akiko&_sort=family&limit=5
    """
    try:
        supported = fhir.supported_search_params("Patient")
    except FhirError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # multi_items() keeps repeated parameters (?given=A&given=B), which FHIR
    # treats as an AND of both values.
    forwarded: list[tuple[str, str]] = []
    unknown: list[str] = []
    for name, value in request.query_params.multi_items():
        if name in RESERVED_PARAMS:
            continue
        if name in supported or name in RESULT_PARAMS:
            forwarded.append((name, value))
        else:
            unknown.append(name)

    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"unsupported search parameter(s): {', '.join(sorted(set(unknown)))}",
                "supported": sorted(supported | RESULT_PARAMS),
            },
        )

    # _total=accurate because the point of a search endpoint is telling the
    # caller how many matches exist, not just how many came back.
    forwarded += [("_count", str(limit)), ("_offset", str(offset)), ("_total", "accurate")]

    try:
        bundle = fhir.search("Patient", forwarded)
    except FhirError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    patients = [
        PatientSummary.from_resource(entry["resource"]) for entry in bundle.get("entry", [])
    ]
    return PatientPage(
        total=bundle.get("total", len(patients)),
        count=len(patients),
        offset=offset,
        patients=patients,
    )


@app.get("/patients/{patient_id}", response_model=PatientSummary)
def get_patient(patient_id: str) -> PatientSummary:
    """Read one patient by FHIR resource id."""
    try:
        resource = fhir.get_patient(patient_id)
    except FhirNotFound as exc:
        raise HTTPException(status_code=404, detail=f"No patient {patient_id}") from exc
    except FhirError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return PatientSummary.from_resource(resource)


@app.get(
    "/patients/{patient_id}/packet",
    response_model=Packet,
    # Optional Fact fields are dropped when they equal their default, so a packet
    # carries no `status: null` noise and no `count: 1` on undeduplicated facts.
    # Packet's own fields have no defaults, so none of them can be dropped.
    response_model_exclude_defaults=True,
)
def get_packet(patient_id: str) -> Packet:
    """Assemble a source-cited clinical packet for one patient.
    The order matters: facts, statuses and source references are all settled
    before the model is called, and the model's output is only ever assigned to
    `summary`. A summary failure therefore costs the prose, not the evidence.
    This service assembles evidence. It never issues an authorization decision.
    """
    try:
        # Read the patient first so a bad id fails as 404 rather than as an
        # empty packet, which an empty record would otherwise be indistinguishable from.
        fhir.get_patient(patient_id)
        condition_resources = fhir.get_conditions(patient_id)
        medication_resources = fhir.get_medications(patient_id)
    except FhirNotFound as exc:
        raise HTTPException(status_code=404, detail=f"No patient {patient_id}") from exc
    except FhirError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    conditions, medications = extract.build_facts(condition_resources, medication_resources)
    summary = summarize.write_summary(extract.to_prompt_facts(conditions, medications))

    return Packet(
        patient_id=patient_id,
        conditions=conditions,
        medications=medications,
        summary=summary,
        missing=extract.build_missing(conditions, medications),
    )


# The dashboard is served by this app rather than a separate dev server so it is
# same-origin with the API: no CORS, and no mixed-content block when the page
# calls a local http:// backend. Mounted last so it cannot shadow an API route.
if UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

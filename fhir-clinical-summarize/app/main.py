import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from app.fhir_client import RESULT_PARAMS, FhirError, FhirNotFound, fhir
from app.models import Packet, PatientPage, PatientSummary

UI_DIR = pathlib.Path(__file__).resolve().parent.parent / "ui"

# Query parameters this service handles itself rather than forwarding to FHIR.
# limit/offset are exposed instead of FHIR's _count/_offset so there is exactly
# one way to page.
RESERVED_PARAMS = frozenset({"limit", "offset"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    fhir.close()


app = FastAPI(title="FHIR Clinical Summarize", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.get("/patients/{patient_id}/packet", response_model=Packet)
def get_packet(patient_id: str) -> Packet:
    raise HTTPException(status_code=501, detail="Not implemented yet")


# The dashboard is served by this app rather than a separate dev server so it is
# same-origin with the API: no CORS, and no mixed-content block when the page
# calls a local http:// backend. Mounted last so it cannot shadow an API route.
if UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

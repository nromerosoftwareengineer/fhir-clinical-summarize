from fastapi import FastAPI, HTTPException

from app.models import Packet

app = FastAPI(title="FHIR Clinical Summarize")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/patients/{patient_id}/packet", response_model=Packet)
def get_packet(patient_id: str) -> Packet:
    raise HTTPException(status_code=501, detail="Not implemented yet")

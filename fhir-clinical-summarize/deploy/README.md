# Deploying to Azure

The service is container-ready: every setting is an environment variable, the
image runs as non-root, and `/health` and `/ready` are separated so a platform
can tell "restart this" from "stop routing here".

> **Status:** the image and its configuration are verified locally (built, run,
> and exercised against a live FHIR server and model). The Azure commands below
> have **not** been run against a live subscription — treat them as the
> deployment plan, not a tested script.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `FHIR_BASE_URL` | `http://localhost:8080/fhir` | What the service calls. Private endpoint in Azure |
| `FHIR_PUBLIC_URL` | falls back to `FHIR_BASE_URL` | What the **browser** links citations to. Set it when the two differ, or leave unset and the dashboard renders references as plain text instead of dead links |
| `FHIR_AUTH_SCOPE` | unset | Set to `https://<ws>-<svc>.fhir.azurehealthcareapis.com/.default` to authenticate with Entra ID. Unset means anonymous, which is what a local HAPI expects |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Or any OpenAI-compatible endpoint, with a small change in `summarize.py` |
| `OLLAMA_MODEL` | `llama3.2:3b` | |
| `REQUEST_TIMEOUT_S` | `60` | Raise it if inference runs on CPU |
| `LOG_JSON` | `true` in the image | JSON lines, so Log Analytics parses fields |
| `LOG_LEVEL` | `INFO` | |
| `PORT` | `8000` | The platform injects this; the image honours it |

No secrets are needed. FHIR auth uses `DefaultAzureCredential`, which resolves
to the container's **managed identity** in Azure and to your `az login` session
locally — nothing is stored.

---

## Run the container locally first

```bash
docker build -t fhir-summarize:local .

docker run --rm -p 8001:8000 \
  -e FHIR_BASE_URL=http://host.docker.internal:8080/fhir \
  -e FHIR_PUBLIC_URL=http://localhost:8080/fhir \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  fhir-summarize:local

curl -s localhost:8001/ready | jq .
# {"status":"ready","checks":{"fhir":"ok","model":"ok"}}
```

The image is ~296 MB and excludes `data/`, `scripts/` and `tests/` — the 1.3 GB
dataset is loaded at runtime, never baked in.

---

## Azure resources

```bash
RG=rg-fhir-summarize
LOC=eastus
ACR=acrfhirsummarize            # must be globally unique

az group create -n $RG -l $LOC

# 1. registry, and build the image in the cloud
az acr create -g $RG -n $ACR --sku Basic
az acr build -r $ACR -t fhir-summarize:v1 .

# 2. managed FHIR instead of self-hosted HAPI
az healthcareapis workspace create -g $RG -n hdsfhirws
az healthcareapis workspace fhir-service create \
  -g $RG --workspace-name hdsfhirws -n fhirsvc --kind fhir-R4

# 3. Container Apps environment
az containerapp env create -g $RG -n cae-fhir --location $LOC

# 4. the model, internal ingress only
az containerapp create -g $RG -n ollama --environment cae-fhir \
  --image ollama/ollama:latest \
  --ingress internal --target-port 11434 \
  --cpu 4 --memory 8Gi --min-replicas 1

# 5. the API
FHIR_URL=$(az healthcareapis workspace fhir-service show \
  -g $RG --workspace-name hdsfhirws -n fhirsvc --query serviceUrl -o tsv)

az containerapp create -g $RG -n api --environment cae-fhir \
  --image $ACR.azurecr.io/fhir-summarize:v1 \
  --registry-server $ACR.azurecr.io \
  --ingress external --target-port 8000 \
  --cpu 1 --memory 2Gi --min-replicas 1 --max-replicas 5 \
  --system-assigned \
  --env-vars \
    FHIR_BASE_URL="$FHIR_URL" \
    FHIR_AUTH_SCOPE="$FHIR_URL/.default" \
    OLLAMA_BASE_URL="http://ollama" \
    LOG_JSON=true

# 6. let the API read FHIR, with no credentials anywhere
PRINCIPAL=$(az containerapp show -g $RG -n api --query identity.principalId -o tsv)
FHIR_ID=$(az healthcareapis workspace fhir-service show \
  -g $RG --workspace-name hdsfhirws -n fhirsvc --query id -o tsv)
az role assignment create --assignee "$PRINCIPAL" \
  --role "FHIR Data Reader" --scope "$FHIR_ID"
```

### Probes

```bash
az containerapp update -g $RG -n api \
  --liveness-probe-path /health  --liveness-probe-initial-delay 10 \
  --readiness-probe-path /ready
```

`/health` checks nothing external on purpose: restarting the process does not fix
an unreachable FHIR service, so only a genuinely broken process should fail
liveness. `/ready` checks FHIR (required) and the model (reported but not
required, since `write_summary()` degrades to an empty summary and a packet with
facts and sources is still worth serving).

---

## Things that still need doing

**Ollama's model storage.** The container downloads 2 GB on first start. Either
mount Azure Files at `/root/.ollama`, or bake the model into a derived image —
bigger image, much faster cold start, usually the better trade.

**Data loading.** `scripts/load_synthea.py` POSTs transaction bundles, which is
right for HAPI. Azure Health Data Services bulk-imports **NDJSON from Blob
Storage** via `$import`, so loading becomes: flatten bundles to per-resource-type
NDJSON → upload → trigger `$import`. Run it as a Container Apps Job, not inside
the API. **Verify that `PUT` with a client-assigned id behaves the same there** —
the stable-citation design depends on it, and it should be tested rather than
assumed.

**Postgres.** Only needed if you keep self-hosted HAPI rather than moving to the
managed FHIR service. Use Azure Database for PostgreSQL Flexible Server; a
container's filesystem is ephemeral and a restart would lose the database.

**Networking.** Everything above uses public ingress for brevity. For real data:
private endpoints on FHIR and Key Vault, VNet-integrated Container Apps
environment, and Front Door or Application Gateway with a WAF in front of the
API. The API should be the only thing publicly reachable, and arguably not that.

**Compliance.** Azure's HIPAA BAA must be in place, and every service used has to
be in scope. Note that the prompt itself contains PHI — condition and medication
lists — so whether inference stays inside the tenant boundary is a question the
customer's security review will ask. It is the strongest argument for keeping the
model in the VNet rather than calling an external API.

**CI/CD.** GitHub Actions with OIDC federated credentials (no stored secrets):
`pytest` and `ruff` as gates, then `az acr build` and `az containerapp update`.
The 20 tests need no FHIR server and no model, so they run in CI as-is.

**Infrastructure as code.** The `az` commands above should become Bicep or
Terraform before anyone deploys this for real.

---

## Swapping the model for Azure OpenAI

`summarize.py` makes a single call with a JSON schema, so the change is small:
point `OLLAMA_BASE_URL` at the Azure OpenAI endpoint, rename `format` to
`response_format`, and add the deployment name. Worth doing behind a small
interface rather than editing in place — a customer with Azure OpenAI already
provisioned under a BAA is the common case, and asking them to run GPU VMs for a
3B model is a harder sell than using what they have.

# Single-stage build: the only dependency that needs compiling is already
# published as a wheel, so a builder stage would add complexity without
# shrinking the image meaningfully.
FROM python:3.12-slim

# PYTHONUNBUFFERED so logs reach the container's stdout immediately rather than
# sitting in a buffer when the process is killed -- a lost crash log is the worst
# time to discover buffering.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first so `pip install` is cached across code-only changes.
COPY pyproject.toml README.md ./
COPY app/__init__.py app/__init__.py
RUN pip install ".[azure]"

COPY app/ app/
COPY ui/ ui/

# Run as a non-root user. Azure Container Apps does not require it, but a
# container that never needs root should never have it.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Container-appropriate defaults; the deployment overrides the URLs.
ENV LOG_JSON=true \
    PORT=8000

EXPOSE 8000

# Liveness only -- /health checks nothing external, so a failure here really does
# mean the process is broken and restarting it is the right response. Readiness
# (/ready) is wired up in the platform, which can stop routing without killing
# the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; \
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\").read()"

# sh -c so ${PORT} is expanded -- Azure Container Apps, App Service and Cloud Run
# all inject the port they expect the app to listen on -- and `exec` so uvicorn
# replaces the shell and receives SIGTERM directly. Without the exec, the shell
# swallows the signal and the platform waits out its grace period on every deploy.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]

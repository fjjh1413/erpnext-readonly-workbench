# Existing toolchains remain fixed; upstream Poetry lock is never regenerated.
FROM node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m pip install poetry==1.8.5 && python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /build
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --sync --no-interaction --no-ansi
COPY enterprise/requirements.txt enterprise/requirements.txt
RUN python -m pip install -r enterprise/requirements.txt && python -m pip check

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS enterprise
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 HOME=/tmp LITELLM_LOCAL_MODEL_COST_MAP=True \
    ERP_SUPPORT_SETTINGS=/state/settings.json FORWARDED_ALLOW_IPS=""
RUN groupadd --gid 10001 workbench && useradd --uid 10001 --gid 10001 --no-create-home workbench
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY holmes/ /app/holmes/
COPY LICENSE /app/LICENSE
COPY enterprise/health.py /app/enterprise/health.py
COPY --from=frontend /frontend/dist/ /app/examples/erpnext-support/frontend/dist/
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=8s --start-period=90s --retries=10 \
    CMD ["python", "/app/enterprise/health.py", "api"]
CMD ["python", "-m", "holmes.erpnext_support", "serve", "--settings", "/state/settings.json", "--host", "0.0.0.0", "--port", "8765"]

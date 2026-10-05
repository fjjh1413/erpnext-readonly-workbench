# Fixed toolchains; upstream Poetry lock and frontend npm lock stay unchanged.
FROM node:24.21.0-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12.10-slim-bookworm AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m pip install poetry==1.8.5 && python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /build
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --sync --no-interaction --no-ansi

FROM python:3.12.10-slim-bookworm AS workbench
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    LITELLM_LOCAL_MODEL_COST_MAP=True ERP_SUPPORT_SETTINGS=/state/settings.json
RUN groupadd --gid 10001 workbench && useradd --uid 10001 --gid 10001 --no-create-home workbench
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY holmes/ /app/holmes/
COPY LICENSE /app/LICENSE
COPY --from=frontend /frontend/dist/ /app/examples/erpnext-support/frontend/dist/
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8765/health',timeout=3).read(1024)"
CMD ["python", "-m", "holmes.erpnext_support", "serve", "--settings", "/state/settings.json", "--host", "0.0.0.0", "--port", "8765"]

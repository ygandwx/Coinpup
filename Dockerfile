FROM node:24-bookworm-slim AS web
WORKDIR /build
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/services/api/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home --uid 10001 coinpup
COPY services/api/ services/api/
COPY alembic.ini ./
COPY scripts/ scripts/
COPY --from=web /build/dist apps/web/dist/
USER coinpup
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "coinpup_api.main:create_app", "--factory", "--app-dir", "services/api/src", "--host", "0.0.0.0", "--port", "8000"]

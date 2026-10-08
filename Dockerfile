ARG OCR_NATIVE_IMAGE=coinpup-ocr-tesseract:local
FROM node:24-bookworm-slim AS web
WORKDIR /build
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim-trixie AS api

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/services/api/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home --uid 10001 coinpup \
    && install -d -m 0700 -o coinpup -g coinpup /app/data/files
COPY services/api/ services/api/
COPY alembic.ini ./
COPY scripts/ scripts/
COPY --from=web /build/dist apps/web/dist/
USER coinpup
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "coinpup_api.main:create_app", "--factory", "--app-dir", "services/api/src", "--host", "0.0.0.0", "--port", "8000"]

FROM ${OCR_NATIVE_IMAGE} AS ocr-native
FROM python:3.12.14-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a AS worker
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/services/api/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpng16-16 libjpeg62-turbo libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 coinpup \
    && install -d -m 0700 -o coinpup -g coinpup /app/data/files
COPY --from=ocr-native /opt/tesseract/ /opt/tesseract/
COPY requirements-ocr.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-ocr.lock \
    && python -m pip check
COPY services/api/ services/api/
COPY alembic.ini ./
COPY scripts/check_ocr_worker.py scripts/check_ocr_worker.py
COPY tests/fixtures/ocr/engine-assets.json /opt/coinpup-ocr/engine-assets.json
COPY build/engine-assets/tesseract/fast/eng.traineddata build/engine-assets/tesseract/fast/chi_sim.traineddata build/engine-assets/tesseract/fast/chi_tra.traineddata /opt/coinpup-ocr/tesseract/fast/
COPY build/engine-assets/tesseract/fast/LICENSE /usr/share/doc/coinpup-ocr/models-LICENSE
COPY build/engine-assets/tesseract/source/LICENSE /usr/share/doc/coinpup-ocr/tesseract-LICENSE
COPY build/engine-assets/native-notices/leptonica/leptonica-license.txt /usr/share/doc/coinpup-ocr/leptonica-LICENSE
RUN python -c "from coinpup_api.ocr.runtime import _verify_models; _verify_models()"
USER coinpup
CMD ["python", "-m", "coinpup_api.ocr.worker"]

# Building without --target keeps the lightweight API as the default image.
FROM api AS final

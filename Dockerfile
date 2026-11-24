ARG OCR_NATIVE_IMAGE=coinpup-ocr-tesseract:local
FROM node:24-bookworm-slim AS web
WORKDIR /build
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim-trixie AS python-base

# Fetch only committed font source identities during the build. No builder or
# original variable font enters the API image; generated names and hashes are fixed.
FROM python-base AS invoice-fonts
WORKDIR /build
ENV PYTHONPATH=/build/services/api/src
COPY requirements-benchmark.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-benchmark.lock
COPY services/api/src/coinpup_api/business/invoice_fonts.py services/api/src/coinpup_api/business/
COPY services/api/src/coinpup_api/ledger/errors.py services/api/src/coinpup_api/ledger/
COPY scripts/ocr_benchmark/fonts.py scripts/ocr_benchmark/
COPY scripts/build_invoice_fonts.py scripts/
COPY tests/fixtures/ocr/font-assets.json tests/fixtures/ocr/
RUN python -m scripts.ocr_benchmark.fonts fetch --source-dir /build/font-sources \
    && python scripts/build_invoice_fonts.py /build/font-sources /build/invoice-fonts

FROM python-base AS api

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/services/api/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-invoice.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-invoice.lock \
    && python -m pip check \
    && useradd --create-home --uid 10001 coinpup \
    && install -d -m 0700 -o coinpup -g coinpup /app/data/files
COPY services/api/ services/api/
COPY alembic.ini ./
COPY scripts/ scripts/
COPY --from=web /build/dist apps/web/dist/
COPY --from=invoice-fonts /build/invoice-fonts/ /opt/coinpup/invoice-fonts/
ENV COINPUP_INVOICE_FONT_DIR=/opt/coinpup/invoice-fonts
USER coinpup
RUN python scripts/check_invoice_dependencies.py \
    && python -c "import os; from coinpup_api.business.invoice_fonts import load_invoice_fonts; load_invoice_fonts(os.environ['COINPUP_INVOICE_FONT_DIR'])"
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

# Building without --target keeps the API as the default image.
FROM api AS final

# syntax=docker/dockerfile:1.7
# Independent candidate environments; the default API Dockerfile is unchanged.
FROM --platform=linux/amd64 python:3.12.14-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a AS system-download
WORKDIR /workspace
COPY ops/ocr-benchmark/build-inputs.json ops/ocr-benchmark/build_inputs.py ops/ocr-benchmark/
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' \
    'deb [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://snapshot.debian.org/archive/debian/20261004T000000Z/ bookworm main' \
    'deb [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://snapshot.debian.org/archive/debian/20261004T000000Z/ bookworm-updates main' \
    'deb [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://snapshot.debian.org/archive/debian-security/20261004T000000Z/ bookworm-security main' \
    > /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=60 update \
    && apt-get -y --no-install-recommends --download-only install $(python ops/ocr-benchmark/build_inputs.py apt-specs)

FROM system-download AS system
RUN --network=none apt-get -y --no-install-recommends --no-download install $(python ops/ocr-benchmark/build_inputs.py apt-specs) \
    && python ops/ocr-benchmark/build_inputs.py system --debs-dir /var/cache/apt/archives --output-dir /opt/environment-system \
    && rm -f /var/cache/apt/archives/*.deb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/workspace/services/api/src:/workspace \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    HF_HOME=/tmp/huggingface MODELSCOPE_CACHE=/tmp/modelscope PADDLE_PDX_CACHE_HOME=/tmp/paddlex \
    XDG_CACHE_HOME=/tmp/cache OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
COPY services/api/src/ services/api/src/
COPY scripts/ scripts/
COPY tests/fixtures/ocr/engine-assets.json tests/fixtures/ocr/engine-assets.json
COPY requirements-ocr.lock ./
COPY ops/ocr-benchmark/ ops/ocr-benchmark/
RUN --network=none useradd --uid 10001 --create-home candidate

FROM system AS tesseract-build
RUN --network=none --mount=type=bind,source=build/engine-assets,target=/inputs/assets,readonly \
    --mount=type=bind,source=build/engine-build-inputs,target=/inputs/build,readonly \
    python -m scripts.ocr_benchmark.engine_assets verify --output-dir /inputs/assets \
    && python ops/ocr-benchmark/build_inputs.py unpack --assets-dir /inputs/assets --sources-dir /inputs/build --output-dir /build/sources
RUN --network=none cmake -S /build/sources/leptonica -B /build/leptonica -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/tesseract -DCMAKE_INSTALL_LIBDIR=lib \
    -DBUILD_SHARED_LIBS=ON -DBUILD_PROG=OFF -DSW_BUILD=OFF -DSTRICT_CONF=ON \
    -DENABLE_ZLIB=ON -DENABLE_PNG=ON -DENABLE_JPEG=ON -DENABLE_GIF=OFF -DENABLE_TIFF=OFF -DENABLE_WEBP=OFF -DENABLE_OPENJPEG=OFF \
    && cmake --build /build/leptonica --parallel 2 && cmake --install /build/leptonica
ENV PKG_CONFIG_PATH=/opt/tesseract/lib/pkgconfig LD_LIBRARY_PATH=/opt/tesseract/lib
RUN --network=none cmake -S /build/sources/tesseract -B /build/tesseract -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/tesseract -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_PREFIX_PATH=/opt/tesseract -DCMAKE_INSTALL_RPATH=/opt/tesseract/lib \
    -DBUILD_SHARED_LIBS=ON -DBUILD_TRAINING_TOOLS=OFF -DBUILD_TESTS=OFF -DOPENMP_BUILD=OFF \
    -DGRAPHICS_DISABLED=ON -DDISABLED_LEGACY_ENGINE=ON -DENABLE_NATIVE=OFF -DENABLE_LTO=OFF \
    -DDISABLE_CURL=ON -DDISABLE_ARCHIVE=ON -DDISABLE_TIFF=ON -DENABLE_CCACHE=OFF \
    && cmake --build /build/tesseract --parallel 2 && cmake --install /build/tesseract \
    && python ops/ocr-benchmark/build_inputs.py record --sources-dir /build/sources --lept-build /build/leptonica --tess-build /build/tesseract --output-dir /opt/environment-build/tesseract

FROM system AS tesseract
COPY --from=tesseract-build /opt/tesseract/ /opt/tesseract/
COPY --from=tesseract-build /opt/environment-build/ /opt/environment-build/
RUN --network=none --mount=type=bind,source=build/paddle-wheelhouse,target=/wheelhouse,readonly \
    python -m venv /opt/tesseract-venv \
    && /opt/tesseract-venv/bin/python -m pip install --no-index --find-links=/wheelhouse --require-hashes -r ops/ocr-benchmark/bootstrap.lock \
    && /opt/tesseract-venv/bin/python -m pip install --no-index --find-links=/wheelhouse --require-hashes -r requirements-ocr.lock \
    && /opt/tesseract-venv/bin/python -m pip check
ENV PATH=/opt/tesseract-venv/bin:/opt/tesseract/bin:/usr/local/bin:/usr/bin:/bin \
    LD_LIBRARY_PATH=/opt/tesseract/lib COINPUP_OCR_CANDIDATE=tesseract
USER 10001
CMD ["python", "-m", "scripts.ocr_benchmark.engine_environment", "audit", "--engine", "tesseract", "--assets-dir", "/opt/assets", "--output-dir", "/audit"]

FROM system AS paddle
RUN --network=none --mount=type=bind,source=build/paddle-wheelhouse,target=/wheelhouse,readonly \
    python -m venv /opt/paddle-venv \
    && /opt/paddle-venv/bin/python -m pip install --no-index --find-links=/wheelhouse --require-hashes -r ops/ocr-benchmark/bootstrap.lock \
    && /opt/paddle-venv/bin/python -m pip install --no-index --find-links=/wheelhouse --require-hashes -r ops/ocr-benchmark/paddle.lock \
    && /opt/paddle-venv/bin/python -m pip check \
    && /opt/paddle-venv/bin/python -c "import paddle, paddleocr, paddlex, cv2; print(paddle.__version__, paddleocr.__version__, paddlex.__version__, cv2.__version__)"
ENV PATH=/opt/paddle-venv/bin:/usr/local/bin:/usr/bin:/bin COINPUP_OCR_CANDIDATE=paddle
USER 10001
CMD ["python", "-m", "scripts.ocr_benchmark.engine_environment", "audit", "--engine", "paddle", "--assets-dir", "/opt/assets", "--output-dir", "/audit"]

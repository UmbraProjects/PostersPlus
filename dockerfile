# ── Builder stage ─────────────────────────────────────────────────────────────
# Compiles pycairo (and any future C-extension wheels) against the cairo dev
# headers, then we copy only the resulting wheels into the runtime image so the
# ~200MB of build toolchain doesn't ship to users.
FROM python:3.11-slim AS builder
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libcairo2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels --no-cache-dir -r requirements.txt
RUN find /wheels -type f -name 'opencv_python-*.whl' -delete

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app

# libcairo2 (runtime only — no -dev headers needed) for pycairo;
# gosu for privilege drop in entrypoint.sh;
# tini as PID 1 so orphaned processes get reaped (see CMD below).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    libcairo2 \
    tini \
    && rm -rf /var/lib/apt/lists/*

# skia-python links libEGL.so.1 and libGL.so.1, though the sash is drawn on the
# CPU and never opens a GL context.  The libegl1/libgl1 packages depend on
# Mesa's drivers, which bring LLVM: ~185 MB for code that is never run.  Only
# glvnd's four dispatch libraries are needed for the module to load, so they
# are unpacked from their packages directly (~3 MB).  Nothing else links them.
RUN apt-get update \
    && cd /tmp \
    && apt-get download libegl1 libgl1 libglx0 libglvnd0 \
    && for deb in ./*.deb; do dpkg -x "$deb" /; done \
    && ldconfig \
    && rm -f ./*.deb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
# RapidOCR's GUI OpenCV dependency is API-compatible with the headless wheel we
# intentionally install. Normalize its installed metadata so `pip check` and
# dependency scanners do not report the deliberate substitution as broken.
RUN pip install --no-cache-dir --no-deps /wheels/*.whl \
    && sed -i 's/^Requires-Dist: opencv_python/Requires-Dist: opencv-python-headless/' \
       /usr/local/lib/python3.11/site-packages/rapidocr-*.dist-info/METADATA \
    && pip check \
    && rm -rf /wheels

# Bake the PP-OCRv5 Mobile detector into the image.  TEXTLESS_TEXT_DETECTION is
# on by default, so baking avoids the one-time ~4.6MB runtime download that would
# otherwise stall the first low-vote textless request, and it survives cache-
# volume wipes / works on air-gapped hosts.  Adds ~4.6MB to the image, and makes
# the build depend on PPOCR_MODEL_URL being reachable.  Opt out for a lean image
# (e.g. if you disable detection) — the model then downloads once at runtime:
#   docker build --build-arg BAKE_PPOCR_MODEL=false ...
#   (or set BAKE_PPOCR_MODEL=false in .env when building via compose)
ARG BAKE_PPOCR_MODEL=true
ARG PPOCR_MODEL_URL=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx
ARG PPOCR_MODEL_SHA256=4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae
RUN if [ "$BAKE_PPOCR_MODEL" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl && \
      mkdir -p /app/models && \
      curl -fsSL "$PPOCR_MODEL_URL" -o /app/models/ch_PP-OCRv5_det_mobile.onnx && \
      echo "$PPOCR_MODEL_SHA256  /app/models/ch_PP-OCRv5_det_mobile.onnx" | sha256sum -c - && \
      apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* ; \
    fi

RUN adduser --disabled-password --gecos '' appuser

# The code stays owned by root, so the process serving requests cannot rewrite
# its own code or pages; the cache volume is the only thing it writes, and
# entrypoint.sh hands that to appuser.  (A `chown -R` here also used to copy
# every file into a second layer, static/ included, ~80 MB for nothing.)
# The bytecode is compiled now for the same reason: appuser cannot write
# __pycache__, and main.py is large enough that every worker compiling it at
# start-up is noticeable.
COPY . .
RUN python3 -m compileall -q -l /app \
    && mkdir -p /app/cache \
    && chown appuser:appuser /app/cache

# Run as root so entrypoint.sh can fix cache volume permissions at startup,
# then it drops to appuser via gosu before exec-ing uvicorn.
#
# Exec form on purpose: the shell form ran `sh -c "python3 ... || exit 1"`, and
# when the probe overran its timeout Docker SIGKILLed only the sh it exec'd.
# The python3 child survived, was reparented to PID 1, and sat there as a
# root-owned `[python3] <defunct>` once it finished — uvicorn was PID 1 and
# never reaps children it didn't spawn. One zombie per overrun, which on a
# small VPS mid-render was every few probes. With no shell in between the
# probe process is the one Docker kills. A non-zero exit already marks the
# container unhealthy, so the `|| exit 1` bought nothing.
#
# The probe uses http.client rather than urllib.request: it imports a
# fraction as much, which matters when the CPU is busy compositing and the
# interpreter start-up is most of the probe's budget. The 10s timeout gives
# that start-up room on a loaded 1-vCPU host.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD ["python3", "-c", "import http.client; c = http.client.HTTPConnection('localhost', 8000, timeout=8); c.request('GET', '/health'); r = c.getresponse(); raise SystemExit(0 if r.status == 200 else 1)"]
# tini as PID 1: reaps any orphan that lands on it and forwards signals, so a
# process Docker leaves behind (an exec'd shell's child, a killed probe) can
# never accumulate as a zombie. entrypoint.sh still execs into uvicorn.
CMD ["/usr/bin/tini", "--", "/bin/sh", "entrypoint.sh"]

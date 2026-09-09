# Local, self-hosted text-to-speech for DJ breaks. No API key, no per-use cost.
# Speaks a plain HTTP endpoint: POST / with the text body -> WAV.
#
# Uses the standalone Piper *binary* (bundles its own phonemizer + onnxruntime) rather
# than `pip install piper-tts`, whose piper-phonemize dependency has no working wheel on
# current Python. A ~20-line Flask wrapper shells out to it.
FROM python:3.12-slim

ARG PIPER_VERSION=2023.11.14-2
ARG PIPER_VOICE=en_US-lessac-medium
ENV PIPER_VOICE=${PIPER_VOICE}
ENV LD_LIBRARY_PATH=/opt/piper

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir flask==3.0.3

# Piper binary, matched to the build platform's architecture.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) pa=x86_64 ;; \
      arm64) pa=aarch64 ;; \
      armhf) pa=armv7l ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    wget -qO /tmp/piper.tar.gz \
      "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_${pa}.tar.gz"; \
    tar -xzf /tmp/piper.tar.gz -C /opt; \
    rm /tmp/piper.tar.gz

# Voice model (two files) baked in, so the container needs no network at runtime. The
# HF path is derived from the PIPER_VOICE name (<lang>-<name>-<quality>), so overriding
# the build arg is enough to switch voices.
RUN set -eux; \
    mkdir -p /voices; cd /voices; \
    v="${PIPER_VOICE}"; \
    lang="${v%%-*}"; rest="${v#*-}"; name="${rest%-*}"; qual="${rest##*-}"; fam="${lang%%_*}"; \
    base="https://huggingface.co/rhasspy/piper-voices/resolve/main/${fam}/${lang}/${name}/${qual}"; \
    wget -q "${base}/${v}.onnx"; \
    wget -q "${base}/${v}.onnx.json"

COPY server.py /server.py
EXPOSE 5000
CMD ["python3", "/server.py"]

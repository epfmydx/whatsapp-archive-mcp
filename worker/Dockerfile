FROM debian:bookworm-slim AS native
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl cmake g++ make && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://codeload.github.com/handy-computer/transcribe.cpp/tar.gz/8facde6fcd8fb9caa585a5788ab450df6e8ec7db -o /tmp/source.tar.gz \
 && echo '69d5f0f10ebaf4ea3d91b10d64abf275971398bb0c64c1798e34b4632d62be73  /tmp/source.tar.gz' | sha256sum -c - \
 && mkdir /src && tar -xzf /tmp/source.tar.gz --strip-components=1 -C /src \
 && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DTRANSCRIBE_BUILD_TESTS=OFF -DTRANSCRIBE_BUILD_TOOLS=OFF -DTRANSCRIBE_BUILD_SHARED=OFF -DTRANSCRIBE_METAL=OFF -DTRANSCRIBE_CUDA=OFF -DTRANSCRIBE_VULKAN=OFF -DTRANSCRIBE_HIP=OFF -DTRANSCRIBE_USE_OPENMP=OFF -DTRANSCRIBE_USE_SYSTEM_BLAS=OFF -DGGML_NATIVE=OFF -DTRANSCRIBE_X86_CONSERVATIVE=ON \
 && cmake --build /build --target transcribe-cli -j2
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 worker && useradd --uid 10001 --gid worker --no-create-home worker \
 && mkdir /model-cache && chown worker:worker /model-cache
COPY --from=native /build/bin/transcribe-cli /usr/local/bin/transcribe-cli
COPY --from=native /src/LICENSE /licenses/transcribe.cpp-LICENSE
COPY --from=native /src/ggml/LICENSE /licenses/ggml-LICENSE
WORKDIR /worker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py pins.json ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MODEL_PATH=/model-cache/Qwen3-ASR-0.6B-Q8_0.gguf WORKER_THREADS=1 AUDIO_CHUNK_SECONDS=30 MAX_AUDIO_SECONDS=1800
USER 10001:10001
ENTRYPOINT ["python", "worker.py"]

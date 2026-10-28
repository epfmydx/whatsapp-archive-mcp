# CPU transcription worker

The worker runs the actual pinned `transcribe.cpp` CPU executable and the matching Qwen3-ASR-0.6B Q8_0 model. The runtime revision, source archive hash, model revision/size/SHA-256 and licenses are recorded in [pins.json](pins.json). Model downloads are verified before use; a configured model URL/hash cannot silently replace the approved pair.

It accepts only an attempt-authenticated job from the internal application API, reads the job's account-scoped S3 object or read-only disk file, requires its exact authenticated archived size and SHA-256 (including a stream hash independent of S3 metadata), then probes duration, converts bounded chunks to mono 16 kHz WAV, and invokes the native executable with one thread by default. A substituted object or truncated download fails before any decoder runs; partially downloaded files are removed. Limits bound media size, duration, subprocess time and output. It posts a transcript or sanitized failure using the same attempt token. It does not have a database credential, Kubernetes API token or WhatsApp client.

The selected runtime output mode does not expose verified timestamps or detected language. Results return `segments: []` and `language: null`. This is transcription; sound-event/emotion analysis is not implemented. Model accuracy on Malaysian mixed speech/Hokkien is unverified.

```sh
python3 -m unittest discover -s worker/tests -p 'test_*.py' -v
docker build -t whatsapp-mcp-worker:dev worker
python3 -m unittest discover -s charts/whatsapp-mcp/tests -p 'test_*.py' -v
helm lint --strict charts/whatsapp-mcp
```

Worker tests are synthetic. Chart tests require Helm and PyYAML. Actual Linux arm64 CPU evidence for public 11-second speech and a synthetic 33-second repetition is in [native-benchmark-results.json](tests/native-benchmark-results.json). Under a hard 2Gi memory limit, 1.5 CPU and no swap, these fixtures completed in 5.761s and17.040s with cgroup peak1,459,933,184bytes (includes file cache), no OOM events. This is not universal memory/accuracy or production AMD64 qualification.

The Helm chart keeps a conservative 3Gi memory request/limit for the initial trial, one global worker, and persistent disk model cache. Requests reserve scheduling capacity. The controller retains pending work and reconciles one deterministic Job name per attempt; it applies cleanup only after durable result reconciliation. Helm changes to the worker template trigger a controller rollout.

Optional transcription is controlled by `worker.enabled` in Helm and
`ASR_ENABLED` in API/controller. Disabled mode keeps media archiving without new
jobs. Disk mode uses `STORAGE_BACKEND=disk` and the same `MEDIA_ROOT` archive as
the bridge/API; no S3 credentials are required. Size/hash checks remain before
decoding for both backends. See [configuration](../docs/configuration.md).

Source chart 0.7.3 and the updated worker accept positive `worker.threads` /
`WORKER_THREADS` integers without the former two-thread maximum. CPU/RAM remain
Helm resource values; defaults stay one thread and 3Gi memory. Model/runtime pins,
media/duration bounds, authenticated integrity checks and optional-ASR behavior
remain unchanged. See [ASR tuning](../docs/asr-tuning.md) and the
[structured public-audio benchmark](tests/thread-benchmark-results.json).

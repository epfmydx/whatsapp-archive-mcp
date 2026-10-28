# ASR threads and resource allocation

In source chart version 0.7.3, `worker.threads` accepts a positive integer with no
fixed application/chart maximum. It becomes `WORKER_THREADS` and is forwarded
to the pinned native executable's `--threads` argument. Zero, negatives,
fractions, booleans and malformed values are invalid. Native/runtime and host
capacity still apply; a valid configuration is not proof that an allocation fits.
This configures parallel CPU work inside one transcription, not additional Jobs.

CPU and RAM come from `worker.resources.requests` / `worker.resources.limits`.
There is no separate application RAM ceiling. Requests determine scheduling
capacity; limits cap the running container. Increasing a memory limit does not
make the model use that much memory or automatically speed it up. Keep a finite
container memory limit and choose CPU allocation from node allocatable capacity
after system/DaemonSet requests; a four-vCPU node need not fit a four-CPU request.

Defaults remain one thread, CPU request `1000m`, CPU limit `1500m`, and memory
request/limit `3Gi`. Model/runtime/dependency pins and all media, duration, output,
context and subprocess bounds remain unchanged.

An infrastructure override for an initial two-thread trial:

```yaml
worker:
  threads: 2
  resources:
    requests: {cpu: "2", memory: 3Gi}
    limits: {cpu: "2", memory: 3Gi}
```

Larger servers can select higher thread counts and allocations without another
application change. A higher thread count does not increase the CPU quota.
Compare processing time, CPU use/throttling, memory peak and failures using the
same audio on the actual node. Existing Jobs keep their original Pod settings;
new Jobs use the changed template. Worker template changes retain the controller
rollout checksum behavior. Updating Helm values is an infrastructure rollout,
not something performed by the release pipeline.

## Local thread-scaling findings — 28 October 2026

Actual Qwen3-ASR-0.6B Q8_0 inference through the worker's conversion/chunking path:

| Audio | 1-thread median | 2-thread median | 4-thread median |
| --- | ---: | ---: | ---: |
| Public 11-second English sample | 5.96s | 3.92s | 2.26s |
| Synthetic threefold repetition, 33 seconds | 17.73s | 10.85s | 6.52s |

All settings used the same native ARM64 image and model, four-CPU quota, CPU set
0–3, 3Gi RAM cap, no swap and no network. Three repetitions each, serial order
1/2/4/4/2/1/2/4/1. Four threads reduced elapsed time by 40–42% versus two and
averaged 3.19–3.44 CPU cores. Whole-container memory peaks stayed about 1.36Gi;
no errors/OOM events occurred. Transcript hashes matched across these fixtures.
CPU time increased somewhat despite lower elapsed time; this is not evidence of
CPU-cost savings. Memory peaks include file cache, Python, tmpfs and both
fixtures; child RSS is cumulative within each container.

Model hash scans preceded timing, warming the file cache. Timings include audio
conversion and per-invocation model initialization; downloads, hash scans,
node/Pod startup, queues, storage and callbacks are excluded. The historical
benchmark used a test-only Config override above two threads because the product
bound was still two at that checkpoint. The native transcription function was
unchanged. Tests of the updated configuration are recorded in verification.

These small English fixtures do not establish AMD64 throughput, cold-start cost,
mixed-language/Hokkien accuracy or universal memory requirements. The measured
four-CPU budget does not establish available CPU on a particular deployment node.
See [structured results](../worker/tests/thread-benchmark-results.json) and
[verification](verification.md). This public repository provides source and a
chart; build your own matching images. Source publication performs no deployment.

## Operator feedback

An operator reported approximately 2.5× improvement after ASR tuning on a
Kubernetes cluster before approving this source port. Hardware, audio and
before/after allocations were not captured as structured benchmark evidence;
this report is separate from the controlled ARM64 measurements above.

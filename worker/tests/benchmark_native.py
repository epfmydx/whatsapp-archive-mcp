"""Run inside built CPU image against the public upstream JFK sample; no private media.

Example: mount upstream samples at /samples and pinned cache at /model-cache,
then python /worker-tests/benchmark_native.py /samples/jfk.wav.
This reports cgroup file-cache-inclusive peak separately from native child RSS.
"""
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time
import wave
sys.path.insert(0, "/worker")
import worker

source = Path(sys.argv[1])
env = {**os.environ, "JOB_ID": "932a7034-bc96-4356-b886-a9b6e9a44959", "JOB_TOKEN": "public-native-benchmark", "INTERNAL_TOKEN": "unused-public-benchmark", "APP_INTERNAL_URL": "http://unused:8000"}
cfg = worker.Config(env)
model = worker.ensure_model(cfg)
results = []
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    repeated = root / "repeated.wav"
    with wave.open(str(source)) as wav:
        parameters = wav.getparams()
        data = wav.readframes(wav.getnframes())
    with wave.open(str(repeated), "wb") as wav:
        wav.setparams(parameters)
        wav.writeframes(data * 3)
    for name, audio in (("public_jfk", source), ("synthetic_3x_jfk_repeated", repeated)):
        duration = worker.probe_duration(audio, cfg)
        started = time.monotonic()
        try:
            result = worker.transcribe(audio, model, cfg, root)
        except worker.WorkerError as error:
            result = {"text": "", "segments": [], "language": None, "error": str(error)}
        results.append({"fixture": name, "audio_seconds": duration, "elapsed_seconds": round(time.monotonic() - started, 3),
                        "error": result.get("error"), "text": result["text"], "segments": result["segments"], "language": result["language"],
                        "native_children_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss})
peak = Path("/sys/fs/cgroup/memory.peak")
events = Path("/sys/fs/cgroup/memory.events")
print(json.dumps({"results": results, "cgroup_memory_peak_bytes_including_file_cache": peak.read_text().strip() if peak.exists() else None,
                  "cgroup_memory_events": events.read_text() if events.exists() else None,
                  "notes": "Public speech and synthetic repeated speech only. Single Linux arm64 CPU run, no WER evaluation or noisy multilingual acceptance. RSS is max across child processes including ffmpeg. Cgroup peak includes file cache."}, indent=2))

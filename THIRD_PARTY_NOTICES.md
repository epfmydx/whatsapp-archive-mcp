# Third-party notices

Project-authored code is GPL-3.0-only, copyright 2026 EPFMy DigitalX Services.
The full GPLv3 text is in [LICENSE](LICENSE). Dependencies and upstream material
retain their respective copyrights and licenses; the project license does not
replace them. Preserve notices when copying source or distributing artifacts.

| Component | Pinned source / record | License |
| --- | --- | --- |
| Whatsmeow | `go.mau.fi/whatsmeow`, revision `1fbae988179a` in `bridge/go.mod` | MPL-2.0 |
| libsignal-protocol-go | `go.mau.fi/libsignal v0.2.2` | GPL-3.0 |
| MinIO Go SDK | `github.com/minio/minio-go/v7 v7.0.95` | Apache-2.0 |
| transcribe.cpp | `handy-computer/transcribe.cpp`, revision `8facde6fcd8fb9caa585a5788ab450df6e8ec7db` | MIT |
| ggml | revision `d7cb574130e6f01ad25b3289685489200febcd74`, runtime dependency | MIT |
| Qwen3-ASR-0.6B weights | original Qwen model and pinned quantized model in `worker/pins.json` | Apache-2.0 |
| lharries/whatsapp-mcp | architectural inspiration; copyright 2025 Luke Harries | MIT |

Copies of the above library/runtime license texts are under `LICENSES/`.
The Qwen model downloads at runtime and is not committed or embedded in source;
retain its upstream license/attribution when redistributing weights. Source and
model checksums are in `worker/pins.json`.

The full Go dependency graph is pinned in `bridge/go.mod` and `bridge/go.sum`.
The [Go notice index](LICENSES/go-dependencies.json) records copied top-level
notices for all 32 pinned modules. The [Python notice index](LICENSES/python-dependencies.json)
records matching locally installed package notices and explicitly marks versions
not present in the qualification environment; it is not a full image SBOM.
Python dependencies are pinned in `service/requirements.lock` and
`worker/requirements.txt`. Their transitive licenses also apply. Native images
include base-system packages and FFmpeg, whose actual build/license configuration
must be included in the distribution review; invoking an external executable
is not a claim that all packaged components share one license.

Before distributing binaries or container images, preserve applicable license
texts/notices and provide the required corresponding source/build material for
GPL/MPL-covered components. An SBOM or an upstream URL alone does not establish
that distribution obligations are fulfilled. Public binary/image publication is
not performed during this source preparation.

DigitalX logos are the owner's approved artwork. This code license does not grant
trademark permission to imply that a fork or service is endorsed by the brand.
WhatsApp is a third-party trademark; this project is unofficial and unaffiliated.

Upstream license references: [Whatsmeow](https://github.com/tulir/whatsmeow/blob/main/LICENSE),
[libsignal v0.2.2](https://github.com/tulir/libsignal-protocol-go/blob/v0.2.2/LICENSE),
[Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B),
[Luke Harries' project](https://github.com/lharries/whatsapp-mcp/blob/main/LICENSE).
The generic Apache-2.0 terms are included in `LICENSES/Apache-2.0.txt`.

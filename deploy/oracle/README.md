# Oracle runtime validation

Build Oracle's Core with `PIN_NETCUP_RUNTIME=true` and `SOURCE_REVISION` set to
the exact source commit. `runtime-constraints.txt` records the inspected Netcup
runtime; verify the resulting renderer and source recipe rather than assuming
dependency pins alone imply parity.

For a bounded x86 validation build, `dockerfile.runtime-overlay` copies the same
source commit over an inspected existing runtime image. Pass its immutable image
SHA as `VERIFIED_BASE_IMAGE` and the source commit as `SOURCE_REVISION`. This is
a test image and does not replace or restart the production Core service.

Run `run-offline-golden.sh IMAGE - FIXTURE_DIR arm` on Oracle and the equivalent
`x86` invocation on Netcup. The `-` executes the actual image's `/app` code with
no source bind. An explicit source directory is supported for development
checkpoints but must not be represented as immutable image execution evidence.
The test container has no network, bounded CPU/memory and a local tmpfs ledger.
Fixtures and exported images can reside on the separate incoming NFS share.

`compare-golden.py` compares both complete reports and verifies every exported
normalized source and WebP against its digest and size. The representative set
contains the four reported media IDs and the three current English presets.
`benchmark-golden.py` measures the real detector in the same offline image;
detector timings use a warmed model, while total pipeline time includes startup.

September 14 integration evidence: 329 tests passed, one optional test skipped,
six subtests passed. A source-bind checkpoint on pinned ARM/x86 runtimes produced
identical OCR decisions, normalized sources and twelve WebPs. All twelve portrait
candidates failed OCR, so all four titles correctly retained their existing
backdrop/logo sources. Oracle detector median was 0.142s, p95 0.155s; the four-title
offline pipeline took 16.43s. These small-sample timings exclude network latency.
Final image identities and the matching immutable-image run belong in the PR
and companion BingeCat release evidence before activation.

Do not leave test fixture directories in incoming when activating the registry:
its flat protocol scan deliberately rejects unexpected directories. Preserve
the required reports, manifests and exported evidence separately first. No active
SQLite file is shared across hosts; incoming cleanup remains owner-only.

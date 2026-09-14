#!/usr/bin/env bash
# Run on either host with the same frozen source and NFS fixture directory.
set -euo pipefail
if (($# != 4)); then
  echo 'Usage: run-offline-golden.sh IMAGE SOURCE_DIR|- FIXTURE_DIR OUTPUT_NAME' >&2
  exit 64
fi
golden_image=$1
golden_source=$2
golden_fixture=$(realpath -e "$3")
golden_output=$4
golden_cpus=${GOLDEN_CPUS:-1}
[[ $golden_output =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]] || exit 64
[[ $golden_cpus =~ ^[1-4]$ ]] || exit 64
source_mount=()
golden_workdir=/app
if [[ $golden_source != - ]]; then
  golden_source=$(realpath -e "$golden_source")
  test -f "$golden_source/offline_golden.py"
  source_mount=(--mount "type=bind,src=$golden_source,dst=/validation,readonly")
  golden_workdir=/validation
fi
test -f "$golden_fixture/inputs.json"
test -f "$golden_fixture/production-facts.json"
# SQLite and temporary normalized sources are confined to this small tmpfs.
# The real source-owner ledger is never mounted into the test container.
exec docker run --rm --network none --cpus "$golden_cpus" --memory 2g \
  --read-only --tmpfs /tmp:rw,size=256m --workdir "$golden_workdir" \
  "${source_mount[@]}" \
  --mount "type=bind,src=$golden_fixture,dst=/fixtures" \
  -e TEXTLESS_DETECTION_CONCURRENCY=1 -e TEXTLESS_DETECTION_THREADS=2 \
  -e OMP_NUM_THREADS=1 -e OPENBLAS_NUM_THREADS=1 \
  "$golden_image" python offline_golden.py export /fixtures \
  "/fixtures/golden-$golden_output.json" \
  --artifact-dir "/fixtures/out-$golden_output" \
  --production-facts /fixtures/production-facts.json \
  --preserved-sources /fixtures/preserved_sources

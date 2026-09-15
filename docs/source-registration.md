# Core source registration contract

Status: agreed for the Oracle prewarm/Core source-store split on 2026-09-14.

This is the wire contract between an Oracle Core source client and the
separately deployable Netcup source owner. The coordination copy is
`/tmp/oracle-prewarm-work/core-source-interface.md`.

The protocol is independent of the renderer revision. Nothing here is part of
a render hash, and `RENDERER_REVISION` plus all existing renderer hash
closures remain unchanged.

## Roles and storage

Netcup is the only source-ledger writer and the only process that creates or
replaces files below the published source-art cache. It validates, hashes,
registers, and verifies metadata. It never downloads provider URLs, runs OCR,
or renders posters.

Oracle owns provider downloads, normalization, and OCR. It never opens
`source_art.sqlite`, `artifact cache_access.sqlite`, or another source
ledger. Raw provider downloads and normalized registration payloads are staged
directly in `POSTERSPLUS_SOURCE_INCOMING_DIR`, a bounded RW NFS directory.
Published derivatives are read from `SOURCE_ART_CACHE_DIR`, a read-only NFS
directory. A missing, non-directory, symlinked, or unavailable mount fails
closed; there is no local fallback.

Remote and owner modes require mount validation (legacy local mode defaults to
off unless explicitly enabled). When `POSTERSPLUS_SOURCE_REQUIRE_MOUNTS=true`, validation reads
`/proc/self/mountinfo` and requires an exact mountpoint entry for both paths;
a parent `/app/cache` mount does not satisfy `/app/cache/source_art`. Oracle
requires filesystem type `nfs` or `nfs4`. The standalone owner requires an
exact target on an allowlisted local filesystem and rejects network filesystems,
so Root should provide the explicit `/opt/postersplus/cache/source_art` bind
alongside the whole-cache bind. `POSTERSPLUS_SOURCE_MOUNTINFO_PATH` exists
only to point tests at a fixture; production keeps the default.
Setting it false is rejected for remote or owner mode.

The Oracle adapter preserves the legacy `get`, `install`,
`reserve_capacity`, and `release_capacity` shape. Its `root` is the
incoming directory so the existing `fetch_derivative` downloader writes raw
bytes to NFS. Its `get` result reconstructs an absolute path only from a safe
owner-supplied relative artifact path and `SOURCE_ART_CACHE_DIR`.

## Authentication and replay protection

Every source-owner request is an HTTP POST with a JSON body and the existing
`service_auth` v1 request signature. The source channel is separate from
`POSTERSPLUS_BINGECAT_REQUEST_SECRET`:

```text
POSTERSPLUS_SOURCE_STORE_MODE=remote       # Oracle client
POSTERSPLUS_SOURCE_REGISTRY_MODE=disabled  # Oracle; owner sets this to owner
POSTERSPLUS_SOURCE_REGISTRY_URL=http://100.71.209.127:18087
POSTERSPLUS_SOURCE_REGISTRY_SECRET=<separate-secret>
POSTERSPLUS_SOURCE_DOWNLOAD_CONCURRENCY=1  # one permit per Oracle Core process
caller=oracle-core
audience=postersplus-source-owner
```

The future Netcup local-Core compatibility image may set
`POSTERSPLUS_SOURCE_ACCOUNT_INCOMING=true` with a read-only
`POSTERSPLUS_SOURCE_INCOMING_DIR` bind. Unset local mode preserves the legacy
accounting seam. With the flag, Core usage and source reservations account for
both direct-child incoming files and the historical `SOURCE_ART_CACHE_DIR/tmp`
pool, plus active shared reservations. The source ledger is counted only in
`source_derivatives`; it is not charged again as physical staging.

The mode defaults to `local` only when the variable is unset. Any other
value fails startup; a typo cannot silently select a local ledger. In remote
mode the legacy cache usage/prune routes are unavailable so Core cannot invoke
the local source-pruner accidentally.

The six `X-PostersPlus-*` headers are the existing
`build_auth_headers` output: timestamp, body SHA-256, UUIDv4 request ID,
caller, audience, and HMAC-SHA256 signature. The signed bytes are the existing
v1 canonical request bytes: auth version, caller, audience, method, decoded
path, request ID, timestamp, and body SHA-256 separated by newlines. Query
strings are forbidden. The owner consumes request IDs atomically before doing
work; replays, stale timestamps, body tampering, wrong path, wrong
caller/audience, and wrong secret are rejected. Retries use a fresh request ID;
the reservation token gives register/release exclusivity, and a repeated
digest converges only when its unexpired reservation is still present.

The owner uses a dedicated replay nonce store. This is not the source ledger
and is never opened by Oracle.

`POSTERSPLUS_SOURCE_DOWNLOAD_CONCURRENCY` is a per-process semaphore bounded
to `1..2`; it is not a cross-process rate limiter and requires neither Redis
nor shared SQLite. With two Oracle Core processes, set it to `1` in both
processes to keep the stack-wide source-download maximum at `2`. The local
legacy default remains `2`.

Remote Oracle Core does not acquire the legacy background leader and does not
start local prune, digital-release, cache-warm, trending, or provider-refresh
loops. The source owner remains the only source-ledger maintenance owner.
The source owner does not rate-limit TMDB metadata: Root's app coordinator
must keep enrich starts at one per second across the two Core processes. The
artwork-only path is compatible with that budget only when it makes exactly
one TMDB images request per title; a multi-image path needs a separately
agreed owner permit seam.

## Standalone Netcup owner entrypoint

Root may deploy the owner independently of the live Core with one private
service (one worker, no background loops):

```text
uvicorn source_registry:app --host 0.0.0.0 --port 8000 --workers 1
```

The intended host bind is `100.71.209.127:18087 -> 8000`. The service mounts
the same Netcup-host source state used by the owner:

```text
/opt/postersplus/cache:/app/cache
/opt/postersplus/source_incoming:/app/source_incoming
```

Required owner environment is:

```text
POSTERSPLUS_SOURCE_REGISTRY_MODE=owner
POSTERSPLUS_SOURCE_STORE_MODE=local
SOURCE_ART_CACHE_DIR=/app/cache/source_art
SOURCE_ART_LEDGER_PATH=/app/cache/source_art.sqlite
POSTERSPLUS_SOURCE_INCOMING_DIR=/app/source_incoming
POSTERSPLUS_SOURCE_REGISTRY_SECRET=<same separate secret as Oracle>
POSTERSPLUS_SOURCE_REGISTRY_NONCE_DB_PATH=/app/cache/source_registry_nonces.sqlite
POSTERSPLUS_SOURCE_REQUIRE_MOUNTS=true
SOURCE_CACHE_MAX_BYTES=15000000000
```

`/health` is the only unauthenticated route and returns `200
{"status":"ok","role":"source-owner"}` only when the source cache,
incoming directory, ledger, and nonce store are available. Missing mounts or
ledger initialization return `503`. The owner exposes only the six signed
`/v2/source-art/{reserve,lookup,register,release,verification-lookup,verification-register}`
routes plus `/health`; it has no `/v2/enrich`, `/v2/render`, ratings/status
poller, Redis dependency, or source-pruner task. Expired reservation rows are
cleaned as part of bounded reserve/release operations.

## Common JSON rules

All bodies have exactly the keys documented below, are UTF-8 JSON, and obey the
existing 256 KiB request cap. The common source envelope is:

```json
{"schema":"postersplus.source_art","version":1}
```

`sha256` values are lower-case 64-character hex. `kind` is one of
`poster`, `backdrop`, or `logo`. Current recipe versions are poster 1,
backdrop 5, and logo 1. Normalized MIME values are `image/jpeg` or
`image/png`. Provider locators use the existing strict `ArtworkLocator`
shape and only the existing TMDB, TVDB, and Metahub HTTPS hosts.

Timestamps are RFC3339 UTC strings with an explicit `Z` or `+00:00`, bounded to
64 bytes.
Staged filenames are one direct-child basename only, 1–128 bytes, matching
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. Slash, backslash, dot, dot-dot, NUL,
encoded separators, symlinks, non-regular files, and hard-link surprises are
rejected.

## Reservation API

### POST /v2/source-art/reserve

Request:

```json
{
  "schema":"postersplus.source_art",
  "version":1,
  "required_bytes":16777216,
  "ttl_seconds":300
}
```

`required_bytes` is positive and at most 16 MiB. `ttl_seconds` is positive
and at most 900; the default is 300. The owner atomically removes expired
reservations, accounts for ledger bytes, active reservations, and all bounded
incoming staging files, and admits the reservation only when the
15,000,000,000-byte source cap remains available. The reservation is the
exclusivity token for its later register.

Owner capacity also includes direct-child files in the local
`SOURCE_ART_CACHE_DIR/tmp` pool. That pool contains legacy active downloads
and crash orphans; it is counted alongside incoming NFS, without a recursive
walk. Owner incoming staging is flat and fail-closed: every direct child must
be a private regular file, and any directory, symlink, hard link, or other
non-regular entry blocks reservation. Legacy local `tmp` staging retains its
existing non-regular-entry skip behavior.

There is no standalone owner pruner. Each signed owner `reserve` performs a
bounded cleanup while holding the source-ledger `BEGIN IMMEDIATE` lock. It
scans at most 4096 direct incoming entries and removes only stale files whose
names exactly match generated `raw-xxxxxx` or
`normalized-{poster,backdrop,logo}-{32-hex}.{jpg,png}` forms. Staleness uses
`SOURCE_CACHE_RAW_MAX_AGE_SECONDS` (24 hours by default). Files created during
an active reservation's protected start window are retained; unknown regular
files are retained. Symlinks, directories, hard links, changed candidates,
or scan overflow fail closed. Oracle and local Core never delete incoming.

This is not an absolute cross-writer cap until every writer uses the same
accounting seam. Existing legacy writers share
`source_art_capacity_reservations` and count `SOURCE_ART_CACHE_DIR/tmp`, but
do not see separate `POSTERSPLUS_SOURCE_INCOMING_DIR` bytes. Before relying on
15 GB as a global cap, the compatible legacy bridge must either scan that
incoming directory too (as a separately mounted/configured capacity root) or
stop placing files there; this owner change does not alter the live Netcup
bridge.

Response 200:

```json
{
  "schema":"postersplus.source_art",
  "version":1,
  "reservation_token":"<opaque-url-safe-token>",
  "expires_at":"2026-09-14T12:00:00Z",
  "reserved_bytes":16777216
}
```

The token is opaque, at most 128 characters, and is never logged. A hard
capacity failure is `409 source_capacity_exceeded`; an owner/mount/ledger
failure is `503 source_owner_unavailable`.

### POST /v2/source-art/release

Request:

```json
{
  "schema":"postersplus.source_art",
  "version":1,
  "reservation_token":"<opaque-url-safe-token>"
}
```

Response 200 is `{"schema":"postersplus.source_art","version":1,"released":true}`
when an active token was removed and the same response with `false` when it
was already consumed, expired, or unknown. Invalid token shape is 422.

## Derivative lookup and registration

### POST /v2/source-art/lookup

Request:

```json
{
  "schema":"postersplus.source_art",
  "version":1,
  "kind":"poster",
  "recipe_version":1,
  "sha256":"<normalized-payload-sha256>"
}
```

Miss response:

```json
{"schema":"postersplus.source_art","version":1,"found":false}
```

Hit response:

```json
{
  "schema":"postersplus.source_art",
  "version":1,
  "found":true,
  "derivative":{
    "source_art_id":"poster-r1-<sha256>",
    "kind":"poster",
    "sha256":"<normalized-payload-sha256>",
    "byte_size":123456,
    "mime":"image/jpeg",
    "recipe_version":1,
    "artifact_relpath":"poster/ab/<sha256>.jpg",
    "width":500,
    "height":750,
    "locator":{"provider":"tmdb","url":"https://image.tmdb.org/t/p/w500/abc.jpg"},
    "created_at":"2026-09-14T11:00:00Z",
    "last_used_at":"2026-09-14T11:00:00Z",
    "pinned":false,
    "reconstructable":true
  }
}
```

The owner returns only a safe relative path below the artifact root; it never
asks Oracle to trust an arbitrary absolute path. A hit is returned only after
no-follow regular-file validation and a bounded SHA-256/byte-size check. A
ledger row whose file is missing or corrupt is not a usable hit. The owner
updates `last_used_at` atomically with a successful lookup.

### POST /v2/source-art/register

Oracle first normalizes a provider download with the existing
`_normalize_payload` recipe. It writes that exact normalized byte payload to
the incoming NFS directory, fsyncs it, and sends:

```json
{
  "schema":"postersplus.source_art",
  "version":1,
  "reservation_token":"<active-token>",
  "staged_filename":"normalized-poster-r1-<random>.jpg",
  "kind":"poster",
  "recipe_version":1,
  "sha256":"<normalized-payload-sha256>",
  "byte_size":123456,
  "mime":"image/jpeg",
  "width":500,
  "height":750,
  "locator":{"provider":"tmdb","url":"https://image.tmdb.org/t/p/w500/abc.jpg"},
  "pinned":false,
  "reconstructable":true
}
```

`locator` is optional only when `reconstructable` is false. The owner opens
the staged file with no-follow semantics, bounds the read at 16 MiB, checks
regular-file type, declared MIME against image magic/Pillow format, exact byte
size, SHA-256, dimensions, recipe kind, and metadata bounds. It then performs
one owner-side transaction: validate the active reservation, atomically create
or verify the content-addressed artifact, insert/update the source ledger row,
consume the reservation, and return the derivative.

A corrupt or tampered staged file leaves both ledger and reservation unchanged.
The owner removes the staged file only after a successful commit; failed
validation leaves it counted as staging pressure for bounded operator cleanup.
Re-registering the same digest/kind/recipe with
a fresh valid reservation converges on the existing row and never duplicates
it. The registry never follows the locator and never invokes a downloader, OCR
engine, or renderer.

## Artwork-only enrichment and candidate payload

The Core /v2/enrich request keeps the existing
`bingecat_postersplus_v2` schema/version and gains an optional strict boolean
`artwork_only`, default false. An artwork-only request has the same render
target fields as a normal request plus the complete, provenance-bound facts
snapshot from the app:

```json
{
  "schema":"bingecat_postersplus_v2",
  "version":1,
  "artwork_only":true,
  "media":{"media_type":"movie","tmdb_id":11,"imdb_id":"tt0133093"},
  "locales":["en","nl"],
  "titles_by_locale":{"en":"The Matrix","nl":"The Matrix"},
  "canonical_configs":[{"...":"existing canonical render config"}],
  "known_ratings":[{"...":"existing ProviderRating values"}],
  "known_facts":{
    "values":{"genre":"Sci-Fi","release_year":1999},
    "provenance":[{
      "fields":["genre","release_year"],
      "source":"bingecat",
      "observed_at":"2026-09-13T12:00:00Z",
      "checked_at":"2026-09-13T12:00:00Z",
      "expires_at":"2026-09-20T12:00:00Z"
    }]
  },
  "known_source_art":[]
}
```

With `artwork_only:true`, Core must not call identity resolution,
rating/MDBList, TMDB fact/release/trending, or any other provider-facts hook.
It reuses active `known_facts` and unexpired `known_ratings` as supplied,
refreshes only missing/expired artwork, and preserves existing usable artwork
when no candidate improves it. The app sends all facts needed by its selected
configs; missing facts stay missing/partial and are not fetched as a side
effect. This flag never changes renderer inputs or renderer hash construction.

Provider artwork candidates remain the existing bounded internal
`V2ArtworkCandidate` shape:

```json
{
  "kind":"poster",
  "locator":{"provider":"tmdb","url":"https://image.tmdb.org/t/p/w500/abc.jpg"},
  "locale":"neutral",
  "vote_average":8.1,
  "vote_count":1234
}
```

`kind` is `poster`, `backdrop`, or `logo`; `locale` is normalized;
vote values are finite and bounded. Provider adapters return at most 128
validated candidates, while selection passes at most three options per missing
role/policy to materialization. Candidates carry no digest or filesystem path:
the source client obtains bytes, normalizes them, and gets the owner-issued
digest/reference through register.

## OCR verification lookup/register

OCR memoization is source-agent-owned. The Oracle OCR helper calls these
methods on its source-store adapter and never opens a database:

```python
memo = store.lookup_verification(key)
if memo is None:
    result = scan_normalized_derivative(path, ...)
memo = store.register_verification(key, result=result, verified_at=now)
```

The verification-cache caller is OCR agent `c70fc2fb-2145-4b16-b0b4-
acbda4f53988`. It must use `build_ocr_memo_key(...).payload()` as the exact
key, treat a stored `unknown` as an authoritative hit (no second scan), and
never call the registry directly or open Oracle SQLite.

The synchronous adapter method names are `lookup_verification` and
`register_verification`; async callers may run them in their existing worker
thread. A key has exactly:

```json
{
  "kind":"poster",
  "source_sha256":"<normalized-payload-sha256>",
  "title_context_sha256":"<canonical-title-context-sha256>",
  "detection_rules":"ppocr.textless.v1",
  "model":"ppocrv5-mobile",
  "runtime":"rapidocr_onnxruntime",
  "runtime_version":"<bounded-version-token>",
  "architecture":"x86_64"
}
```

The memo signature is lower-case SHA-256 of canonical UTF-8 JSON of the key
(sorted keys, compact separators, no ASCII escaping). The source digest and
kind are in the signature, so a result cannot cross normalized images or
roles. Title-context digest is the OCR helper's digest of its normalized,
de-duplicated ordered title tuple. Rules/model/runtime/version/architecture
changes deliberately miss the memo.

`runtime_version` is an opaque lowercase token matching
`[a-z0-9][a-z0-9._:-]{0,79}`. An OCR implementation may carry model/rule
digest markers such as `m<hex>-r<hex>` there, but the markers must remain
inside the 80-character bound and may not be silently truncated away. The v1
wire shape cannot carry two full 64-hex digests plus arbitrary runtime text;
if full-digest identity is required, that is a contract revision rather than
an adapter-side field addition.

### POST /v2/source-art/verification-lookup

Request:

```json
{
  "schema":"postersplus.source_art.verification",
  "version":1,
  "key":{"...":"the exact key above"},
  "signature":"<sha256-of-canonical-key>"
}
```

Miss response is `{"schema":"postersplus.source_art.verification","version":1,"found":false}`.
A hit is:

```json
{
  "schema":"postersplus.source_art.verification",
  "version":1,
  "found":true,
  "memo":{
    "signature":"<sha256-of-canonical-key>",
    "key":{"...":"the exact key above"},
    "result":"textless",
    "verified_at":"2026-09-14T12:00:00Z",
    "source_sha256":"<normalized-payload-sha256>"
  }
}
```

`result` is exactly one of `textless`, `text`, or `unknown`. Only
`textless` may produce `textless_verified=true` in a
`SourceArtReference`; `text` and `unknown` are negative/indeterminate
cached results and do not publish verified textless art.

### POST /v2/source-art/verification-register

Request:

```json
{
  "schema":"postersplus.source_art.verification",
  "version":1,
  "key":{"...":"the exact key above"},
  "signature":"<sha256-of-canonical-key>",
  "result":"textless",
  "verified_at":"2026-09-14T12:00:00Z"
}
```

The owner recomputes and compares the signature, requires
`key.source_sha256` to be an existing intact derivative of `key.kind`, and
stores the memo with an atomic insert-or-verify operation. A conflicting result
for the same signature is `409 verification_conflict`; an identical retry
converges on the existing memo. The owner performs no OCR and accepts no
caller-supplied path or image bytes.

The OCR agent maps a `textless` memo to the existing reference evidence:
`textless_verified=true`, `verification_recipe=detection_rules`,
`verified_at=memo.verified_at`, and
`verification_source_digest=key.source_sha256`. For `text`/`unknown`, it
rejects that candidate and tries the next bounded candidate. The memo is
reusable across presets because it is based on normalized source digest plus
title/detection/model/runtime identity, not a preset or renderer hash.

## Failure and atomicity rules

The client treats capacity exhaustion, expired reservations, owner/mount
failures, and corrupt/missing published files as retryable source-art
unavailability. It never downloads from a locator in the renderer path. A
failed download/normalization/register releases the reservation and removes
only its own strict staging file.

The owner transaction boundary is ledger row plus content-addressed destination
visibility plus reservation consumption. A crash before commit leaves no usable
ledger row; a crash after commit leaves a verified row and incoming is safe for
bounded cleanup. Lookup never returns a row whose file fails digest/metadata
checks. No source-art operation may open Oracle's local source ledger or
silently fall back to a local SQLite store.

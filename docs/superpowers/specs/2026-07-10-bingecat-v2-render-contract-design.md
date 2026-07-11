# BingeCat v2 Render Contract Design

Date: 2026-07-10
Base: `origin/dev` at `2b44e8fd70f635e9f7360e2348915d334a909afc`
Feature branch: `codex/bingecat-v2-integration`

## Goal

Add an optional, open-source BingeCat integration API that turns PostersPlus into a deterministic enrichment/render service while keeping the existing standalone configurator and `/poster` route compatible.

## Boundary

PostersPlus remains the source of truth for visual configuration, preset definitions, provider adapters, and composition. It does not import proprietary BingeCat code, access BingeCat ORM models, or require BingeCat to run.

The integration is enabled only when a dedicated service secret is configured. Existing public behavior, legacy access-key checks, companion scripts, and standalone provider/cache flow remain available.

## New modules

### `render_spec.py`

- Defines the canonical, versioned visual configuration schema.
- Accepts only known render-affecting fields.
- Normalizes aliases, booleans, numbers, weights, colors, and sash order.
- Clamps every numeric input before it reaches NumPy/Pillow.
- Produces stable canonical JSON and a SHA-256 config hash.
- Compiles `DataRequirements` flags for ratings, awards, trending, lifecycle/release, credits/studios, logo, OCR, fallback art, and quality.

The legacy query parser adapts into this schema for cache identity but retains backwards-compatible response semantics. Unknown legacy query fields remain ignored and no longer fragment the composite cache.

### `preset_registry.py`

Owns the namespaced `BINGECAT_PRESET_REGISTRY` with immutable IDs and versions; it does not replace or rename the ten legacy standalone configurator presets:

- `clean-notch@1`: a deliberate BingeCat variant of legacy Clean Notch, changed from legacy mode `2` to rating display mode `5` (Dual Clean);
- `prestige@1`: existing Prestige rating-bar visual;
- `minimalist@1`: the supplied BingeCat replacement config after removing credentials and disabling quality badges.

The registry validates itself on import and exposes safe metadata through `GET /v2/presets`. Any render-affecting change requires a version bump. Referenced versions remain renderable for the full BingeCat snapshot/artifact retention horizon; an unsupported version returns `409 unsupported_preset_version` and never falls forward.

### `integration_contract.py`

Typed DTOs for:

- canonical media identity;
- provider ratings and votes;
- sash/award/release/lifecycle facts;
- localized artwork locators;
- bounded `titles_by_locale` with an explicit English title;
- enrichment request/result;
- immutable render input bundle and result metadata.

DTO schema identifier is `bingecat_postersplus_v2`, version `1`. Unknown top-level fields are rejected, bounded strings/lists are enforced, and JSON bodies are capped at 256 KiB.

### `service_auth.py`

Verifies directional timestamped HMAC-SHA256 requests. BingeCat → PostersPlus uses `POSTERSPLUS_BINGECAT_REQUEST_SECRET` with caller/audience `bingecat/postersplus`; callbacks use `BINGECAT_POSTERSPLUS_CALLBACK_SECRET` with `postersplus/bingecat`. Every request signs canonical UTF-8 lines containing `v1`, caller, audience, uppercase method, decoded absolute path, UUIDv4 request ID, integer Unix timestamp, and lowercase body SHA-256. Signed endpoints accept no query string; GET signs the empty-body digest. Clock skew is at most 60 seconds, comparisons are constant-time, and atomically recorded request IDs reject replay for at least 120 seconds. Both repositories share golden vectors.

The legacy `ACCESS_KEY` is not accepted as v2 service authentication.

## Internal endpoints

### `GET /v2/presets`

Returns only preset IDs, versions, labels, config hashes, supported locales, contract schema, and renderer revision. No credentials or provider keys are serialized. It requires BingeCat-direction service authentication.

### `POST /v2/enrich`

Called only by a BingeCat background worker. It:

1. validates canonical identity plus the preset/custom config references the snapshot must support and derives `DataRequirements` locally;
2. merges trusted known facts from BingeCat;
3. calls only providers needed for missing/expired requested fields;
4. resolves every time-derived lifecycle/release boolean at an explicit `evaluated_at` and normalizes values into the contract DTO;
5. returns observation/expiry timestamps and provider result statuses;
6. omits/bounds raw provider payloads.

In integration-stateless mode, ratings and metadata results are not written to PostersPlus SQLite. Provider backoff/cooldown state and normalized source-art files may remain local because they protect the provider and renderer rather than becoming an authoritative metadata cache.

### `POST /v2/render`

Consumes a complete immutable snapshot plus preset reference or canonical custom config. It recompiles requirements and rejects a snapshot that does not satisfy them. It must not call ratings, awards, trending, release, or quality providers and must not read the wall clock for any visual decision.

Artwork inputs are content addressed: opaque source-art ID, immutable derivative SHA-256, byte size, MIME, normalization recipe version, and an allowlisted provider locator used only to reconstruct a missing derivative. Fetches reject private/link-local destinations, DNS rebinding, unapproved hosts/schemes, redirect overflow, oversized bodies/decoded pixels, wrong MIME, and digest mismatch. A changed source requires a new enrichment snapshot; it is never accepted under an existing snapshot hash.

The endpoint returns WebP bytes with:

- content SHA-256 ETag;
- `X-PostersPlus-Content-SHA256`;
- `X-PostersPlus-Renderer-Revision`;
- `X-PostersPlus-Config-SHA256`;
- `X-PostersPlus-Snapshot-SHA256`.

No public immutable cache header is set on this private response; BingeCat owns final artifact installation and public caching.

### `GET /v2/cache/usage`

Returns authenticated `{schema, version, generated_at, total_bytes, pools}` data. Each pool exposes integer bytes and hard/high/target limits for source derivatives, legacy composites, SQLite, SQLite WAL, and temp files.

### `POST /v2/cache/prune`

Requests bounded eviction to configured targets and returns bytes before/after plus eviction counts. It accepts no caller-provided path and cannot exceed the operator-configured policy.

Every `/v2/*` endpoint requires valid BingeCat-direction service authentication. When its secret is absent, the integration endpoints return 404 while legacy routes remain unchanged.

## Configurator handoff

When BingeCat handoff environment settings are configured, a top-level form `POST` to `/bingecat/configurator/session` accepts an opaque one-time token. PostersPlus consumes it through signed `POST /api/internal/posterplus/v2/handoffs/consume`, receives safe current config state, a one-time save grant, and a BingeCat-issued allowlisted return target, then redirects to a clean URL.

Saving:

1. canonicalizes and validates settings locally;
2. computes the config hash;
3. requires a session-bound CSRF token and sends the canonical DTO plus save grant to signed `POST /api/internal/posterplus/v2/configs`;
4. shows the resulting revision and redirects to the exact BingeCat-issued return target.

The random server-side session cookie is `Secure`, `HttpOnly`, `SameSite=Lax`, scoped to `/bingecat/configurator`, and bounded to the handoff lifetime. Configurator responses use `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. The handoff/config is never stored in localStorage, embedded in a poster URL, or authorized by the legacy access key.

## Preset and custom-config policy

The renderer does not decide BingeCat account entitlements. It distinguishes fixed preset references from custom canonical configs and relies on the authenticated BingeCat caller. Direct public v2 custom rendering is disabled.

Quality mode is forced to `0` in every fixed BingeCat preset. Initial custom configs accept badge modes `0` (hidden) and `3` (age-only); modes `1`, `2`, `4`, and `5` are rejected because they require out-of-scope quality data. `DataRequirements.quality` is rejected. The standalone configurator and legacy `/poster` route keep their existing quality controls.

## Localization

Supported integration locales are `en`, `pt`, `nl`, `de`, and `es`. New complete `nl`, `de`, and `es` translation files are added alongside existing English and Portuguese. Normalization lowercases, replaces `_` with `-`, validates an alphabetic base subtag, and maps shared vectors `pt-BR/pt_BR → pt`, `NL-nl → nl`, `de_DE → de`, `es-419 → es`, `en-US → en`, and unknown/empty/malformed values → `en`.

Text and artwork use separate deterministic chains. Labels and `titles_by_locale` use requested language then English per key. Base art prefers verified neutral/textless posters, then requested-language textual art, then English textual art, then deterministic backdrop/fallback. Logos use requested language, supported native/original choices explicitly allowed by the canonical config, neutral, then English; unsupported-language original assets never outrank English. Backdrops are language-neutral only after text/metadata validation.

## Legacy hardening and cost reductions

These changes apply without removing legacy behavior:

1. Every gradient height/opacity/offset is bounded before array allocation.
2. Composite keys use canonical identity + canonical recognized render spec + reproducibility renderer revision + relevant server policy revision; ignored unknown parameters cannot create cache variants while encoder/assets/OCR/rating policy still invalidate correctly.
3. L1 composite entries store expiry and are invalidated when L2 is pruned or stale.
4. ETags are hashes of response bytes, not logical cache keys.
5. Provider work is gated by locally compiled `DataRequirements`; logo/OCR/rating/trending/award/release/quality work is skipped when the render cannot display it.
6. Canonical identity is accepted from an authenticated integration call so warm renders do not perform TMDB identity resolution.
7. Language-independent TMDB facts are separated from localized artwork lookups where practical.
8. Background schedulers use a single-leader file lock so `WORKERS > 1` does not duplicate daily/provider work.
9. Trending refresh no longer replays every historical custom variant; it targets affected current/pinned variants.

## Source-art lifecycle and cache budget

Source art is stored only as normalized derivatives:

- poster: 500×750 JPEG/WebP master;
- backdrop: normalized/cropped derivative needed for fallback;
- logo: alpha-cropped PNG/WebP derivative.

Full provider downloads are temporary and atomically removed after normalization. A source-art ledger records provider locator, content hash, bytes, created/last-used time, and pin/reconstructability state.

New configuration:

- `SOURCE_CACHE_MAX_BYTES`;
- `SOURCE_CACHE_HIGH_WATERMARK_BYTES`;
- `SOURCE_CACHE_TARGET_BYTES`;
- `SOURCE_CACHE_RAW_MAX_AGE_SECONDS`;
- `POSTERSPLUS_INTEGRATION_STATELESS_METADATA`.

The default standalone behavior remains conservative. The BingeCat allocation is normative:

- normalized source derivatives hard 15 GB/high 13.5 GB/target 12 GB;
- legacy composites plus SQLite/WAL hard 5 GB/high 4.5 GB/target 4 GB;
- BingeCat owns a separate 70 GB final-artifact/poster-table pool and 10 GB staging/headroom reserve.

`GET /v2/cache/usage` returns authenticated integer totals/limits for source derivatives, legacy composites, SQLite, SQLite WAL, and temp. `POST /v2/cache/prune` performs bounded pruning to configured targets. Each local hard cap is enforced independently; BingeCat coordinates aggregate cleanup at 90 GB to 80 GB.

Eviction removes expired temp files first, then cold reconstructable derivatives. It never deletes a non-reconstructable sole source without an explicit retention policy.

## Caching correctness

The legacy composite SQLite cache remains for standalone `/poster`. Its entry-count cap continues to work, and an optional byte cap is added. L1 entries contain `(bytes, content_type, expires_at, content_hash)`.

Integration `/v2/render` is deterministic for the tuple:

`renderer_revision + canonical_config_hash + snapshot_hash + locale + source_content_hashes + output_format`

`renderer_revision` is a reproducibility digest over renderer code, locked dependency/container build, Pillow/libwebp/Cairo versions, encoder settings, fonts, translations, fallback assets, and crop/normalization recipes. Identical tuples under the same revision produce identical bytes. `evaluated_at` and every time-derived visual fact are frozen in the snapshot; observation timestamps not affecting visuals are excluded from the visual hash.

## Error behavior

- Invalid contract/config input: 422 with bounded error details.
- Missing/invalid service auth: 401/403 without signature diagnostics.
- Missing integration secrets: all `/v2/*` and BingeCat configurator endpoints return 404; legacy endpoints remain available.
- Provider rate limit during enrich: typed partial result plus retry timestamp; never an unbounded retry loop.
- Missing optional rating/sash fact: successful partial bundle with explicit missing status.
- Render input missing required art: deterministic fallback art when permitted, otherwise typed 409; no provider metadata lookup.
- Rendering/resource limit: 422/503 and no cache write.

Logs redact all credentials, signatures, handoff tokens, and canonical custom payloads.

## Tests

Required automated coverage:

- preset registry schema, exact versions, canonical hashes, and no secrets;
- supplied minimalist equivalence after key removal/quality disable;
- clean-notch Dual Clean and Prestige rating-bar modes;
- canonical config equivalence and ignored legacy parameter de-duplication;
- clamp/resource-bound regression for custom gradients;
- HMAC valid, invalid, stale, and body-tamper cases;
- HMAC nonce replay, direction/audience, empty-body, and shared golden-vector cases;
- enrich requirement gating and integration-stateless persistence behavior;
- render contract proving zero non-art provider calls;
- deterministic WebP/content hash for identical inputs;
- frozen-clock lifecycle facts and source-digest mismatch/SSRF/image-limit rejection;
- L1 TTL/prune coherence and content-derived ETags;
- complete `en/pt/nl/de/es` key parity and English fallback;
- single-leader scheduler behavior;
- source ledger byte accounting/eviction and authenticated usage stats;
- configurator handoff expiry/replay/origin validation and absence from localStorage/URLs;
- all existing 69 tests remain green.

## Compatibility and rollout

No existing environment variable is removed. `/poster`, `/stats`, `/health`, static assets, the normal configurator, Plex/Jellyfin scripts, and legacy cache rows remain supported. BingeCat workers verify the returned content digest, install once at a content-hash path, never overwrite it, and expose one-year immutable responses; any current-poster alias is separate and revalidated.

Recommended rollout:

1. deploy the core with v2 endpoints configured but unreachable publicly;
2. validate HMAC contract and deterministic sample renders from BingeCat dev;
3. rotate the legacy public access key;
4. enable BingeCat background enrichment before user-facing v2 selection;
5. observe provider/cache metrics before enabling global prewarm.

Production deployment is deliberately outside this branch implementation.

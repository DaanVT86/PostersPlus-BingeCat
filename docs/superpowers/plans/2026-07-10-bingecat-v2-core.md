# BingeCat v2 Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, authenticated BingeCat v2 enrichment/render core while preserving every legacy PostersPlus route and cache behavior.

**Architecture:** Keep the existing FastAPI/Pillow renderer and provider adapters as the composition engine, adding isolated contract, auth, preset, source-art, and cache-policy modules. `/v2/enrich` performs bounded provider enrichment; `/v2/render` accepts only an immutable snapshot and normalized derivatives, so visual output is pure and reproducible. Legacy `/poster` adapts recognized query settings into the same canonical identity without changing its response contract.

**Tech Stack:** Python 3.11 (the service Docker runtime; code remains 3.12-compatible), FastAPI, Pydantic (strict DTO validation), Pillow/NumPy, httpx, SQLite/WAL, pytest, and injected clock functions/pytest monkeypatching.

## Global Constraints

- DTO schema identifier is `bingecat_postersplus_v2`, version `1`; JSON request bodies are capped at 256 KiB and reject unknown top-level fields.
- Every `/v2/*` request uses directional timestamped HMAC-SHA256; legacy `ACCESS_KEY` is never accepted as v2 authentication.
- Supported integration locales are exactly `en`, `pt`, `nl`, `de`, and `es`; malformed or unknown values normalize to `en`.
- Fixed BingeCat presets force quality mode `0`; custom configs may use only badge modes `0` and `3`.
- `/v2/render` performs no ratings, awards, trending, release, lifecycle, or quality provider calls and reads no wall clock for visual decisions.
- Determinism tuple is `renderer_revision + canonical_config_hash + snapshot_hash + locale + source_content_hashes + output_format`.
- Source derivatives are normalized poster `500x750`, normalized fallback backdrop, and alpha-cropped logo; raw downloads are temporary and atomically removed.
- Missing integration secrets make all `/v2/*` and handoff endpoints return `404`; legacy endpoints remain available.

---

### Task 1: Canonical spec, BingeCat presets, and locales

**Files:**
- Create: `render_spec.py`, `preset_registry.py`
- Modify: `i18n.py`, `languages/en.json`, `languages/pt.json`
- Create: `languages/nl.json`, `languages/de.json`, `languages/es.json`
- Test: `tests/test_v2_render_spec.py`, `tests/test_v2_presets_i18n.py`

**Interfaces:**
- `render_spec.canonicalize_config(raw: Mapping[str, Any]) -> CanonicalRenderSpec` and `.canonical_json() -> str`, `.sha256() -> str`.
- `render_spec.compile_requirements(spec: CanonicalRenderSpec) -> DataRequirements`.
- `preset_registry.get_preset(ref: str) -> Preset` and `list_public_presets() -> list[PresetMetadata]`.
- `i18n.normalize_locale(value: str | None) -> str` and `i18n.resolve_locale_chain(requested: str, available: Iterable[str]) -> list[str]`.

- [ ] **Step 1: Write failing tests** for alias/boolean/number/color/sash normalization, numeric clamping, ignored-field cache equivalence, exact `clean-notch@1`/`prestige@1`/`minimalist@1` hashes, no secret fields, and complete five-locale key parity.
- [ ] **Step 2: Run tests to verify failure** — `pytest tests/test_v2_render_spec.py tests/test_v2_presets_i18n.py -q` (expected import/registry failures).
- [ ] **Step 3: Implement canonical schema and registry** with strict known fields, `DataRequirements` flags, versioned immutable IDs, quality restrictions, and import-time self-validation. Add locale normalization mappings (`pt-BR`, `NL-nl`, `de_DE`, `es-419`, `en-US`).

```python
spec = canonicalize_config({"rating_display_mode": 5, "top_gradient_height": 99999})
assert spec.rating_display_mode == 5
assert spec.top_gradient_height == 1.0
assert spec.sha256() == hashlib.sha256(spec.canonical_json().encode()).hexdigest()
```

- [ ] **Step 4: Add translations** by copying the existing key set and supply deterministic English fallback per key.
- [ ] **Step 5: Run tests to verify pass** — `pytest tests/test_v2_render_spec.py tests/test_v2_presets_i18n.py -q` (expected PASS).

- [ ] **Step 6: Commit** — `git add render_spec.py preset_registry.py i18n.py languages tests/test_v2_render_spec.py tests/test_v2_presets_i18n.py && git commit -m "feat: add BingeCat v2 render specs and presets"`.

### Task 2: Directional HMAC and strict DTOs

**Files:**
- Create: `integration_contract.py`, `service_auth.py`
- Modify: `config.py`
- Create: `tests/fixtures/postersplus_v2_auth_vectors.json`
- Test: `tests/test_v2_contract_auth.py`, `tests/test_v2_auth_vectors.py`

**Interfaces:**
- `integration_contract` exports strict DTOs `MediaIdentity`, `ProviderRating`, `EnrichmentRequest`, `EnrichmentResult`, `RenderInputBundle`, `RenderResultMetadata`.
- `service_auth.verify_request(request: Request, secret: bytes, caller: str, audience: str, nonce_store: NonceStore) -> AuthContext`.
- `service_auth.sign_request(method: str, path: str, body: bytes, request_id: UUID, timestamp: int, secret: bytes, caller: str, audience: str) -> str`.

- [ ] **Step 1: Write failing tests** for valid/invalid/stale/tampered signatures, empty-body GET, query rejection, UUIDv4 nonce replay, direction/audience mismatch, golden vectors, strict unknown fields, bounded strings/lists, and 256 KiB body limit.
- [ ] **Step 2: Run** `pytest tests/test_v2_contract_auth.py tests/test_v2_auth_vectors.py -q` and observe failures.
- [ ] **Step 3: Implement** canonical UTF-8 signing lines (`v1`, caller, audience, uppercase method, decoded absolute path, request UUID, integer timestamp, lowercase body SHA-256), constant-time comparison, ±60 second skew, and atomic 120-second nonce recording. Add `POSTERSPLUS_BINGECAT_REQUEST_SECRET` and `BINGECAT_POSTERSPLUS_CALLBACK_SECRET` config reads.
- [ ] **Step 4: Run** the same pytest command and require PASS.

- [ ] **Step 5: Commit** — `git add integration_contract.py service_auth.py config.py tests/fixtures/postersplus_v2_auth_vectors.json tests/test_v2_contract_auth.py tests/test_v2_auth_vectors.py && git commit -m "feat: authenticate PostersPlus v2 contracts"`.

### Task 3: Deterministic source art and enrichment

**Files:**
- Create: `source_art.py`, `v2_enrich.py`
- Modify: `ratings.py`, `tmdb.py`, `tvdb.py`, `bingecat_resolver.py`, `main.py`
- Test: `tests/test_v2_enrich_art.py`

**Interfaces:**
- `source_art.normalize_and_store(kind: Literal["poster", "backdrop", "logo"], raw: BinaryIO, recipe_version: int) -> SourceDerivative`.
- `source_art.fetch_derivative(locator: AllowlistedLocator) -> SourceDerivative` (SSRF-safe and digest-checked).
- `v2_enrich.enrich(request: EnrichmentRequest, now: datetime) -> EnrichmentResult`.
- FastAPI handler `POST /v2/enrich` in `main.py` (authenticated, bounded partial result with retry/expiry metadata).

- [ ] **Step 1: Write failing tests** for requirement-gated provider calls, trusted-fact and known-source-art reuse, preserved rating scale/vote counts, frozen lifecycle booleans, partial/missing statuses, derivative dimensions/MIME/hash, temporary raw deletion, source-digest mismatch, private/link-local DNS, redirect/size/pixel/MIME limits, and stateless metadata persistence.
- [ ] **Step 2: Run** `pytest tests/test_v2_enrich_art.py -q` (expected failures).
- [ ] **Step 3: Implement** normalized derivative ledger (locator, hash, bytes, timestamps, pin/reconstructability), allowlisted fetch validation, and enrichment that calls only missing/expired requirements and omits raw payloads. Refactor `ratings.py` behind a detail adapter that retains provider score/scale/vote metadata while preserving the legacy dictionary projection. Reuse bounded `known_source_art` until its explicit expiry instead of resolving artwork on every unrelated fact refresh. Respect `POSTERSPLUS_INTEGRATION_STATELESS_METADATA`.
- [ ] **Step 4: Run** the targeted test and require PASS.

- [ ] **Step 5: Commit** — `git add source_art.py v2_enrich.py ratings.py tmdb.py tvdb.py bingecat_resolver.py main.py tests/test_v2_enrich_art.py && git commit -m "feat: enrich deterministic PostersPlus inputs"`.

### Task 4: Pure render endpoint and deterministic WebP

**Files:**
- Create: `v2_render.py`
- Modify: `main.py`
- Test: `tests/test_v2_render_endpoint.py`

**Interfaces:**
- `v2_render.render(bundle: RenderInputBundle) -> tuple[bytes, RenderResultMetadata]`.
- FastAPI handlers `POST /v2/render` and `GET /v2/presets` in `main.py`.

- [ ] **Step 1: Write failing tests** proving zero non-art provider calls, requirement mismatch rejection, deterministic bytes/hash for identical tuples, fallback/409 behavior for missing art, and required response headers (`ETag`, `X-PostersPlus-*`).
- [ ] **Step 2: Run** `pytest tests/test_v2_render_endpoint.py -q` (expected failures).
- [ ] **Step 3: Implement** authenticated handlers, preset/custom policy, immutable snapshot verification, renderer-revision digest, pure Pillow composition, WebP output, content-derived ETag, and no public cache header.
- [ ] **Step 4: Run** targeted tests and require PASS.

- [ ] **Step 5: Commit** — `git add v2_render.py main.py tests/test_v2_render_endpoint.py && git commit -m "feat: render immutable BingeCat poster bundles"`.

### Task 5: Cache correctness, budgets, and scheduler leadership

**Files:**
- Create: `cache_policy.py`
- Modify: `cache.py`, `config.py`, `main.py`
- Test: `tests/test_v2_cache_budget.py`

**Interfaces:**
- `cache_policy.get_usage() -> CacheUsage` and `prune_to_targets() -> PruneResult`.
- Authenticated handlers `GET /v2/cache/usage`, `POST /v2/cache/prune`.

- [ ] **Step 1: Write failing tests** for L1 `(bytes, content_type, expires_at, content_hash)` TTL/prune coherence, content ETags, source/legacy/SQLite/WAL/temp independent hard/high/target caps, ledger accounting/eviction ordering, bounded prune, and single-leader file lock with `WORKERS > 1`.
- [ ] **Step 2: Run** `pytest tests/test_v2_cache_budget.py -q` (expected failures).
- [ ] **Step 3: Implement** environment limits (`SOURCE_CACHE_*`, normative 15/13.5/12 GB and 5/4.5/4 GB pools), stale L1 invalidation, byte cap, expired-temp then cold-reconstructable eviction, and leader lock; never delete a sole non-reconstructable source.
- [ ] **Step 4: Run** targeted tests and require PASS.

- [ ] **Step 5: Commit** — `git add cache_policy.py cache.py config.py main.py tests/test_v2_cache_budget.py && git commit -m "feat: enforce PostersPlus cache budgets"`.

### Task 6: Legacy hardening and compatibility adapter

**Files:**
- Modify: `main.py`, `cache.py`, `config.py`
- Test: `tests/test_v2_legacy_hardening.py`, existing `tests/test_cache_hardening.py`

**Interfaces:**
- `render_spec.legacy_query_to_spec(query: Mapping[str, str]) -> CanonicalRenderSpec` (unknown legacy keys ignored).
- Existing `/poster`, `/stats`, `/health`, configurator, Plex, and Jellyfin interfaces remain unchanged.

- [ ] **Step 1: Write failing regression tests** for bounded gradient allocations, canonical composite keys ignoring unknown parameters, policy/asset/OCR/rating invalidation, requirement-gated work, authenticated canonical identity bypassing TMDB resolution, language-independent fact reuse, and affected-only trending refresh.
- [ ] **Step 2: Run** `pytest tests/test_v2_legacy_hardening.py tests -q` and capture baseline failures.
- [ ] **Step 3: Implement** clamps before NumPy/Pillow allocation, adapter-based cache identity, provider gating, single-leader background jobs, and preserve all legacy response semantics.
- [ ] **Step 4: Run** `pytest tests -q` and require all existing 69 tests plus new tests PASS.

- [ ] **Step 5: Commit** — `git add main.py cache.py config.py render_spec.py tests/test_v2_legacy_hardening.py tests/test_cache_hardening.py && git commit -m "fix: harden legacy poster rendering and cache identity"`.

### Task 7: Configurator handoff and signed save

**Files:**
- Create: `bingecat_handoff.py`
- Modify: `main.py`, `configurator.html`
- Test: `tests/test_v2_handoff.py`

**Interfaces:**
- `POST /bingecat/configurator/session` accepts one-time opaque token and redirects to clean configurator URL.
- Internal clients call signed `POST /api/internal/posterplus/v2/handoffs/consume` and `POST /api/internal/posterplus/v2/configs`.

- [ ] **Step 1: Write failing tests** for expiry/replay/origin validation, exact return-target redirect, CSRF-bound save grant, secure HttpOnly SameSite=Lax cookie scoped to `/bingecat/configurator`, no-store/no-referrer headers, and absence of token/config in localStorage, URLs, or legacy access-key auth.
- [ ] **Step 2: Run** `pytest tests/test_v2_handoff.py -q` (expected failures).
- [ ] **Step 3: Implement** signed consume/save calls, local canonicalization/hash, revision display, exact allowlisted return redirect, bounded server session, and missing-secret 404 behavior.
- [ ] **Step 4: Run** targeted tests and require PASS.

- [ ] **Step 5: Commit** — `git add bingecat_handoff.py main.py configurator.html tests/test_v2_handoff.py && git commit -m "feat: add BingeCat configurator handoff"`.

### Task 8: Final validation and rollout handoff

**Files:**
- Modify: `README.md`, `CHANGELOG.md`
- Test: `tests/test_v2_full_contract.py`

**Interfaces:**
- `pytest` suite is the release gate; deployment remains external to this branch.

- [ ] **Step 1: Add contract smoke tests** covering endpoint auth/404 gating, preset metadata redaction, deterministic sample render, cache usage/prune schemas, locale parity, and all required error statuses (422/401/403/404/409/503).
- [ ] **Step 2: Run complete validation:** `python3 -m pytest tests -q` and `python3 -m compileall .`; expected all tests pass and compileall exits 0.
- [ ] **Step 3: Verify compatibility checklist** for `/poster`, `/stats`, `/health`, static assets, normal configurator, Plex/Jellyfin scripts, and legacy cache rows; document BingeCat worker content-hash installation and one-year public artifact caching in `README.md`/`CHANGELOG.md`.
- [ ] **Step 4: Run final commands** `git diff --check` and `pytest tests -q`; retain generated reports/logs free of credentials, signatures, handoff tokens, and canonical custom payloads.

- [ ] **Step 5: Commit** — `git add README.md CHANGELOG.md tests/test_v2_full_contract.py && git commit -m "test: validate BingeCat v2 render contract"`.

## Self-review checklist

- [x] Every spec area is mapped: canonical schema/presets/locales (Task 1), auth/DTOs (Task 2), source art/enrich (Task 3), pure render (Task 4), cache budget/correctness (Task 5), legacy hardening (Task 6), configurator handoff (Task 7), and complete validation/rollout (Task 8).
- [x] No placeholder language or unspecified implementation steps is used.
- [x] Interfaces and names are consistent across tasks (`CanonicalRenderSpec`, `DataRequirements`, `RenderInputBundle`, `SourceDerivative`, and cache endpoint handlers).

# PostersPlus BingeCat Agent Instructions

This repository is the BingeCat-owned PostersPlus service repo:
`DaanVT86/PostersPlus-BingeCat`.

It is currently a close mirror of `UmbraProjects/PostersPlus` on the `dev`
branch, with BingeCat-specific runtime configuration kept outside the repo.
Keep upstream compatibility in mind when changing code.

## Current Purpose

- Run PostersPlus as a standalone service for BingeCat.
- Keep service changes independent from the main BingeCat production app deploy.
- Gradually integrate PostersPlus into BingeCat over time through explicit API
  contracts and small, reviewable changes.

When work touches only this service, do not deploy or modify the main BingeCat
production app repo.

## Repo And Worktrees

- Local service worktrees: `/root/bingecatwork/postersplus-*`
- Private production Core checkout on `netcupvps`: `/opt/postersplus/repo`
- Origin remote: `https://github.com/DaanVT86/PostersPlus-BingeCat.git`
- Upstream remote: `https://github.com/UmbraProjects/PostersPlus.git`
- Default branch: `dev`

Use `origin` for BingeCat work. Use `upstream` only to fetch or compare with the
source project.

## Runtime Topology

PosterPlus has two deliberately separate production roles:

- The existing public legacy instance continues to own
  `https://posterplus.bingecat.com`. Never stop, replace, reuse its tunnel
  token, or attach another connector without an explicit public migration.
- The BingeCat v2 Core runs privately on `netcupvps` from `/opt/postersplus`.
  Its tracked compose file is `compose.production.yaml`, its container is
  `postersplus-v2-core`, its pre-go Docker alias is `postersplus-v2-core`, and
  its operator-only bind is `127.0.0.1:18084 -> 8000`.

The private stack has no `cloudflared` service. BingeCat owns the public
content-addressed WebP URLs; Core only serves signed enrich/render requests on
the external Docker network `aicat-app-internal`.

Runtime env and cache data remain outside the checkout under
`/opt/postersplus/env` and `/opt/postersplus/cache`. Do not commit secrets,
`.env` files, cache DBs, generated posters, tunnel tokens, or API keys.

## Runtime Config

Current private Core config is stored on Netcup, not in git:

- `TMDB_API_KEY`: app-wide key configured in `/opt/postersplus/env/postersplus.env`
- `MDBLIST_API_KEY`: copied from the BingeCat app-wide MDBList setting
- `COMPOSITE_MAX_ENTRIES=10000`
- `ACCESS_KEY`: existing PostersPlus access key
- `POSTERSPLUS_BINGECAT_REQUEST_SECRET`: exact shared private request secret

The private Core does not require or consume a Cloudflare tunnel token.

AIOStreams quality badge settings are currently blank unless the user provides
`AIOSTREAMS_URL` and `AIOSTREAMS_AUTH`.

## Deploying This Service

Deploy only the private Core with an exact pushed ref:

```bash
ssh netcupvps 'POSTERSPLUS_DEPLOY=1 /opt/postersplus/repo/deploy/netcup/deploy-production.sh origin/dev'
```

The wrapper refuses BingeCat's currently configured live alias during pre-go,
builds an SHA-tagged image, starts only the private Core service, and verifies
Docker plus localhost health. It does not deploy/recreate BingeCat and cannot
modify the legacy public PosterPlus tunnel.

Useful checks:

```bash
ssh netcupvps 'curl -fsS http://127.0.0.1:18084/health'
curl -fsS https://posterplus.bingecat.com/health
ssh netcupvps 'docker inspect postersplus-v2-core --format "{{.State.Health.Status}}"'
```

## Development Guidance

- Prefer small service-scoped changes.
- Preserve public PostersPlus behavior unless the BingeCat integration requires
  a deliberate contract change.
- Keep BingeCat integration points configurable, preferably through environment
  variables or explicit HTTP/API contracts.
- Avoid coupling this repo to `/root/bingecat` internals until the integration
  plan calls for it.
- If a change requires a matching BingeCat app change, document the contract and
  keep the two deploy paths separate.
- Use `rg` for search and inspect focused files instead of bulk-reading the repo.

## Validation

For code changes, prefer targeted validation before committing:

```bash
python3 -m pytest tests
python3 -m compileall .
```

For deployment-sensitive changes, validate the production Compose model,
rebuild on Netcup, verify private `/health`, and separately confirm that the
legacy public Cloudflare URL remains healthy and unchanged.

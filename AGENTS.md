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

- Local service worktree: `/root/bingecatwork/postersplus-bingecat`
- Production service checkout on OVH: `/opt/postersplus/repo`
- Origin remote: `https://github.com/DaanVT86/PostersPlus-BingeCat.git`
- Upstream remote: `https://github.com/UmbraProjects/PostersPlus.git`
- Default branch: `dev`

Use `origin` for BingeCat work. Use `upstream` only to fetch or compare with the
source project.

## Runtime Topology

PosterPlus runs on `ovhdedi` as its own Docker Compose stack:

- Stack root: `/opt/postersplus`
- Compose file: `/opt/postersplus/docker-compose.yml`
- Runtime env: `/opt/postersplus/env/postersplus.env`
- Cache volume path: `/opt/postersplus/cache`
- App container: `postersplus-app`
- Tunnel container: `postersplus-cloudflared`
- Public URL: `https://posterplus.bingecat.com`
- Local app bind on OVH: `127.0.0.1:18083 -> 8000`

The compose file, runtime env, and cache directory are intentionally outside
the git checkout. Do not commit secrets, `.env` files, cache DBs, generated
posters, tunnel tokens, or API keys.

The old Oracle VPS PostersPlus containers are stopped and should remain stopped
unless the user explicitly asks for rollback work.

## Runtime Config

Current runtime config is stored on OVH, not in git:

- `TMDB_API_KEY`: app-wide key configured in `/opt/postersplus/env/postersplus.env`
- `MDBLIST_API_KEY`: copied from the BingeCat app-wide MDBList setting
- `COMPOSITE_MAX_ENTRIES=10000`
- `ACCESS_KEY`: existing PostersPlus access key
- `TUNNEL_TOKEN`: existing Cloudflare tunnel token

AIOStreams quality badge settings are currently blank unless the user provides
`AIOSTREAMS_URL` and `AIOSTREAMS_AUTH`.

## Deploying This Service

Deploy only this service with:

```bash
ssh ovhdedi /opt/postersplus/deploy.sh
```

That script pulls `origin/dev`, rebuilds the PostersPlus image, restarts the
PosterPlus compose stack, and checks local health. It does not deploy the main
BingeCat app.

Useful checks:

```bash
ssh ovhdedi 'curl -fsS http://127.0.0.1:18083/health'
curl -fsS https://posterplus.bingecat.com/health
ssh ovhdedi 'sudo docker compose --env-file /opt/postersplus/env/postersplus.env -f /opt/postersplus/docker-compose.yml ps'
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

For deployment-sensitive changes, also rebuild locally or on OVH and verify
`/health` through both the local OVH bind and the public Cloudflare URL.

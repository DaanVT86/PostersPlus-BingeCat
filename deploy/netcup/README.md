# Netcup private production Core

This stack prepares the PosterPlus v2 Core independently from the BingeCat
application rollout. It starts one private application container and does not
create a Cloudflare connector, public DNS record, scheduler, queue consumer, or
BingeCat container.

The pre-go network alias is `postersplus-v2-core`. It deliberately does not
claim the currently configured BingeCat hostname `postersplus-v2-private`, so a
Core-only deployment cannot activate existing queue/drain traffic. The later
BingeCat rollout points `POSTERSPLUS_V2_BASE_URL` at
`http://postersplus-v2-core:8000` as an explicit operation.

The existing `posterplus.bingecat.com` tunnel remains on its legacy host. This
stack has no `cloudflared` service and therefore cannot take over or split that
traffic. Existing public URL patterns remain served by the legacy instance;
BingeCat's v2 enrich/render traffic uses the Docker-private API only.

## Install

1. Keep the checkout at `/opt/postersplus/repo` and secrets below
   `/opt/postersplus/env`, owned by root with mode `0600`.
2. Populate `postersplus.env` and `postersplus-v2-private.env` from the examples.
3. Deploy an exact pushed ref:

   ```bash
   ssh netcupvps 'POSTERSPLUS_DEPLOY=1 /opt/postersplus/repo/deploy/netcup/deploy-production.sh origin/dev'
   ```

The wrapper refuses the live alias, validates required secrets without printing
them, builds an SHA-tagged image, starts only the `app` service, waits for Docker
health, checks localhost port `18084`, and verifies network aliases. It never
runs `docker compose down` and never operates on the BingeCat or legacy
PosterPlus projects.

## Pre-go checks

```bash
ssh netcupvps 'curl -fsS http://127.0.0.1:18084/health'
ssh netcupvps 'docker inspect postersplus-v2-core --format "{{.State.Health.Status}}"'
ssh netcupvps 'docker network inspect aicat-app-internal --format "{{json .Containers}}"'
```

Use a signed `/v2/presets` probe from an isolated operator process to verify the
private contract. Do not enable BingeCat's PosterPlus selection cohort until the
BingeCat PR is merged/deployed and the private base URL is switched explicitly.

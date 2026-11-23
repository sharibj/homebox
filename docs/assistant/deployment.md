# Docker deployment

## Requirements and boundaries

Use Docker Engine 27 or newer, BuildKit, and Docker Compose **2.24.4 or newer**. The acceptance override uses Compose's `!override` tag. The default target is Linux `amd64` or `arm64`, a 16 GiB CPU-only home server, and two DNS names under your control. The delivered image has been built on native `arm64`; an `amd64` build still needs validation on that platform.

The existing Homebox source remains in `backend/` and `frontend/`. The root Compose file remains a development setup. Use only `deploy/docker-compose.yml` for this bundle.

Only Caddy publishes TCP ports 80 and 443. Homebox, Assistant API, MCP, PWA, and Ollama have no published ports. All application networks are internal Docker networks. Only Caddy has normal outbound access, for certificate issuance.

The companion currently supports **native Homebox username/password login**, not an OIDC redirect flow. An OIDC-only account cannot sign in to the companion. This does not remove OIDC support from the full Homebox interface, but that interface needs explicitly configured network egress to use an external identity provider.

## Configure a new installation

Run these commands from the repository root. They use containers for runtime-dependent work:

```sh
sh deploy/scripts/init-config.sh
sh deploy/scripts/init-secrets.sh
```

Bootstrap creates a protected `deploy/.env`, a new `HOMEBOX_INSTANCE_ID` UUID, and three random secret files under `deploy/secrets/`. Existing values are preserved. Edit `deploy/.env`:

- Set **different** `HOMEBOX_HOST` and `ASSISTANT_HOST` names. Point both names at the server.
- Set `PROXY_BIND_IP` to the server's LAN or VPN IPv4 address. Its safe default, `127.0.0.1`, is reachable only from the server.
- Set a unique `STACK_VERSION` for this source build and `SOURCE_REVISION` to its revision.
- Preserve `HOMEBOX_INSTANCE_ID` for the life of this inventory and its restores. It is not a hostname or a secret. It binds assistant state and NFC mappings to the intended Homebox inventory.
- Keep `TLS_ISSUER=internal` for private names, or use `acme` for names and network access that can pass public certificate validation.

With `internal`, install Caddy's local CA certificate on each phone/browser. The root is stored in the `caddy_data` volume at `caddy/pki/authorities/local/root.crt`. Export only the public certificate, never the CA private key:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  cp proxy:/data/caddy/pki/authorities/local/root.crt ./homebox-local-ca.crt
```

Review that certificate before trusting it on a device. Prefer a private VPN for remote use. The bundle does not configure router port forwarding.

Secret files are readable inside only the containers that mount them. Their host directory is mode `0700`; files are `0444` so non-root containers can read local Compose bind-mounted secrets. Protect the host directory and Docker access. Local Compose secrets are not an encrypted secret manager. Never put secrets in frontend build arguments, Git, image layers, or command-line values.

## Build and start

The deployment-specific Homebox Dockerfile pins Go 1.27.4, Node 22.23.5, pnpm 10.28.0, and Alpine 3.24.4. It uses the repository's frozen pnpm lockfile and Go module files. Its build context excludes deployment secrets and unrelated applications. The root Dockerfiles are unchanged.

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml config --quiet
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  build --build-arg COMMIT="$(git rev-parse HEAD)" homebox
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  build assistant-api homebox-mcp pwa
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  up -d --no-build --wait --wait-timeout 180
```

Build one native platform at a time on a small disk. The Homebox build bounds pnpm concurrency, the Node heap, and Go parallelism. Do not prune unrelated Docker data to make a build fit. Keep a pre-upgrade backup and the previous application images.

Homebox runs as UID/GID `65532`; Assistant API runs as UID `10001`. Fresh named volumes inherit each image's `/data` ownership. Do not replace them with a generic root-owned tmpfs. Bind-mounted existing data needs a deliberately scoped ownership migration.

For a **fresh** Homebox database, temporarily enable registration:

```sh
HBOX_ALLOW_REGISTRATION=true docker compose --env-file deploy/.env \
  -f deploy/docker-compose.yml up -d --no-deps homebox
```

Create the initial account through the separate Homebox hostname. Then restore the normal disabled-registration setting:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  up -d --no-deps homebox
```

The example configuration keeps registration disabled. Ensure `HBOX_ALLOW_REGISTRATION=false` remains in the protected environment file. Existing users sign in to the companion with their own native Homebox credentials.

## Model download, readiness and resources

Normal startup never downloads model weights. Ollama has no outbound network in the default stack. Download only after checking disk space on the target server:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  -f deploy/docker-compose.model-pull.yml up -d --no-deps ollama
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  exec ollama ollama pull qwen3-vl:2b
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  exec ollama ollama list
# Recreate Ollama WITHOUT the download network.
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  up -d --no-deps ollama
```

Record the downloaded model ID and retain the model volume if reproducibility matters. `qwen3-vl:2b` is a mutable model tag, not an immutable weight digest. The approximately 1.9 GB download is not the full inference memory requirement.

The default memory caps total **11.375 GiB**, leaving 4.625 GiB on a 16 GiB host before other workloads. Ollama gets 8 GiB, four CPUs, one parallel inference, one loaded model, a 4096-token default context, and a short queue. Tune only after measuring the actual CPU, heaviest supported images, memory use, and first-call latency. Docker Desktop's default VM allocation may be too small for the model profile.

Health checks are not inference benchmarks. Ollama health checks that its local API responds, not that the desired model is installed or accurate. Assistant readiness requires Homebox, MCP and local state, but **not Ollama**. Search/manual entry should remain available when the model is absent or stopped. The API limits uploads to 12 MiB and 24 million pixels; the proxy allows multipart overhead up to 16 MB.

No real model weights were pulled or CPU inference benchmarked during this implementation. A mock vision response in an integration test is not evidence of model quality or target-server performance.

## Existing outbound integrations

The internal Homebox network blocks external OIDC discovery/login, SMTP delivery, HTTP notifiers, remote MQTT brokers, external search, and update checks by default. These integrations do **not** work unchanged. Outbound access is not the same as a published port.

After reviewing the integration destinations, an operator can add a dedicated, untracked override:

```yaml
services:
  homebox:
    networks:
      - homebox_ui
      - inventory
      - integrations
networks:
  integrations: {}
```

Apply it with an additional `-f` argument to every relevant Compose command. This permits Homebox egress without publishing its ports. Prefer host/firewall controls that restrict destinations. Do not attach MCP or the assistant to this network without a separate need and review. The temporary model-download override is not a routine all-service egress setting.

## Tests in Docker

Run the application tests in their delivered images:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  run --rm --no-deps assistant-api python -m pytest
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  run --rm --no-deps homebox-mcp python -m pytest
docker build --target build -t homebox-pwa-test apps/pwa
```

The PWA Docker build runs its locked tests and build. Run the deployment checks without a production environment or inventory:

```sh
HOMEBOX_INSTANCE_ID=11111111-1111-4111-8111-111111111111 \
  docker compose --env-file deploy/.env.example -f deploy/docker-compose.yml \
  config --format json |
  docker run --rm -i --network none --read-only --cap-drop ALL \
    --mount "type=bind,source=$PWD/deploy/tests,target=/tests,readonly" \
    python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
    python -B /tests/check_config.py
sh deploy/tests/check-proxy.sh
sh deploy/tests/check-backup.sh
docker build --check -f deploy/Dockerfile.homebox .
```

The proxy test uses real Caddy/TLS and disposable private fixtures. The backup test uses generated files and verifies private ownership, all secrets, configuration, CA state, and stop/resume behavior. Neither test accesses existing inventory.

## Disposable browser acceptance

First build/tag the tested Assistant API, MCP and PWA runtime images with the `STACK_VERSION` in the example file. Do not use test-builder stages as runtime images. Then:

```sh
sh deploy/scripts/acceptance.sh prepare /tmp/homebox-acceptance-unique
sh deploy/scripts/acceptance.sh up /tmp/homebox-acceptance-unique
```

This creates its own UUID, secrets, named volumes and generated test accounts. It publishes only `127.0.0.1:8088` (companion) and `127.0.0.1:8089` (Homebox). The generated login details stay in mode `0600` at `/tmp/homebox-acceptance-unique/credentials.json`. The test-only HTTP exception is limited to `http://localhost`. Open `http://localhost:8088`, not a LAN address. Ollama is excluded unless explicitly enabled; test model-offline behavior before adding a protocol fixture.

Never apply `docker-compose.acceptance.yml` to an existing installation. Keep the acceptance stack until browser and backup/restore tests finish:

For a separate protocol-only photo success test, after testing the real model-offline fallback:

```sh
ACCEPTANCE_VISION_FIXTURE=1 sh deploy/scripts/acceptance.sh up /tmp/homebox-acceptance-unique
```

This adds the test-only fixture override and recreates the API with its internal fixture URL. It does not download or run a model. Restart/recreate that fixture if its source changes. Without the override, no fixture exists in production or normal acceptance.

```sh
sh deploy/scripts/acceptance.sh backup /tmp/homebox-acceptance-unique acceptance.tar.gz
sh deploy/scripts/acceptance.sh down /tmp/homebox-acceptance-unique
```

The final command removes only that generated project's Docker resources. Its protected credential and backup files remain for the operator to handle. Do not include them in a source archive or handoff.

## Image pins and updates

The production Compose file pins Caddy 2.11.9, Ollama 0.40.17 and the backup Alpine image by multi-architecture digest. The deployment Homebox Dockerfile pins its build/runtime bases by digest. Reviewed references:

- [Caddy 2.11.9](https://github.com/caddyserver/caddy/releases/tag/v2.11.9)
- [Ollama 0.40.17](https://github.com/ollama/ollama/releases/tag/v0.40.17)
- [Alpine supported releases](https://alpinelinux.org/releases/)

Local application tags must identify a retained source build, not `latest`. A base-image pin does not freeze APK repository contents or model tags. Update deliberately, rebuild, run tests/restore checks, and preserve rollback data and image provenance.

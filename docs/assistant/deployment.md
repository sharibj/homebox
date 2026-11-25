# Docker deployment

## Requirements and boundaries

Use Docker Engine 27 or newer, BuildKit, and Docker Compose **2.24.4 or newer**. The acceptance override uses Compose's `!override` tag. The default target is Linux `amd64` or `arm64`, a 16 GiB CPU-only home server, and two DNS names under your control. The delivered image has been built on native `arm64`; an `amd64` build still needs validation on that platform.

The existing Homebox source remains in `backend/` and `frontend/`. The root Compose file remains a development setup. Use only `deploy/docker-compose.yml` for this bundle.

Only Caddy publishes TCP ports 80 and 443 by default. The optional Wi-Fi override also publishes Caddy's native Homebox HTTPS listener on 8443. Homebox, Assistant API, MCP, PWA, and Ollama have no published ports. By default all application networks are internal Docker networks. Only Caddy has normal outbound access, for certificate issuance. External AI access requires the explicit API-only override below.

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

For Mac-only use, `homebox.localhost` and `inventory.localhost` can replace the two example names when the client resolves them to loopback. A phone's `.localhost` is the phone itself; use LAN/VPN DNS names for phone access. Keep `TLS_ISSUER=internal` and trust the reviewed local CA after starting the proxy.

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

After the proxy has started, export only Caddy's public CA certificate, never its private key:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  cp proxy:/data/caddy/pki/authorities/local/root.crt ./homebox-local-ca.crt
```

The file does not exist in a container until Caddy has started. With `internal`, review and trust this CA on each client. On macOS, import the certificate into Keychain Access and explicitly approve its SSL trust; some browsers need a separate import or restart. Prefer a private VPN for remote use. The bundle does not configure router port forwarding.

### Optional DNS-free Wi-Fi HTTPS

Keep `PROXY_BIND_IP=127.0.0.1` to preserve the desktop URLs. Set `PROXY_LAN_IP` in the protected environment file to the Mac's current Wi-Fi **RFC1918 IPv4 address**, not `0.0.0.0`. Include `deploy/docker-compose.lan.yml` on every subsequent configuration or recreation command. Include `deploy/docker-compose.external-ai.yml` as well when external AI is already enabled. For example:

```sh
docker compose -p homebox-inventory --env-file deploy/.env \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.external-ai.yml \
  -f deploy/docker-compose.lan.yml config --quiet
# Check queued/running capture and action counts before an API config restart.
docker compose -p homebox-inventory --env-file deploy/.env \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.external-ai.yml \
  -f deploy/docker-compose.lan.yml \
  up -d --no-deps --no-build --pull never --wait assistant-api proxy
```

The companion URL is `https://<PROXY_LAN_IP>`; its native Homebox links use `https://<PROXY_LAN_IP>:8443`. The original desktop aliases remain available. Only the selected Wi-Fi IP gets the added 443 and 8443 listeners; no internal application port is published. Trust the existing exported **public** CA certificate on the phone. Do not copy Caddy's private key or disable the firewall.

The LAN proxy checks the original `Origin` before changing any header. Every method except GET requires exactly the matching LAN origin. Missing, foreign, null, wrong-port, and duplicate origins are rejected. Only that exact companion origin is then changed to the unchanged canonical desktop origin. Upstream authentication, CSRF, cookies, and idempotency checks remain in place. A literal `route` preserves this order. The native listener validates its own exact origin without rewriting it.

The proxy sets the IP as default SNI for clients that omit SNI when using an IP URL. It keeps the existing CA and issues a certificate with that IP SAN. Run `deploy/tests/check_lan_access.py` in the proxy's container network namespace with `LAN_IP`, `LAN_CA_FILE`, and optional `LAN_EVIDENCE_FILE`. It checks TLS, both desktop aliases, and the Origin boundary without logging in or modifying inventory. Also verify the real host Wi-Fi URL, not only the container listener.

Backups include both LAN configuration files and record `LAN_HTTPS=1` when the running API uses this override. Restores accept these optional files and still accept older snapshots without them. Check the restored IP against the new host before starting. To remove Wi-Fi access, preserve the latest environment, wait for zero queued/running work, and recreate only the API and proxy without the LAN override, retaining the external-AI override when selected. No database or CA rollback is needed.

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

If chat and vision use distinct models on this Ollama service, run `ollama pull` for each distinct local model name while the temporary override is active. Each role prefers its explicit model, then shared `AI_MODEL`, then `OLLAMA_MODEL` only for native Ollama. Never pull an external OpenAI-compatible model name into Ollama. A role using another Ollama endpoint needs its weights on that server, not here. Keep the existing model volume and remove the download override afterward.

Record the downloaded model ID and retain the model volume if reproducibility matters. `qwen3-vl:2b` is a mutable model tag, not an immutable weight digest. The approximately 1.9 GB download is not the full inference memory requirement.

The default memory caps total **11.375 GiB**, leaving 4.625 GiB on a 16 GiB host before other workloads. Ollama gets 8 GiB, four CPUs, one parallel inference, one loaded model, a 4096-token default context, and a short queue. Tune only after measuring the actual CPU, heaviest supported images, memory use, and first-call latency. Docker Desktop's default VM allocation may be too small for the model profile.

`OLLAMA_MAX_LOADED_MODELS` defaults to `1`. Distinct role models can therefore reload when roles switch. Setting it to `2` may retain both only if measured memory headroom permits; it does not enable parallel inference. The default one-inference budget and the user's existing settings remain unchanged.

Health checks are not inference benchmarks. Ollama health checks that its local API responds, not that the desired model is installed or accurate. Assistant readiness requires Homebox, MCP and local state, but **not Ollama**. Search/manual entry should remain available when the model is absent or stopped. The API limits uploads to 12 MiB and 24 million pixels; the proxy allows multipart overhead up to 16 MB.

The automated suite uses model-offline and protocol fixtures. These do not measure real-model quality or target-server performance. Installing weights or passing health checks does not establish a vision-accuracy or latency result.

## Slow photo analysis and recovery

New clients submit a durable photo job, receive HTTP 202 after validation/storage, and poll its status using short requests. `queued` and `running` do not mean an inventory item exists. Saving still requires review and explicit confirmation. Reload recovery uses an opaque submission key/job ID, never a cached private image or draft.

The defaults are:

| Environment setting | Default | Allowed range |
| --- | --- | --- |
| `ASSISTANT_ANALYSIS_QUEUE_LIMIT` | 8 waiting jobs | 1–32 |
| `ASSISTANT_ANALYSIS_QUEUE_TIMEOUT_SECONDS` | 120 seconds | 1–600 |
| `ASSISTANT_ANALYSIS_TIMEOUT_SECONDS` | 900 seconds total running work | 1–3600 |
| `ASSISTANT_CAPTURE_SYNC_TIMEOUT_SECONDS` | 90 seconds for older clients | 1–120 |

The running budget includes schema repair and result enrichment, not a fresh budget per attempt. Keep the capture lifetime greater than queue plus running time; its default is 3600 seconds. Queue saturation, bounded timeouts or interrupted running work retain the photo as an explicit manual draft. Older synchronous clients return a manual draft before their browser deadline. Shared-volume locking permits one photo analysis across API workers.

These controls do not reduce the 1024-pixel normalized edge, 4096-token context, 1200-token prediction limit, strict schema or human confirmation. They do not make CPU inference fast. Avoid concurrent CLI warmups while assessing a photo: Ollama's single inference slot can queue either request.

## Conversation scope and recovery

The assistant shows the installation and active collection on each conversation. `ASSISTANT_INSTANCE_LABEL` sets an optional public display label, limited to 100 characters. Leave it blank for a stable short installation ID. Do not use a secret or internal URL as the label.

| Environment setting | Default | Allowed range |
| --- | --- | --- |
| `ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS` | 60 seconds for the whole turn | 1–120 |
| `ASSISTANT_CONVERSATION_TTL_SECONDS` | 1800 seconds | 1–28800 |

The turn budget includes lock waits, model extraction and repair, inventory reads, and state processing. Structured conversation state is encrypted and scoped to the signed-in browser session and collection. Switching collection or logging out invalidates the context, including a switch from A to B and back to A. Changing the chat provider, endpoint, or model also requires a reset; changing only vision settings does not.

Reload recovers the current conversation and linked action without repeating a turn. Read-only questions preserve a pending draft. Choosing a destination or typing “yes” does not confirm a write. An edit to a proposed action requires a new full preview; a running or uncertain action requires status recovery instead. Confirmation remains an explicit control.

For this upgrade, retain a full backup and the previous image set. Use one new `STACK_VERSION` across all four application image tags. Update the backward-compatible MCP first and wait for health, then the API, then the PWA. Do not rebuild or recreate Homebox, Ollama, or the proxy for a conversation-only upgrade.

## Chat and vision provider settings

Existing installations need no provider changes. Both roles default to native Ollama with the existing `OLLAMA_URL` and `OLLAMA_MODEL`. There is no automatic provider failover.

Shared defaults are `AI_PROVIDER`, `AI_BASE_URL`, `AI_MODEL`, and `AI_API_KEY_FILE`. Each role can override these independently with `AI_CHAT_PROVIDER`, `AI_CHAT_BASE_URL`, `AI_CHAT_MODEL`, `AI_CHAT_API_KEY_FILE`, or the corresponding `AI_VISION_*` settings. Supported providers are `ollama`, `openai-compatible`, `anthropic-messages`, and `gemini`. An explicit role model wins over `AI_MODEL`; `OLLAMA_MODEL` is the final fallback only for native Ollama.

A role inherits the shared URL only for the same provider kind. A shared key is inherited only for the same provider and canonical full base URL, including its base path. A key must never silently follow a role to a different endpoint. An empty `AI_*_API_KEY_FILE` environment value is unset, so permitted inheritance still applies. A nonempty explicit role file path wins; intentionally empty file contents select anonymous access. Fully specified roles do not require a shared URL or model.

For the same local Ollama endpoint with different models, set only `AI_CHAT_MODEL` and `AI_VISION_MODEL`. Keep the vision model image-capable, pull each distinct local model deliberately, and measure memory before raising the loaded-model limit.

For native Ollama chat and external vision, set these additional values in protected `deploy/.env`:

```dotenv
AI_CHAT_PROVIDER=ollama
AI_CHAT_MODEL=qwen3-vl:2b
AI_VISION_PROVIDER=openai-compatible
AI_VISION_BASE_URL=https://your-provider.example/v1
AI_VISION_MODEL=your-vision-capable-model
AI_VISION_API_KEY_FILE=/run/secrets/ai/ai_vision_api_key
```

For the reverse arrangement:

```dotenv
AI_CHAT_PROVIDER=openai-compatible
AI_CHAT_BASE_URL=https://your-provider.example/v1
AI_CHAT_MODEL=your-chat-model
AI_CHAT_API_KEY_FILE=/run/secrets/ai/ai_chat_api_key
AI_VISION_PROVIDER=ollama
AI_VISION_MODEL=qwen3-vl:2b
```

Leave shared overrides blank unless you intend them to apply. If both roles use one external endpoint, shared `AI_PROVIDER=openai-compatible`, `AI_BASE_URL`, `AI_MODEL` and `AI_API_KEY_FILE=/run/secrets/ai/ai_api_key` can supply their defaults. Roles can still choose distinct models or endpoints.

`openai-compatible` means Chat Completions. `anthropic-messages` selects native Anthropic Messages; `gemini` selects Google's native Gemini `generateContent` protocol, not an OpenAI-compatible shim. Changing a provider label does not translate an incompatible endpoint. The vision role must support images. URLs must not contain credentials or keys. Choosing an external service sends that role's submitted images/prompts to it; review privacy, retention and cost before opting in.

### Native Gemini with a free-tier image-capable model

Use `gemini-3.1-flash-lite` for lightweight text and image recognition. Google's [model page](https://aistudio.google.com/docs/models/gemini-3.1-flash-lite) lists image input, text output, structured JSON output, and free-tier input/output pricing, checked on November 23, 2026. It recognizes images; it does not generate them. Google does not publish parameter counts, so “smallest” here means the lightweight Flash-Lite option, not a verified parameter-size ranking.

The older `gemini-2.5-flash-lite` has [restricted access for new users](https://aistudio.google.com/docs/models#gemini-25-flash-lite). The newer `gemini-3.5-flash-lite` is another image-capable free-tier option. The selected 3.1 model has the lower published non-free text/image and output rates of those two generally available Flash-Lite versions.

For Gemini vision while retaining the existing chat role, set only these vision values in the protected environment:

```dotenv
AI_VISION_PROVIDER=gemini
AI_VISION_BASE_URL=https://generativelanguage.googleapis.com/v1beta
AI_VISION_MODEL=gemini-3.1-flash-lite
AI_VISION_API_KEY_FILE=/run/secrets/ai/ai_vision_api_key
```

Import a **Gemini API key**, not an existing Anthropic/OpenAI key or a service-account JSON file, using the unchanged initializer:

```sh
sh deploy/scripts/init-ai-secret.sh vision /absolute/protected/gemini-key-file
```

For Gemini chat instead, use the corresponding `AI_CHAT_*` settings and `chat` import. To select Gemini for both roles, set shared `AI_PROVIDER=gemini`, the same base URL and model, and `AI_API_KEY_FILE=/run/secrets/ai/ai_api_key`; explicitly clear unwanted role overrides and import with `shared`. Do not clear an existing role accidentally or send its provider's key to Google. The application uses the `x-goog-api-key` header, never a key query parameter.

Enable the existing external-AI override; retain the LAN override when already selected. No Ollama download, model warmup, new host port, or Gemini-specific secret mount is needed. Rebuild the API after source changes before selecting a newly supported provider; editing configuration does not add Gemini support to an older image.

**Free-tier eligibility does not guarantee zero charges or permission to use unpaid quota.** Check the project's [active quotas](https://aistudio.google.com/docs/rate-limits), [supported region](https://aistudio.google.com/docs/available-regions), [pricing](https://aistudio.google.com/docs/pricing), and [terms](https://aistudio.google.com/docs/terms) before importing a key or switching. Limits vary per project/model; capacity is not guaranteed. A billing-enabled project can incur charges. The terms checked on November 23, 2026 require **Paid Services when making API clients available to users in the EEA, Switzerland, or the UK**. A free price in the model table does not override that restriction.

Google's unpaid-service terms allow submitted prompts, images, and responses to be used to improve its products, including human review, and say not to submit sensitive, confidential, or personal information. Separate paid-service data-use terms apply in the EEA, Switzerland, and UK, including to unpaid quota. Review the terms for the actual region and project; do not assume that billing and data-use rules are identical. No paid provider/model fallback is automatic. Quota failures do not authorize upgrading a model or enabling billing.

Adding support does not switch an existing deployment. Before any live Gemini call, obtain a new protected key **file path**, confirm the selected role and regional/free-tier eligibility (or explicit bounded paid-use permission), and approve the applicable prompt/image data-use terms. Never paste the key into chat. Offline tests need neither a real key nor private photos.

### Native Anthropic gateway

For a native Anthropic gateway on the Docker Desktop host, chat can use:

```dotenv
AI_CHAT_PROVIDER=anthropic-messages
AI_CHAT_BASE_URL=http://host.docker.internal:6655/anthropic
AI_CHAT_MODEL=claude-haiku-4-5
AI_CHAT_API_KEY_FILE=/run/secrets/ai/ai_chat_api_key
```

This base path targets `/anthropic/v1/messages`. Use the actual base path and model supported by your gateway. `localhost` inside the API container means that container, not the Mac. Docker Desktop supplies `host.docker.internal` for host access. The gateway must be reachable there; do not expose it publicly as a workaround. The API still needs the deliberate outbound override below, even when the gateway runs on the host. Vision can remain on its existing Ollama defaults.

### Provider keys and outbound access

The key-file setting is a **container path**, not a raw key and not the source file's host path. The selected file contains only the key. Native Messages uses its own authentication/version headers and strict local result validation. Model tool calls do not grant authority to execute inventory changes.

Prepare the optional provider-key directory, even for an anonymous external endpoint:

```sh
sh deploy/scripts/init-ai-secret.sh
# Import only the selected keys from protected files, never command-line values:
sh deploy/scripts/init-ai-secret.sh vision /absolute/protected/vision-key-file
# Optional alternatives:
# sh deploy/scripts/init-ai-secret.sh chat /absolute/protected/chat-key-file
# sh deploy/scripts/init-ai-secret.sh shared /absolute/protected/shared-key-file
```

The exact host files are `deploy/secrets/ai/ai_api_key`, `ai_chat_api_key`, and `ai_vision_api_key`; their container paths are under `/run/secrets/ai/`. The initializer never prints or replaces an existing key and never changes the original three secret values. With no arguments it creates no key files. Importing an intentionally empty source file is allowed for an explicit anonymous-role override. The host parent stays `0700`, the mounted provider-only directory is `0755`, and files are `0444` so API UID 10001 can read them.

Enable API-only outbound access and the read-only provider-key directory:

```sh
dce() {
  docker compose -p "${COMPOSE_PROJECT_NAME:-homebox-inventory}" \
    --env-file deploy/.env -f deploy/docker-compose.yml \
    -f deploy/docker-compose.external-ai.yml "$@"
}
# If LAN HTTPS is selected, also add -f deploy/docker-compose.lan.yml
# before "$@" in the function. Keep all three overlays for that deployment.
dce config --quiet
dce up -d --no-deps --no-build --pull never --force-recreate \
  --wait --wait-timeout 180 assistant-api
```

This override forces no provider, URL or model. The API validates the resolved roles. It adds no host port and changes no Homebox/MCP/PWA egress. The normal local Ollama stack does not mount the provider directory or require a fourth secret. Do not put key contents in `.env`, browser settings, build arguments, logs or chat.

Compose reads `--env-file` when creating a container. Editing `.env` does not update a running process, and `restart` retains the container's old environment. Configuration-only changes need no build; recreate only the API with the same selected override files. `--force-recreate` also reloads a changed key when its path is unchanged. `config --quiet` checks Compose wiring, not semantic provider validity; an unsupported kind or unreadable key can still fail API startup.

Keep the same selected Compose files for updates/restarts. A base-only recreate drops optional egress and key mounts. To return to defaults, clear unwanted shared and role overrides, keep `AI_PROVIDER=ollama`, then recreate only the API using the base file.

Backups include nested role keys, the external override and `deployment-options.env`, which records whether it was active. Restore these with the matching environment/state. Tests require no real external key or provider call. Public role metadata contains only provider kind/model, never URL/key. Pending photo work must not silently move to a changed provider, URL or vision model.

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
EXTERNAL_PROVIDER_FIXTURE=1 sh deploy/tests/check-backup.sh
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

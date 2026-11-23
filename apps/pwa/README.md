# Homebox Companion PWA

A separate mobile-first Vue 3, TypeScript, and Vite app. It does not change Homebox's existing frontend. Serve the companion on its own origin so Homebox's service worker cannot control it.

## What it supports

- Native Homebox password sign-in and an explicit active collection. OIDC-only sign-in is not supported.
- Grounded chat, item search, record details, location contents, and nested locations.
- Camera/file selection, local preview, duplicate suggestions, editable metadata, and reviewed create.
- Manual entry with a retained photo when the server accepts the upload but the model is unavailable.
- Confirmed moves, edits, replacement tags, and new locations.
- Stable authenticated NFC URL landing pages, QR labels, reassignment, and revocation. Use a separate phone app to write NFC URL records.
- An install manifest and an offline **static shell**, not offline inventory or writes.

The API contract is in `../assistant-api/CONTRACT.md`. All calls use the same-origin prefix `/api/assistant/v1`. The reverse proxy must forward that prefix unchanged. `/t/<tagId>` is a client-side landing route. The container serves static files on port 8080 and reports health at `/healthz`.

## Browser security and recovery

The browser sends the opaque HttpOnly session cookie. It never receives a Homebox token or stores credentials in localStorage or sessionStorage. Authenticated writes send `X-CSRF-Token`. All collection-dependent requests send the collection displayed in the UI as `X-Collection-ID`.

On a collection mismatch, the app clears private in-memory state, reloads the session, and shows a warning. It does not retry the original request. Local collection switches also clear drafts, photos, selected records, chat, and actions. Delayed responses from an old session context are discarded.

Before enabling inventory work after sign-in or reload, the app loads current-session action history. An optional `?action=<opaque-id>` restores one action. An unavailable action does not block the new session. No private payload is stored in the URL.

The confirmation card shows the approved payload separately from resolved values. Confirmation uses the API's `confirmationKey`. A failed change can retry only when the backend marks it `retryable`. An uncertain result offers status inspection, never an automatic retry or fresh create.

The service worker caches an explicit build-time list of public static files. It does not intercept API requests, cache navigation responses, or cache photos and other private responses.

## Build and test in Docker

Run from this directory. No host Node installation is required.

```sh
docker build -t homebox-companion-pwa:local .
```

The build runs locked dependency installation, unit tests, the Vue/TypeScript check, icon generation, and the production build. The runtime uses unprivileged nginx. The Docker deployment bundle supplies the separate-origin proxy and API.

For a source-mounted test/build:

```sh
docker run --rm -v "$PWD:/app" -w /app node:22.22.0-alpine npm ci --no-audit --no-fund
docker run --rm -v "$PWD:/app" -w /app node:22.22.0-alpine sh -c 'npm test && npm run build'
```

## Disposable browser tests

`tests/fixture-server.mjs` serves the production build with synthetic, in-memory API responses. It is **not** a production API, a real model, or evidence of Homebox compatibility. It uses only disposable test credentials. Do not expose it beyond loopback.

```sh
docker run --rm -d --name homebox-pwa-ui-fixture \
  -p 127.0.0.1:4173:4173 -v "$PWD:/app:ro" -w /app \
  node:22.22.0-alpine node tests/fixture-server.mjs
docker build -t homebox-pwa-browser:local tests/browser
docker run --rm --network container:homebox-pwa-ui-fixture \
  -v "$PWD:/workspace:ro" -v "$PWD/test-results:/results" \
  homebox-pwa-browser:local
docker stop homebox-pwa-ui-fixture
```

The browser image and client both use Playwright 1.58.2. The first browser-image pull is roughly 0.9 GB compressed. Check free disk space first. Subsequent runs reuse the image. Tests cover review/confirm gates, photo removal, cancel/edit, cross-tab collection changes, unavailable action recovery, lost-response recovery, NFC mappings, model-offline manual entry, and static-only offline behavior.

`tests/browser/live.spec.mjs` runs separately against a disposable real Docker stack. Set `BASE_URL` to its companion origin and mount its protected credential JSON as `ACCEPTANCE_CREDENTIAL_FILE`. The file has `users:[{email,password}]` for two isolated test accounts. Keep the API's configured public origin equal to the browser origin. Do not run this test against real inventory. It creates synthetic locations, an item, a photo attachment, and tag mappings. Live-test traces are disabled so they cannot retain sign-in credentials.

## Checks that need real hardware

Desktop Chromium does not verify phone camera behavior, HEIC decoding on the target device, home-screen installation, or physical NFC stickers. Test those over the deployment's real HTTPS hostname. Model recognition quality and CPU latency also require the actual model and target server; synthetic browser tests do not measure them.

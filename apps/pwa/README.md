# Homebox Companion PWA

A separate mobile-first Vue 3, TypeScript, and Vite app. It does not change Homebox's existing frontend. Serve the companion on its own origin so Homebox's service worker cannot control it.

## What it supports

- Native Homebox password sign-in and an explicit active collection. OIDC-only sign-in is not supported.
- Scoped conversational turns, inventory overviews, paged item/destination cards, and distinct Use/View controls.
- Server-owned pending tasks, destination clarification, editable full previews, and explicit interrupted-turn recovery.
- Item search, record details, location contents, and nested locations.
- Camera/file selection, local preview, duplicate suggestions, editable metadata, and reviewed create.
- Durable photo jobs with short status polls, reload recovery, and manual edits preserved while analysis runs.
- Manual entry with a retained photo when the server accepts the upload but the model is unavailable.
- Confirmed moves, edits, replacement tags, and new locations.
- Stable authenticated NFC URL landing pages, QR labels, reassignment, and revocation. Use a separate phone app to write NFC URL records.
- An install manifest and an offline **static shell**, not offline inventory or writes.
- A separate AI settings page for the installation's original account, with independent Chat and Images providers, base URLs, model IDs, and write-only key updates.

The API contracts are in `../assistant-api/CONTRACT.md` and `../assistant-api/CHAT_CONTRACT.md`. All calls use the same-origin prefix `/api/assistant/v1`. The reverse proxy must forward that prefix unchanged. `/t/<tagId>` is a client-side landing route. The container serves static files on port 8080 and reports health at `/healthz`.

## Browser security and recovery

The browser sends the opaque HttpOnly session cookie. It never receives a Homebox token or stores credentials, private history, photos, or drafts in localStorage or sessionStorage. Authenticated writes send `X-CSRF-Token`. All collection-dependent requests send the displayed collection as `X-Collection-ID` and the server's context version as `X-Context-Version`.

On a collection mismatch, the app clears private in-memory state, reloads the session, and shows a warning. It does not retry the original request. Local collection switches also clear drafts, photos, selected records, chat, and actions. Delayed responses from an old session context are discarded.

Before enabling inventory work after sign-in or reload, the app loads current-session action history. An optional `?action=<opaque-id>` restores one action. An unavailable action does not block the new session. No private payload is stored in the URL.

The confirmation card shows the approved payload separately from resolved values. Confirmation uses the API's `confirmationKey`. A failed change can retry only when the backend marks it `retryable`. An uncertain result offers status inspection, never an automatic retry or fresh create.

### Conversation

The browser sends one new message, issued selection, or page continuation per turn. It sends the server's conversation ID/revision and a fresh turn key, not a raw transcript. Explicit retry reuses the exact request and key. The visible thread is bounded to forty messages in memory.

Read-only side questions retain the pinned task. Use selects an issued candidate; View only opens its record. Selecting, typing “yes,” and Review draft never confirm. Review draft starts a new normal-mode turn to prepare a complete preview. Only the current review has an interactive confirmation control. Opening history first fetches its current server status.

The composer stays editable during chat. Enter sends, Shift+Enter inserts a line, and IME composition does not submit. Newly typed text survives a late response. Readers who scroll away are not pulled to the bottom; a New response button appears when needed.

Reload restores only the latest saved reply/task through `GET /conversations/current`, not a fabricated transcript or unsaved local text. The server serializes that read with active turns. A timeout, busy response, or interrupted turn keeps Confirm and Cancel blocked across inline and global/history reviews. `chat_turn_interrupted` offers Restore saved review, an explicit reset request that recovers any durable action without resending lost text. Only successful authoritative recovery, replay, or reset unlocks review controls.

Manual/photo proposal conflicts retain all fields and the selected photo. The app reads existing changes and the current conversation instead of retrying or cancelling. A related photo is identified by the original action and the server's exact `pendingOperation.captureId`, never by the fact that a reply came from chat. Success removes the consumed photo/job draft. Newer manual edits and unrelated drafts remain intact.

### Photo progress

The app uploads once to `POST /capture-jobs` with a fresh `Idempotency-Key`. The server returns a saved job before inference. The app then checks that job about every two seconds with separate 15-second GET timeouts. It shows the server's queued/running state and elapsed time, not an estimated percentage.

If the upload response is lost, the app searches by the same opaque submission key. It never automatically uploads again or confirms an inventory change. A reload can restore `?captureJob=<opaque-id>` or `?captureSubmission=<opaque-key>` and can find active jobs in the current session. Unavailable jobs show a warning without blocking manual work.

Manual edits stay in memory while the job runs, including fields edited and then cleared. A reload does not preserve those unsaved edits or the local photo preview. It can recover the server's retained photo and suggestions. Removing/replacing a photo, signing out, or switching collections stops polling and rejects old responses. Stopping progress does not cancel server analysis.

The optional public AI metadata appears behind the compact AI details button. Open it to see each role's provider, full model name, and endpoint disclosure: Chat and Images. Unknown providers use a neutral label and endpoint disclosure, never an Ollama or local label. This read-only dialog never shows base URLs or API keys. Older shared metadata and servers without metadata still work.

The service worker caches an explicit build-time list of public static files. It does not intercept API requests, cache navigation responses, or cache photos and other private responses.

### AI settings

Only `session.capabilities.canManageAISettings === true` exposes the AI settings tab. The server authorizes every settings request against its pinned original-account UUID. Collection ownership, superuser status, and first sign-in never grant access. A local operator must provision the original-account binding. An absent capability hides the tab. Direct `/?page=ai-settings` entry also fails closed, and a server 403 removes access and clears the editor.

The page sits outside inventory collection and recovery gates. It can load settings even when inventory recovery fails or no collection is selected. Offline users can open the page, but loading and saving require a connection.

The form supports the four existing provider kinds and custom exact model IDs. Base URLs are visible only in this authorized editor. Enter a replacement key in a password field, keep the current key setting, or explicitly clear it. Blank input never means clear. Changing a provider or complete endpoint requires Replace or Clear; changing only the model can Keep.

Nothing changes until **Save both roles** sends one revision-checked PUT. Defaults remain active before that first Save. Saving does not contact a provider, list or download models, or run inference. A changed revision or uncertain response keeps the draft and requires an explicit reload before another Save. Reload warns before discarding edits. The saved public metadata refreshes only from a successful server response.

Keys stay only in component memory. Save, discard, sign-out, permission/session loss, unmount, and pagehide clear them. A page-cache restore reloads only redacted settings. Tab changes, home navigation, sign-out, and browser unload warn about unsaved edits. Settings and keys never enter browser storage, conversation history, logs, or the URL. The server returns only `keyConfigured`, never key material. See `../assistant-api/AI_SETTINGS_CONTRACT.md`.

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
  -v "$PWD:/app:ro" -w /app \
  node:22.22.0-alpine node tests/fixture-server.mjs
# Build the large browser image only if it is not already available.
docker build -t homebox-pwa-browser:local tests/browser
docker run --rm --network container:homebox-pwa-ui-fixture \
  -v "$PWD:/workspace:ro" -v "$PWD/test-results:/results" \
  -v "$PWD/tests/browser/companion.spec.mjs:/browser/companion.spec.mjs:ro" \
  -v "$PWD/tests/browser/async-capture.spec.mjs:/browser/async-capture.spec.mjs:ro" \
  -v "$PWD/tests/browser/ai-details.spec.mjs:/browser/ai-details.spec.mjs:ro" \
  -v "$PWD/tests/browser/ai-settings.spec.mjs:/browser/ai-settings.spec.mjs:ro" \
  -v "$PWD/tests/browser/conversation.spec.mjs:/browser/conversation.spec.mjs:ro" \
  -v "$PWD/tests/browser/conversation-visual.spec.mjs:/browser/conversation-visual.spec.mjs:ro" \
  homebox-pwa-browser:local npm test -- companion.spec.mjs async-capture.spec.mjs ai-details.spec.mjs ai-settings.spec.mjs conversation.spec.mjs conversation-visual.spec.mjs
docker stop homebox-pwa-ui-fixture
```

The browser image and client both use Playwright 1.58.2. The first browser-image pull is roughly 0.9 GB compressed. Check free disk space first. Subsequent runs reuse the image. The suites cover review/confirm gates, photo removal, cancel/edit, cross-tab collection changes, unavailable action recovery, lost-response recovery, NFC mappings, model-offline manual entry, and static-only offline behavior. Async cases include an actual 184-second simulated analysis, edit preservation, interrupted upload/status recovery, session expiry, content-key conflicts, and role-specific provider labels. Conversation cases cover task continuation, stale choices, paging, safe reload/reset, one current confirmation, compact 360px layout, and related/unrelated photo overlap. The full suite takes about five minutes. Its synthetic delay does not measure real model latency or recognition quality. To skip the long case, append `--grep-invert 'real 184-second'`.

`tests/browser/live.spec.mjs` runs separately against a disposable real Docker stack. Set `BASE_URL` to its companion origin and mount its protected credential JSON as `ACCEPTANCE_CREDENTIAL_FILE`. The file has `users:[{email,password}]` for two isolated test accounts. Keep the API's configured public origin equal to the browser origin. Do not run this test against real inventory. It creates synthetic locations, an item, a photo attachment, and tag mappings. Live-test traces are disabled so they cannot retain sign-in credentials.

`tests/browser/conversation-live.spec.mjs` is a separate real-Haiku acceptance gate. It requires an explicitly authorized disposable stack, protected `CONVERSATION_QA_CREDENTIAL_FILE` (`username,password`), independent `CONVERSATION_QA_ORACLE_FILE`, and generic synthetic `CONVERSATION_QA_IMAGE_FILE`. `EXPECTED_PWA_ASSET` pins the tested JavaScript build. Do not point it at production. Run its tests in file order against the matching fresh fixture: it creates exactly one reviewed `mobile charge`, checks repeat confirmation and scope/reload recovery, and reads a pixel-only marker through actual photo analysis without creating another item. It disables traces and automatic screenshots; only explicit synthetic screenshots and safe evidence are saved.

## Checks that need real hardware

Desktop Chromium does not verify phone camera behavior, HEIC decoding on the target device, home-screen installation, or physical NFC stickers. Test those over the deployment's real HTTPS hostname. Model recognition quality and CPU latency also require the actual model and target server; synthetic browser tests do not measure them.

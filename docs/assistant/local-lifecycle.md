# One local deployment utility

`deploy/homebox` is the single entrypoint for this deployment bundle. It uses
Docker Engine, Compose 2.24.4 or newer, a POSIX shell, and the existing pinned
Python helper image. It does not require host Python, Node, Go, or new packages.

**Validated with isolated synthetic Docker installations. No production
installation was changed. Existing installations require explicit adoption.**

## New local installation

```sh
./deploy/homebox --yes fresh
./deploy/homebox status
```

Fresh uses separate `homebox.localhost` and `inventory.localhost` names, loopback
listeners, internal TLS, disabled public registration, and the existing Ollama
defaults. It builds all four application images from local source using the
production Dockerfiles. Runtime image downloads are not model-weight downloads.
Manual/search service health does not prove AI configuration, model availability,
inference quality, or free external-provider billing.

`./deploy/homebox models status` checks installed-model metadata without inference.
For roles that actually select this local Ollama, the separate explicit command
`./deploy/homebox models pull --confirm-download` downloads only their distinct
selected models, then removes the temporary download network. It briefly
recreates only Ollama. It never pulls an external provider's model into Ollama or
changes provider/model settings. Review disk, bandwidth and memory first.
After an interrupted download, `models restore-network` restores the normal
Ollama network without downloading models or running inference. The durable pending
record prevents an ordinary redeploy from accidentally retaining download egress.

No model is pulled, provider switched, firewall changed, certificate trusted, or
account created automatically. Review and trust only Caddy's public CA on each
client. Never export its private key.
Successful startup copies that public certificate to the private state
directory's `work/public-ca.crt` for review and client import; it changes no
operating-system or browser trust store.

An existing project or data volume makes `fresh` fail. Use `redeploy` for an
existing installation. `fresh --resume` is only for a reviewed interrupted setup;
it preserves existing UUID/configuration/keys and refuses to regenerate missing
encryption keys when data exists.

## Add direct Tailscale access to an existing installation

Use the original checkout, project, and private state directory. The installation
must already have a completed `ready` journal, its established owner receipt,
and all six healthy services. Healthy containers alone do not prove setup
completed. This command refuses unfinished setup; do not reset data or invent an
adoption receipt to bypass that refusal.

```sh
./deploy/homebox status
tailscale ip -4
./deploy/homebox --yes access tailscale --ip 100.99.101.89
```

Replace the example with the server's locally assigned Tailscale IPv4. The
command supports the full `100.64.0.0/10` range, not a list of client addresses.
It keeps loopback port 80/443 and adds port 443/8443 on only that selected address.
Without `--listen-all`, it never binds a wildcard address. The explicit
`--listen-all` mode instead publishes only the two UI HTTPS ports, 443 and 8443,
on all IPv4 interfaces. The advertised Tailscale address remains the TLS and
Origin identity; `0.0.0.0` is not a browser URL. Neither mode changes Tailscale,
ACLs, firewall rules, provider settings, Ollama, or models. IPv6 is not included.

Open `https://100.99.101.89` for the companion and
`https://100.99.101.89:8443` for native Homebox. Trust the existing **public**
Caddy CA on each browser device. Do not bypass TLS checks or export the private
key. Tailnet routing and ACLs must permit access; namespace tests do not prove
that a remote device can reach the server.

This configuration-only operation retains all six exact image IDs. It can
recreate only the API and proxy. It preserves the four other containers, data
volumes, networks, UUID, core/provider secrets, accounts, sessions, registration
flag, and existing CA. It builds and downloads no image or model. It records a
private pending plan before changing configuration, closes ingress, and drains
active work without forced cancellation. It commits the protected environment
and receipt only after runtime, CA/hostname TLS, auth and static-byte checks.

If interrupted, use one of these explicit commands:

```sh
./deploy/homebox --yes access resume
./deploy/homebox --yes access rollback
```

Rollback applies only to a pending access operation. It restores its original
configuration, not a database or data backup, and does not require Tailscale to
remain available. Recovery first authenticates and closes its owned proxy before
the remaining guards. A rejected recovery leaves that verified ingress closed.
An uncatchable kill after a detached proxy starts can leave it running until
recovery begins. A stale lock requires review, not automatic removal. Restore
the reviewed source/configuration after a drift refusal; never use reset, prune,
key regeneration, or a data restore to change access.

## Local source redeployment

For a stack installed with the older deployment helpers, first bind its existing
healthy services to this utility. Adoption checks the actual volumes, data and
secret mounts, UUID, configuration and runtime settings. It does not start or
recreate containers, generate keys, register an account, or replace data.

```sh
./deploy/homebox --yes adopt
```

This is an explicit first-time step, not a fallback during redeployment.
An already managed installation must use its original project and state path.

```sh
./deploy/homebox --yes redeploy
```

The utility discovers the installation's base, external-AI and LAN overlays from
its existing Compose labels. It refuses a different checkout, Docker daemon,
project, unknown overlay, renamed/external data volume, or material configuration
drift rather than silently starting a different inventory.

The protected `deploy/.env` controls application configuration, including omitted
settings that use Compose defaults. The utility clears conflicting shell values
for variables referenced by the selected Compose files. It also clears ambient
`COMPOSE_*` controls. It preserves the selected Docker context and explicit CLI
options. Do not put Docker client controls in the deployment file.

Before any backup or writer startup, it checks actual volume creation identities,
ownership labels, data and secret mount bindings, secret hashes and permissions,
and the installation UUID against its receipt. Rendered volume names alone are
not proof that the original data still exists. Missing or substituted volumes,
missing containers, or stopped services make ordinary maintenance fail before
it can create empty replacements. Review and recover the original installation
instead of using `fresh` to bypass that refusal.

It freezes source inputs before building, uses Docker cache, runs API/MCP tests
for rebuilt images, and retains old image IDs and rollback aliases. The PWA
production Dockerfile runs its own unit/type/build checks. Unchanged image IDs
do not need container recreation.

Before a needed cutover it closes ingress, waits for capture/action/chat work to
drain without forced cancellation, and invokes the existing consistent backup
helper. Backup includes databases, attachments, assistant state/uploads, keys,
configuration and Caddy state. It never stops Ollama. Backups remain private in
`deploy/backups/`, not in shared source/evidence.

Only the active `STACK_VERSION` changes. Registration, accounts, provider/model
selection, endpoints, key selections, origins, resource/security settings, volume
and network identities stay unchanged. Health barriers precede reopening access.
Final checks verify actual images, persistent mounts, networks, CA/hostname TLS,
public native status, unauthenticated companion access and exact static bytes.
No smoke test signs in, submits an image, calls a model, or changes inventory.

Private receipts default to
`${XDG_STATE_HOME:-$HOME/.local/state}/homebox/homebox-inventory`. They contain
sensitive configuration. Keep this directory private and do not publish it.
`--project NAME --state-dir /absolute/private/path` selects another deliberate
installation, never an automatic fallback.
The private `deploy/.env.lifecycle-owner` receipt binds that canonical path to
the checkout, project and Docker daemon. All commands also share one installation
lock in the checkout, so different state paths cannot run competing operations.
Do not remove a receipt or lock to bypass a refusal. Review an interrupted
operation first. The source checkout, owner receipt and private state directory
need their own secure retention; the existing data archive does not include
these wrapper receipts.

Failures before cutover leave the active environment/images unchanged. A failed
new writer can have migrated its database, so the utility does **not** pretend an
automatic image downgrade or database restore is safe. It leaves a recovery
receipt and closes ingress. Review the retained images, previous protected
environment and backup before an authorized recovery. Never use `down -v`,
prune, secret regeneration or a data restore merely to change images.

```sh
./deploy/homebox --yes backup
```

## First owner and routine users

An empty Homebox database has no first-user exemption when registration is
disabled. Bootstrap is an **explicit** action for a fresh managed installation:

```sh
./deploy/homebox users bootstrap
```

It briefly enables public registration, recreates only native Homebox, registers
the initial owner, then restores the exact prior flag and recreates Homebox
again. This causes brief native-service downtime. Its durable recovery record
must restore the prior flag after interruption. Never infer “empty” from a
failed login or member-list request.

After a terminated bootstrap, `./deploy/homebox users restore-registration`
restores the recorded prior flag and recreates only Homebox. It does not register
another account. Inspect the result before trying signup again.
Recovery ignores conflicting ambient Compose values. It verifies the healthy
native container's environment and its actual public registration status before
clearing the recovery marker. A failed verification keeps that marker.
Recovery also checks the desired data and secret mounts before startup. It
requires the selected native image to resolve locally to the running image ID.
It refuses configuration drift or a different writer image instead of combining
registration recovery with an upgrade.
An interrupted signup may have committed even if the client did not receive its
response. A durable attempt receipt blocks duplicate-owner signup in that state;
verify the original login instead. Do not infer that an account is absent from a
timeout or failed enumeration.

### Guided new account in an existing household

The simplest owner-led flow is:

```sh
./deploy/homebox users create
```

Use a private interactive terminal. Sign in as the owner of the existing
household, choose its displayed name and UUID, then enter the new member's name,
email and password. Password entry is hidden and the new password must match
twice. The helper refuses an insecure password-input fallback.

It creates a one-use, 15-minute invitation and sends its token only in the JSON
registration body. Native Homebox still decides whether the signed-in account
owns that household. A read-only native-data helper supplies private ownership
hints for the picker; hints never bypass the authenticated owner check.
The new account is an ordinary household member, not an owner, superuser or
AI-settings administrator. Homebox retains its normal default/personal-household
behavior. No service restarts, registration-policy changes or inventory edits
are needed.

For automation, put two owned, nonsymlink mode-`0600` JSON files in the original
private lifecycle state directory. The owner file needs `username` and
`password`; the new-member file also needs `name`. Use a private editor, not
password-bearing shell commands. Select the household explicitly:

```sh
./deploy/homebox users create --credentials-file owner-login.json \
  --new-user-file new-member.json --collection HOUSEHOLD_UUID
```

No default household is inferred, and global `--yes` does not supply credentials
or choose a household. Passwords and tokens must never appear in arguments,
environment values, stdout or shared logs.

A private `new-member-*.json` receipt records progress without passwords or
invitation tokens. Once a registration request might have been sent, repeating
the same operation performs only login and membership checks. It never silently
repeats signup, resets a password or joins an existing account. If confirmation
fails or membership is absent, stop and have the household owner review the
existing account and receipt. Do not delete the receipt to force another signup.
Native uniqueness checks remain decisive when a failed login cannot establish
whether an account already exists.

### Original account for AI settings

Only the original installation account can edit the companion's **AI settings**
page. Household ownership, superuser status and first login do not grant that
permission. Provision its immutable identity from the installation's original
checkout, project and private state directory:

```sh
./deploy/homebox users ai-primary
./deploy/homebox users ai-primary --confirm-original-user-id ORIGINAL_USER_UUID
```

The first command is read-only. It displays only the unique earliest **current**
native user UUID and creation time, never an email or password. Current ordering
does **not** prove which account was original: an earlier account may have been
deleted. Confirm the account's installation history before passing the explicit
UUID. If that history is uncertain, stop. Global `--yes` is not this attestation.

The second command pins that explicitly attested UUID and creation proof to the
installation. Repeating the same confirmation is safe. A missing, tied or
malformed native record, a different previous binding, or deletion of the pinned
account causes refusal, not reassignment. Both commands require the canonical
completed installation and all six healthy services, with no pending deployment,
registration, model or access transition.

The native database is read only by an ephemeral helper, never mounted into the
assistant API. Provisioning changes no native account, role, registration flag,
provider, image, key, CA or deployment setting. The local deployment owner
receipt identifies installation files; it is not a native-user permission.

### Separate invitations and access removal

Routine users can also use a one-use invitation, not public signup:

```sh
./deploy/homebox users collections
./deploy/homebox users members --collection COLLECTION_UUID
./deploy/homebox users invite --collection COLLECTION_UUID --output invitation.json
./deploy/homebox users accept-invite --invitation-file invitation.json
./deploy/homebox users remove-member --collection COLLECTION_UUID \
  --user MEMBER_UUID --confirm-revoke
```

Credentials use a private interactive TTY. For automation, `--credentials-file`
accepts a plain filename in the private state directory, owned by the invoking
user with mode `0600`. JSON contains `username` and `password`; new-user commands
also need `name`. Never put passwords, login tokens, invitation tokens or
token-bearing URLs in arguments, environment values, logs, source or shared
evidence. Invitation files use the same private directory, never stdout; deliver
them to the intended recipient through a secure channel.

Invitations expire after 15 minutes and allow one use. Routine user commands do
not restart services or change the registration flag. Invitations work while
public signup is disabled. Owner-only native APIs enforce authorization. Every scoped operation
requires an explicit collection UUID; no default collection is guessed.
`accept-invite` creates a new native account. Joining an existing account to a
collection is not supported by this utility. The native join API puts invitation
tokens in a URL path that the native server's default request logger records.
This utility omits that flow; it does not weaken server logging as a workaround.

`remove-member` revokes access to **only the selected collection**. It does not
delete the account, remove other memberships, log out existing sessions, or
delete inventory/attachments. Homebox can create a personal collection if the
removed user otherwise has none. Global administrator account deletion and
collection deletion are intentionally absent: native self-deletion can orphan
inventory, and collection deletion destroys shared data.

The account helper uses the native HTTP API on loopback inside Homebox's own
container network namespace. Credentials never traverse an exposed HTTP port.
It never bypasses native authorization or writes SQLite directly.

## Docker-only validation

```sh
sh deploy/tests/check-lifecycle.sh
sh deploy/tests/check-users.sh
```

The unit and syntax checks mount only the new public utility sources. They use
no Docker socket and need no real environment, inventory or credentials.
The focused account suite adds 26 unit checks. Its retained six-service Docker
fixture also checks original-account binding, closed-registration membership,
uncertain signup recovery and private TTY prompts with hidden passwords.

The focused `deploy/tests/access_integration.py` driver runs inside a Docker
controller with a reviewed public-only snapshot at `/source`. It requires
`--workspace /absolute/new/private/path --subnet UNUSED_PRIVATE_CIDR` and
`--image-source-project OWNED_SYNTHETIC_PROJECT`. The selected image-source
project must start with `homebox-lifecycle-test-` and have all six cached
application images. There is no machine-specific default. Add
`--image-preflight-only` to read the six selected image IDs without creating or
changing a fixture. The transition test downloads no image or model and retains
its stopped application containers and volumes.

The full harness has a separate default-installation mode. Both require a
reviewed public-only source snapshot, a new private synthetic workspace, explicit
non-overlapping internal subnets, and the Docker socket. Never mount a real
deployment or inventory into a test controller. Its build client needs public
registry access; application test networks remain internal and publish no ports.
The default-mode missing-volume case deliberately removes only its own empty
assistant test volume, after a verified backup. It retains that archive and the
remaining test data.

The earlier lifecycle release passed 45 utility unit/syntax checks, 19 full integration
checks and 7 default-installation checks. Both integration suites exited zero.
They exercised actual native and PWA source rebuilds, old sessions, account and
membership changes, quantity/location/attachment preservation, shared locking,
registration recovery with omitted settings, and failure refusal/recovery.
Candidate-image tests also passed 543 API tests and 130 MCP tests.

No real provider, model download or inference ran. Model downloads, backup
restoration, active real-work draining, cached-service-worker upgrades, physical
phones were not validated. The focused account check separately exercised
interactive CLI prompts, not browser interactions. Deployment checks do not
establish live model quality, provider reachability or billing.

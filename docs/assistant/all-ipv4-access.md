# Listen on every IPv4 interface

This is an explicit access change for an existing, completed installation. It
does not change the default loopback-only deployment.

This mode is for a single Tailnet identity. For Wi-Fi and Tailscale at the same
time, use [precise dual access](wi-fi-access.md) instead. Dual access does not
support wildcard HTTPS listeners.

After the reviewed change is installed, run this on the Linux host:

```sh
TAILNET_IP=$(tailscale ip -4)
./deploy/homebox --yes access tailscale --ip "$TAILNET_IP" --listen-all
```

Use the installation's normal project and state-directory options if they
differ from the defaults. The command checks that the selected Tailnet IPv4 is
locally assigned and that the original six services and ready journal match.
Healthy services alone are not a completed deployment receipt.

## What changes

Only the proxy publishes host ports:

| Host listener | Purpose |
| --- | --- |
| `127.0.0.1:80` | Existing loopback HTTP listener |
| `0.0.0.0:443` | Inventory Companion HTTPS, including existing hostnames |
| `0.0.0.0:8443` | Native Homebox HTTPS |

The wildcard bindings include loopback and the Tailnet address. The command
replaces the separate HTTPS bindings rather than adding overlapping bindings.
It does not publish native port 7745, the PWA server, the assistant API, MCP,
or Ollama.

All IPv4 interfaces includes LAN and any public interface. Restrict access
with the host firewall and network policy as needed. This command does not
change firewall rules, Tailscale ACLs, Serve, Funnel, routes, or CA trust.
It does not add IPv6 listeners.

Continue to use the canonical URLs:

- Companion: `https://<TAILNET_IP>`
- Native Homebox: `https://<TAILNET_IP>:8443`

`0.0.0.0` is a bind address, not a browser URL or certificate identity. The
Tailnet IP stays in the certificate's IP SAN and exact Origin policy. Listening
on another interface does not authorize arbitrary hostnames, IP identities, or
Origins. The existing configured hostnames remain available. Trust the existing
public Caddy CA on each browser device; no trust change happens automatically.

## Preservation and recovery

The access operation preserves all six exact image IDs, persistent volumes,
inventory, accounts, provider settings and keys, the installation UUID, and the
existing Caddy CA. It recreates only the API and proxy. The other four service
container IDs, start times, and environments must remain unchanged. It performs
no image build or pull, model download, inference, data restore, or reset.

The private plan records the listener mode and the exact old configuration
before changes begin. The protected environment stores the selected mode through
two paired bind overrides. Existing defaults leave both blank:

```dotenv
PROXY_BIND_IP=127.0.0.1
PROXY_LAN_IP=<TAILNET_IP>
PROXY_HTTPS_BIND_IP=0.0.0.0
PROXY_LAN_BIND_IP=0.0.0.0
```

The last two settings are both set by the explicit migration. Do not append
duplicates or change only one override. The operation does not use a wildcard
advertised address or relax general runtime guards.

If interrupted, use one of the existing recovery commands:

```sh
./deploy/homebox --yes access resume
./deploy/homebox --yes access rollback
```

Resume uses the recorded mode. Rollback restores the exact prior configuration,
including whether it was loopback-only, exact-address HTTPS, or all-IPv4 HTTPS.
Rollback is configuration-only and never restores an older database. An
unavailable Tailnet address or CLI does not prevent rollback, but ownership,
source, image, volume, key, and binding guards still apply.

A pending operation blocks ordinary maintenance. Restore the matching reviewed
source if a source guard refuses recovery. Do not clear a lock, adopt another
installation, regenerate keys, reset data, or weaken guards to bypass a refusal.
Existing pending plans created by different source do not silently migrate.

Repeating the completed command with `--listen-all` is a no-op. To deliberately
return a completed installation to exact-address HTTPS, run the same access
command without `--listen-all`; the normal guarded configuration transition
applies.

## Validation limits

The focused validation uses stock Compose renders and an owned six-service
fixture that publishes no host ports. It checks the exact intended host
bindings separately from real API/proxy recreation, CA/IP-SAN verified
namespace TLS, authentication, exact Origin policy, and rollback.

Namespace checks do not prove a route from another device, firewall access,
Tailscale ACLs, browser trust, or reachability through an actual public or LAN
interface. Verify those from the intended client after the operator applies
the change.

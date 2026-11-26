# Wi-Fi access alongside Tailscale

Use this mode on an existing managed Linux installation with working Tailnet HTTPS. It adds one physical Wi-Fi or Ethernet IPv4 address. HTTPS listens on that address and the existing Tailnet address, on ports 443 and 8443. Existing localhost access remains.

Wi-Fi clients do not need Tailscale when they can reach the selected LAN address. Remote clients still need Tailscale and access to the Tailnet address. This command does not change router settings, firewalls, network ACLs, or Tailscale. Do not add public Internet port forwarding.

## Enable LAN access

Run these commands from a published checkout that includes `access lan`, on the installation host. Keep the same project and private state directory. If you use global `--project` or `--state-dir` options, retain them before the subcommand in every command below.

1. Check the existing services:

   ```sh
   ./deploy/homebox status
   ```

   The installation must have completed deployment with journal phase `ready` and no pending deployment, access, registration, or model recovery. Six healthy services alone do not prove completion. If a command reports pending access recovery, use the recovery commands below.

2. Before the first LAN activation, deploy the updated application code:

   ```sh
   ./deploy/homebox --yes redeploy
   ```

   Wait for successful completion. The LAN command refuses an installed assistant API that predates LAN support.

3. Replace `LAN_IPV4` below with the host's current LAN IPv4 address. It must be an assigned RFC1918 private address on an UP physical Wi-Fi or Ethernet interface. The command checks local interface, device, driver, and bus evidence. A Tailnet, VPN, Docker bridge, loopback, public, or unassigned address is not valid.

   ```sh
   ./deploy/homebox --yes access lan --ip LAN_IPV4
   ```

   This changes runtime configuration, not application images. Do not edit source, templates, or image settings to insert an IP address. The first activation can take a private backup and restart Native Homebox to update its URL configuration.

If you previously enabled `--listen-all`, LAN activation replaces those wildcard HTTPS listeners with the two selected addresses. Dual LAN/Tailnet mode cannot use `--listen-all`; the combination is refused before configuration changes. Installations that do not enable LAN mode keep their existing behavior.

## Trust the certificate and sign in

Caddy issues the LAN certificate automatically under the installation's existing CA. The command does not install client trust.

On each new client, manually trust only the existing **public root certificate**. The command exports it as `work/public-ca.crt` inside the lifecycle state directory. Transfer that public certificate through a trusted channel and verify that it belongs to this installation. Never transfer private keys or bypass certificate checks.

Replace the placeholders in these URLs:

| Connection | Companion | Native Homebox |
| --- | --- | --- |
| Wi-Fi / LAN | `https://LAN_IPV4/` | `https://LAN_IPV4:8443/` |
| Tailscale | `https://TAILNET_IPV4/` | `https://TAILNET_IPV4:8443/` |

The Tailnet address does not change. Verify both connections from the intended clients; server-side checks do not prove client routing or certificate trust.

Different hosts use separate cookies and PWA installations. Sign in at the address you use; sessions and installed PWAs do not move automatically. New tag links use the active host. Previously printed QR labels do not change. A LAN link needs a LAN route, and a Tailnet link needs a Tailnet route. This mode does not create a universal DNS name.

## Address changes and recovery

For a later LAN IP change, repeat `access lan --ip LAN_IPV4` with the newly assigned address. No application rebuild is needed just to change the IP. An operator-managed DHCP reservation can help keep the address stable.

If access maintenance remains pending, preserve the private environment file, journal, plan, and original `before.env`. Use the recorded operation, without a new IP argument:

```sh
./deploy/homebox --yes access resume
```

To request rollback instead:

```sh
./deploy/homebox --yes access rollback
```

A pending or `rescue-active` result is **not completion**, even if Tailnet access is available. Resolve the reported condition and use the recovery command again. Do not edit or discard recovery state to make the installation appear ready.

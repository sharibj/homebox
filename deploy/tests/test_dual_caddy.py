"""Bounded Docker-only stock-Caddy integration; retains and stops owned containers."""
import argparse
import base64
import copy
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import sys
import time
import uuid


CADDY = "caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0"
PYTHON = "sha256:5e987bfffb514cddf396f1319e73cfea7e79c411a3724c772d37bac8276ece3e"
LAN, TAIL = "10.23.45.67", "100.90.231.7"
OWNER = "homebox-dual-caddy-8889"
ENV = [f"PROXY_LOCAL_LAN_IP={LAN}", f"PROXY_LAN_IP={TAIL}", f"CADDY_DEFAULT_SNI={TAIL}",
       "HOMEBOX_HOST=homebox.localhost", "ASSISTANT_HOST=inventory.localhost", "TLS_ISSUER=internal"]


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


class Engine:
    def __init__(self, root):
        self.root = root
        self.owned = []
        self.runtime_files = {}
        self.prefix = OWNER + "-" + uuid.uuid4().hex[:8]

    def request(self, method, path, value=None, allowed=(200, 201, 204, 304)):
        connection = UnixHTTP("localhost", timeout=45)
        try:
            body = json.dumps(value).encode() if value is not None else None
            connection.request(method, "/v1.45" + path, body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
            if response.status not in allowed:
                raise RuntimeError(f"Owned Docker test request failed: {method} {path}: {response.status}")
            return data
        finally:
            connection.close()

    def create(self, suffix, image, command, *, env=None, network="none", mounts=None):
        assert image in (CADDY, PYTHON)
        assert network == "none" or network.removeprefix("container:") in self.owned
        paths = {
            "/etc/caddy/Caddyfile": "deploy/reverse-proxy/Caddyfile.lan-local",
            "/etc/caddy/Caddyfile.base": "deploy/reverse-proxy/Caddyfile",
            "/etc/caddy/run-dual.sh": "deploy/reverse-proxy/run-dual.sh",
        } if image == CADDY else {"/test.py": "deploy/tests/test_dual_caddy.py"}
        binds = [f"{self.root}/{source}:{target}:ro" for target, source in paths.items()]
        assert mounts is None
        config = {"Image": image, "Cmd": command, "Env": ENV + (env or []),
                  "Labels": {"homebox.test.owner": OWNER},
                  "HostConfig": {"NetworkMode": network, "ReadonlyRootfs": True,
                                 "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                                 "Binds": binds, "Tmpfs": {
                                     "/tmp": "rw,exec,nosuid,nodev,size=32m,mode=1777",
                                     "/data": "rw,nosuid,nodev,size=16m",
                                     "/config": "rw,nosuid,nodev,size=16m"}}}
        data = self.request("POST", "/containers/create?name=" + self.prefix + "-" + suffix, config)
        identity = json.loads(data)["Id"]
        self.owned.append(identity)
        self.request("POST", f"/containers/{identity}/start")
        return identity

    def logs(self, identity):
        assert identity in self.owned
        data = self.request("GET", f"/containers/{identity}/logs?stdout=true&stderr=true")
        return self.streams(data)

    @staticmethod
    def streams(data):
        streams = {1: bytearray(), 2: bytearray()}
        while data:
            stream, length = data[0], struct.unpack(">I", data[4:8])[0]
            streams[stream].extend(data[8:8 + length])
            data = data[8 + length:]
        return tuple(bytes(streams[n]).decode() for n in (1, 2))

    def wait(self, identity):
        assert identity in self.owned
        result = json.loads(self.request("POST", f"/containers/{identity}/wait?condition=not-running"))
        output, errors = self.logs(identity)
        assert result["StatusCode"] == 0, errors + output
        return output

    def public_file(self, identity, path):
        assert identity in self.owned
        assert path == "/data/caddy/pki/authorities/local/root.crt" or path in self.runtime_files.get(identity, set())
        command = {"AttachStdout": True, "AttachStderr": True,
                   "Privileged": False, "Cmd": ["/bin/cat", path]}
        execution = json.loads(self.request("POST", f"/containers/{identity}/exec", command))["Id"]
        data = self.request("POST", f"/exec/{execution}/start", {"Detach": False, "Tty": False})
        output, errors = self.streams(data)
        if errors or not output:
            raise RuntimeError("Owned public test file is not ready")
        return output.encode()

    def stop(self):
        for identity in reversed(self.owned):
            self.request("POST", f"/containers/{identity}/stop?t=5")


BATCH = r'''
set -eu
mkdir -p /tmp/fake
cat > /tmp/fake/caddy <<'SH'
#!/bin/sh
case "$1" in
 adapt)
  [ "$*" = 'adapt --pretty --adapter caddyfile --config /etc/caddy/Caddyfile' ] || exit 91
  printf 'adapt\n' >> /tmp/calls
  if [ "${OVERSIZED:-}" = yes ]; then
   awk 'BEGIN {for (i=0;i<20001;i++) print ""}'
  else
   cat /tmp/input.json
  fi
  ;;
 validate)
  printf 'validate\n' >> /tmp/calls
  exec /usr/bin/caddy "$@"
  ;;
 run)
  printf 'run\n' >> /tmp/calls
  ;;
 *) exit 92 ;;
esac
SH
chmod 700 /tmp/fake/caddy
export PATH=/tmp/fake:$PATH
for n in $CASES; do
 printf 'case: %s\n' "$n"
 printenv "CASE_$n" | base64 -d > /tmp/input.json
 : > /tmp/calls
 if timeout 5 /bin/sh /etc/caddy/run-dual.sh > /tmp/output 2>&1; then status=0; else status=$?; fi
 if [ "$n" = valid ]; then
  if [ "$status" -ne 0 ]; then cat /tmp/output; exit "$status"; fi
  cat /tmp/calls
  cat /tmp/output
  [ "$(wc -l < /tmp/calls)" -eq 3 ]
 else
  [ "$status" -eq 1 ]
  [ "$(wc -l < /tmp/calls)" -eq 1 ]
 fi
done
export OVERSIZED=yes
: > /tmp/calls
if /bin/sh /etc/caddy/run-dual.sh > /tmp/output 2>&1; then exit 93; fi
[ "$(wc -l < /tmp/calls)" -eq 1 ]
unset OVERSIZED
for ip in '' 10.023.45.67 10.23.45 10.23.45.256 10.23.45.067 10.23.45.67.1 \
 100.90.231.7 127.0.0.1 169.254.1.2 172.15.0.1 172.32.0.1 192.169.1.1 \
 '10.23.45.67/24' '10.23.45.67;true' ' 10.23.45.67' true; do
 : > /tmp/calls
 if PROXY_LOCAL_LAN_IP="$ip" /bin/sh /etc/caddy/run-dual.sh > /tmp/output 2>&1; then exit 94; fi
 [ ! -s /tmp/calls ]
done
: > /tmp/calls
if PROXY_LAN_IP="$PROXY_LOCAL_LAN_IP" /bin/sh /etc/caddy/run-dual.sh > /tmp/output 2>&1; then exit 95; fi
[ ! -s /tmp/calls ]
printf 'PASS: adapter shape faults, output bound, invalid addresses, and equal addresses fail before validate/run\n'
'''


def cases(adapted):
    baseline = json.loads(adapted)
    servers = baseline["apps"]["http"]["servers"]
    first = next(name for name, value in servers.items() if value["listen"] == [":14443"])
    result = {"valid": adapted}
    for label in ("missing_server", "duplicate_server", "extra_listener", "missing_policy",
                  "missing_sni", "extra_policy_field", "second_policy", "wrong_port"):
        value = copy.deepcopy(baseline)
        group = value["apps"]["http"]["servers"]
        target = group[first]
        if label == "missing_server":
            del group[first]
        elif label == "duplicate_server":
            group["duplicate"] = copy.deepcopy(target)
        elif label == "extra_listener":
            target["listen"].append(":14444")
        elif label == "missing_policy":
            del target["tls_connection_policies"]
        elif label == "missing_sni":
            del target["tls_connection_policies"][0]["default_sni"]
        elif label == "extra_policy_field":
            target["tls_connection_policies"][0]["alpn"] = ["h2"]
        elif label == "second_policy":
            target["tls_connection_policies"].append({"default_sni": TAIL})
        elif label == "wrong_port":
            target["listen"] = [":14444"]
        result[label] = json.dumps(value, indent="\t")
    old = '\t' * 7 + '"default_sni": "' + TAIL + '"'
    result["duplicate_field"] = adapted.replace(old, old + ",\n" + old, 1)
    result["unexpected_format"] = json.dumps(baseline)
    return result


def tls_client():
    context = ssl.create_default_context(cadata=os.environ["PUBLIC_ROOT_CA"])
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    for port, address in ((443, TAIL), (8443, TAIL), (14443, LAN), (18443, LAN)):
        ipaddress.IPv4Address(address)
        # CPython/OpenSSL omits SNI for an IP server_hostname, but verifies its SAN.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=address) as connection:
                assert ("IP Address", address) in connection.getpeercert()["subjectAltName"]
    print("PASS: CA-chain and IP-SAN verified TLS without IP SNI on all four listeners")


def main():
    if sys.argv[1:] == ["--tls-client"]:
        return tls_client()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-source-root", required=True)
    args = parser.parse_args()
    assert Path(args.host_source_root).is_absolute() and ".." not in Path(args.host_source_root).parts
    engine = Engine(args.host_source_root.rstrip("/"))
    try:
        adapter = engine.create("adapt", CADDY, ["caddy", "adapt", "--pretty", "--adapter",
                                               "caddyfile", "--config", "/etc/caddy/Caddyfile"])
        adapted = engine.wait(adapter)
        fixtures = cases(adapted)
        env = ["CASES=" + " ".join(fixtures)]
        env += ["CASE_" + key + "=" + base64.b64encode(value.encode()).decode()
                for key, value in fixtures.items()]
        batch = engine.create("faults", CADDY, ["/bin/sh", "-c", BATCH], env=env)
        print(engine.wait(batch).strip())
        runtime = engine.create("runtime", CADDY, ["/bin/sh", "/etc/caddy/run-dual.sh"])
        deadline = time.monotonic() + 15
        while True:
            try:
                root = engine.public_file(runtime, "/data/caddy/pki/authorities/local/root.crt")
                _, errors = engine.logs(runtime)
                paths = [json.loads(line)["file"] for line in errors.splitlines()
                         if line.startswith("{") and '"file":"/tmp/homebox-caddy-dual.' in line]
                output = paths[0]
                assert re.fullmatch(r"/tmp/homebox-caddy-dual\.[A-Za-z0-9]+/run\.json", output)
                engine.runtime_files[runtime] = {output, output.replace("/run.json", "/adapted.json")}
                break
            except (RuntimeError, IndexError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        before = json.loads(engine.public_file(runtime, output.replace("/run.json", "/adapted.json")))
        after = json.loads(engine.public_file(runtime, output))
        restored = copy.deepcopy(after)
        changed = []
        for name, server in before["apps"]["http"]["servers"].items():
            if server["listen"] in ([":14443"], [":18443"]):
                assert after["apps"]["http"]["servers"][name]["tls_connection_policies"] == [{"default_sni": LAN}]
                restored["apps"]["http"]["servers"][name]["tls_connection_policies"][0]["default_sni"] = (
                    server["tls_connection_policies"][0]["default_sni"])
                changed.append(name)
        assert len(changed) == 2 and restored == before
        print("PASS: independent JSON oracle reverses exactly two SNI values to identical semantics")
        client = engine.create("tls", PYTHON, ["python", "/test.py", "--tls-client"],
                               env=["PUBLIC_ROOT_CA=" + root.decode()], network="container:" + runtime)
        print(engine.wait(client).strip())
        assert engine.public_file(runtime, "/data/caddy/pki/authorities/local/root.crt") == root
        print(json.dumps({"status": "PASS", "format_cases": len(fixtures), "bad_ip_cases": 17,
                          "tls_listeners": 4, "modified_sni_values": 2,
                          "cached_caddy": CADDY, "owned_containers_retained": len(engine.owned)}))
    finally:
        engine.stop()


if __name__ == "__main__":
    main()

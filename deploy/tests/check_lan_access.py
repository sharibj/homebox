"""Check LAN HTTPS from the proxy namespace without login or inventory writes."""

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl

def lan_ipv4(value):
    ip = ipaddress.ip_address(value)
    if ip.version != 4 or not any(ip in ipaddress.ip_network(net) for net in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    )):
        raise ValueError("LAN_IP must be an RFC1918 or 100.64.0.0/10 IPv4 address")
    return ip


def main():
    ip = lan_ipv4(os.environ["LAN_IP"])
    from cryptography import x509

    address = str(ip)
    origin = "https://" + address
    native_origin = origin + ":8443"
    ca = Path(os.environ["LAN_CA_FILE"])
    context = ssl.create_default_context(cafile=str(ca))

    def request(port, method, path, headers=(), host=None):
        host = host or address + (":" + str(port) if port != 443 else "")
        server = host.split(":")[0]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=server) as secure:
                cert = x509.load_der_x509_certificate(secure.getpeercert(binary_form=True))
                if server == address:
                    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                    assert ip in names.get_values_for_type(x509.IPAddress)
                message = (f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
                           "Connection: close\r\nContent-Length: 0\r\n")
                message += "".join(f"{key}: {value}\r\n" for key, value in headers) + "\r\n"
                secure.sendall(message.encode())
                response = http.client.HTTPResponse(secure, method=method)
                response.begin()
                return response.status, response.read()

    status, html = request(443, "GET", "/")
    assert status == 200
    asset = re.search(rb'src="(/assets/index-[^"]+\.js)"', html).group(1).decode()
    status, javascript = request(443, "GET", asset)
    assert status == 200 and len(javascript) > 10000
    assert request(443, "GET", "/api/assistant/v1/session")[0] == 401
    assert request(8443, "GET", "/")[0] == 200
    assert request(8443, "GET", "/api/v1/status")[0] == 200
    action = "/api/assistant/v1/actions"
    assert request(443, "POST", action, [("Origin", origin)])[0] == 401
    invalid = (
        [], [("Origin", "https://foreign.example.invalid")],
        [("Origin", "https://inventory.localhost")], [("Origin", "null")],
        [("Origin", native_origin)],
        [("Origin", origin), ("Origin", "https://foreign.example.invalid")],
        [("Origin", origin), ("Origin", origin)],
    )
    checks = 0
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"):
        for headers in invalid:
            assert request(443, method, action, headers)[0] == 403, (method, headers)
            checks += 1

    # routes.go registers this POST with userMW; mwAuthToken returns 401 before
    # its handler without a token. Compare against the unchanged desktop alias.
    logout = "/api/v1/users/logout"
    old_native = request(443, "POST", logout, [("Origin", "https://homebox.localhost")],
                         host="homebox.localhost")
    lan_native = request(8443, "POST", logout, [("Origin", native_origin)])
    assert old_native[0] == 401 and lan_native == old_native
    assert request(8443, "POST", logout)[0] == 403
    assert request(8443, "POST", logout, [("Origin", origin)])[0] == 403
    for host in ("inventory.localhost", "homebox.localhost"):
        assert request(443, "GET", "/", host=host)[0] == 200
    assert request(443, "POST", action, [("Origin", "https://inventory.localhost")],
                   host="inventory.localhost")[0] == 401
    assert request(443, "POST", action, [("Origin", "https://foreign.example.invalid")],
                   host="inventory.localhost")[0] == 403
    assert request(443, "POST", action, host="inventory.localhost")[0] == 403
    report = {
        "status": "passed", "phoneURL": origin, "nativeHomeboxURL": native_origin,
        "TLSVerifiedWithExistingPublicCA": True, "IP_SAN": address,
        "publicCA_SHA256": hashlib.sha256(ca.read_bytes()).hexdigest(),
        "javascriptAsset": asset, "javascriptBytes": len(javascript),
        "unauthenticatedAssistantHTTP": 401, "validLANOriginUnauthenticatedHTTP": 401,
        "invalidNonGETOriginCasesRejected": checks,
        "nativeProtectedPOST": logout, "nativeUnauthenticatedHTTP": 401,
        "nativeResponseMatchesOriginalDesktop": True,
        "nativeMissingAndForeignOriginHTTP": 403,
        "desktopAliasesTLSAndLandingHTTP": 200,
        "desktopCanonicalPOSTHTTP": 401, "desktopMissingAndForeignOriginHTTP": 403,
        "productionLogins": 0, "inventoryWrites": 0, "modelCalls": 0,
    }
    if output := os.environ.get("LAN_EVIDENCE_FILE"):
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()

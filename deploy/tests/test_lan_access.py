"""Offline address boundaries for the LAN HTTPS verifier."""

import importlib.util
import ipaddress
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import check_lan_access as verifier


ALLOWED = (
    "10.0.0.0", "10.23.45.67", "10.255.255.255",
    "172.16.0.0", "172.20.1.2", "172.31.255.255",
    "192.168.0.0", "192.168.1.2", "192.168.255.255",
    "100.64.0.0", "100.99.101.89", "100.127.255.255",
)
DENIED = (
    "9.255.255.255", "11.0.0.0", "172.15.255.255", "172.32.0.0",
    "192.167.255.255", "192.169.0.0",
    "100.63.255.255", "100.128.0.0", "100.0.0.0", "100.255.255.255",
    "0.0.0.0", "0.0.0.1", "127.0.0.1", "127.255.255.255",
    "8.8.8.8", "1.1.1.1", "169.254.0.0", "169.254.255.255",
    "224.0.0.0", "239.255.255.255", "240.0.0.0", "255.255.255.255",
    "192.0.0.1", "192.0.2.1", "198.18.0.1",
    "::", "::1", "fc00::1", "fd00::1", "2001:db8::1",
    "::ffff:100.99.101.89",
)
MALFORMED = (
    "", "localhost", "100.99.101.89/32", "https://100.99.101.89",
    "100.064.0.1", "100.128.256.1",
)


class LANAddressTests(unittest.TestCase):
    def test_explicit_allowed_ranges_and_edges(self):
        for address in ALLOWED:
            with self.subTest(address=address):
                result = verifier.lan_ipv4(address)
                self.assertIsInstance(result, ipaddress.IPv4Address)
                self.assertEqual(str(result), address)

    def test_adjacent_and_other_address_classes_are_rejected(self):
        for address in DENIED:
            with self.subTest(address=address), self.assertRaises(ValueError):
                verifier.lan_ipv4(address)

    def test_malformed_addresses_are_rejected(self):
        for address in MALFORMED:
            with self.subTest(address=address), self.assertRaises(ValueError):
                verifier.lan_ipv4(address)

    def test_main_rejects_before_ca_or_network_access(self):
        with patch.object(verifier.ssl, "create_default_context") as tls, \
                patch.object(verifier.socket, "create_connection") as connect:
            for address in DENIED + MALFORMED:
                with self.subTest(address=address), \
                        patch.dict(os.environ, {"LAN_IP": address}, clear=True), \
                        self.assertRaises(ValueError):
                    verifier.main()
            tls.assert_not_called()
            connect.assert_not_called()

    def test_import_needs_no_cryptography_or_network(self):
        path = Path(verifier.__file__)
        spec = importlib.util.spec_from_file_location("isolated_lan_verifier", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"cryptography": None, "cryptography.x509": None}), \
                patch.object(verifier.ssl, "create_default_context") as tls, \
                patch.object(verifier.socket, "create_connection") as connect:
            spec.loader.exec_module(module)
            self.assertEqual(str(module.lan_ipv4("100.99.101.89")), "100.99.101.89")
            tls.assert_not_called()
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()

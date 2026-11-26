"""Offline iproute2 evidence checks for the second, physical LAN identity."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle"))
import local_lan


LAN_IP = "192.168.42.19"
EXPORTER = Path(local_lan.__file__).with_name("local_lan_evidence.sh")


def physical(interface="enp3s0", index=2, ip=LAN_IP):
    flags = ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"]
    link = {"ifindex": index, "ifname": interface, "flags": flags,
            "operstate": "UP", "link_type": "ether"}
    addresses = {"ifindex": index, "ifname": interface, "flags": flags,
                 "addr_info": [{"family": "inet", "local": ip, "prefixlen": 24,
                                "scope": "global", "label": interface,
                                "valid_life_time": 4294967295,
                                "preferred_life_time": 4294967295}]}
    return [link], [addresses]


def hardware(interface="enp3s0", index=2):
    device = "/sys/devices/pci0000:00/0000:00:1f." + str(index)
    return {"interface": device + "/net/" + interface, "device": device,
            "driver": "/sys/bus/pci/drivers/igc", "bus": "/sys/bus/pci"}


def device_row(interface="enp3s0", index=2, paths=None):
    paths = hardware(interface, index) if paths is None else paths
    return "\t".join([interface, str(index)] + [
        paths[key] for key in ("interface", "device", "driver", "bus")]) + "\n"


def expected(interface="enp3s0", index=2, ip=LAN_IP):
    return {"ip": ip, "interface": interface, "ifindex": index,
            "hardware": hardware(interface, index)}


class AddressTests(unittest.TestCase):
    def test_all_three_RFC1918_ranges_and_boundaries(self):
        for value in ("10.0.0.0", "10.12.34.56", "10.255.255.255",
                      "172.16.0.0", "172.20.3.4", "172.31.255.255",
                      "192.168.0.0", LAN_IP, "192.168.255.255"):
            with self.subTest(value=value):
                self.assertEqual(local_lan.address(value), value)

    def test_non_RFC1918_addresses_are_rejected(self):
        for value in ("9.255.255.255", "11.0.0.0", "172.15.255.255",
                      "172.32.0.0", "192.167.255.255", "192.169.0.0",
                      "100.64.0.0", "100.90.1.2", "100.127.255.255",
                      "8.8.8.8", "169.254.0.1", "169.254.255.255",
                      "127.0.0.1", "0.0.0.0", "224.0.0.1",
                      "::1", "fd00::1", "::ffff:192.168.42.19"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_lan.address(value)

    def test_invalid_IPv4_spellings_are_rejected(self):
        for value in ("", "localhost", "192.168.42.256", "192.168.042.19",
                      "192.168.42.19/24", "https://192.168.42.19",
                      "192.168.42.19:443", " 192.168.42.19", "192.168.42.19\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_lan.address(value)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.m = SimpleNamespace(WORK=self.work)
        self.links, self.addresses = physical()
        self.write()

    def write(self, links=None, addresses=None, devices=None):
        links = self.links if links is None else links
        (self.work / "local-lan-links.json").write_text(
            json.dumps(links))
        (self.work / "local-lan-addresses.json").write_text(
            json.dumps(self.addresses if addresses is None else addresses))
        if devices is None:
            devices = "".join(device_row(row["ifname"], row["ifindex"]) for row in links
                              if isinstance(row, dict) and isinstance(row.get("ifname"), str)
                              and type(row.get("ifindex")) is int)
        path = self.work / "local-lan-devices.tsv"
        path.write_text(devices)
        path.chmod(0o600)

    def test_inventory_reads_actual_iproute2_array_shape(self):
        self.assertEqual(local_lan.inventory(self.m), (self.links, self.addresses))
        self.write([], [])
        self.assertEqual(local_lan.inventory(self.m), ([], []))

    def test_inventory_rejects_nonarray_JSON(self):
        for name in ("local-lan-links.json", "local-lan-addresses.json"):
            for body in ("{}", "null", '"untrusted"', "1"):
                self.write()
                (self.work / name).write_text(body)
                with self.subTest(name=name, body=body), self.assertRaises(ValueError):
                    local_lan.inventory(self.m)

    def test_inventory_rejects_invalid_JSON(self):
        for name in ("local-lan-links.json", "local-lan-addresses.json"):
            self.write()
            (self.work / name).write_text("[truncated")
            with self.subTest(name=name), self.assertRaises(ValueError):
                local_lan.inventory(self.m)

    def test_inventory_requires_both_files(self):
        for name in ("local-lan-links.json", "local-lan-addresses.json"):
            self.write()
            (self.work / name).unlink()
            with self.subTest(name=name), self.assertRaises(OSError):
                local_lan.inventory(self.m)

    def test_assigned_matches_exact_IPv4_address(self):
        self.assertTrue(local_lan.assigned(self.m, LAN_IP))
        self.assertFalse(local_lan.assigned(self.m, "192.168.42.20"))
        self.addresses[0]["addr_info"][0]["family"] = "inet6"
        self.write()
        self.assertFalse(local_lan.assigned(self.m, LAN_IP))

    def test_assigned_reports_existing_assignment_even_when_interface_is_down(self):
        self.links[0]["operstate"] = "DOWN"
        self.links[0]["flags"] = ["BROADCAST", "MULTICAST"]
        self.write()
        self.assertTrue(local_lan.assigned(self.m, LAN_IP))
        with self.assertRaises(ValueError):
            local_lan.evidence(self.m, LAN_IP)

    def test_evidence_accepts_UP_Ethernet_and_WiFi(self):
        for interface, index, ip in (("enp3s0", 2, "10.12.34.56"),
                                     ("eth0", 3, "172.20.3.4"),
                                     ("wlp2s0", 4, LAN_IP),
                                     ("wlan0", 5, "192.168.42.20")):
            links, addresses = physical(interface, index, ip)
            self.write(links, addresses)
            with self.subTest(interface=interface):
                self.assertEqual(local_lan.evidence(self.m, ip),
                                 expected(interface, index, ip))

    def test_empty_linkinfo_is_allowed_on_physical_interface(self):
        self.links[0]["linkinfo"] = {}
        self.write()
        self.assertEqual(local_lan.evidence(self.m, LAN_IP)["interface"], "enp3s0")

    def test_other_addresses_and_virtual_interfaces_do_not_select_the_target(self):
        links, addresses = physical("docker0", 3, "172.17.0.1")
        links[0]["linkinfo"] = {"info_kind": "bridge"}
        self.links += links
        self.addresses += addresses
        self.addresses[0]["addr_info"].insert(
            0, {"family": "inet6", "local": "fe80::1", "scope": "link"})
        self.write()
        self.assertEqual(local_lan.evidence(self.m, LAN_IP),
                         expected())

    def test_requested_address_must_be_assigned(self):
        self.write(self.links, [])
        with self.assertRaises(ValueError):
            local_lan.evidence(self.m, LAN_IP)

    def test_address_family_must_be_inet(self):
        self.addresses[0]["addr_info"][0]["family"] = "inet6"
        self.write()
        with self.assertRaises(ValueError):
            local_lan.evidence(self.m, LAN_IP)

    def test_matching_link_requires_both_interface_name_and_index(self):
        for changes in ({"ifindex": 7}, {"ifname": "wlp2s0"}):
            self.links, self.addresses = physical()
            self.links[0].update(changes)
            self.write()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)
        self.write([], self.addresses)
        with self.assertRaises(ValueError):
            local_lan.evidence(self.m, LAN_IP)

    def test_interface_must_have_UP_state_and_UP_flag(self):
        for changes in ({"operstate": "DOWN"}, {"operstate": "UNKNOWN"},
                        {"flags": ["BROADCAST", "MULTICAST", "LOWER_UP"]}):
            self.links, self.addresses = physical()
            self.links[0].update(changes)
            self.write()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_virtual_interface_names_are_rejected_without_linkinfo(self):
        for name in ("lo", "docker0", "br-123abc", "veth123", "virbr0",
                     "tailscale0", "tun0", "tap0", "wg0", "zt123",
                     "dummy0", "cni0", "flannel.1", "vnet0", "ifb0",
                     "vxlan0", "geneve0"):
            self.write(*physical(name))
            with self.subTest(name=name), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_virtual_link_kinds_are_rejected_even_with_physical_looking_name(self):
        for kind in ("bridge", "veth", "tun", "wireguard", "vlan", "bond"):
            self.links, self.addresses = physical()
            self.links[0]["linkinfo"] = {"info_kind": kind}
            self.write()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_non_Ethernet_link_types_are_rejected(self):
        for kind in ("loopback", "none", "ppp"):
            self.links[0]["link_type"] = kind
            self.write()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_non_global_or_tentative_addresses_are_rejected(self):
        for changes in ({"scope": "link"}, {"scope": "host"},
                        {"flags": ["tentative"]}):
            self.links, self.addresses = physical()
            self.addresses[0]["addr_info"][0].update(changes)
            self.write()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_duplicate_address_on_one_or_two_interfaces_is_rejected(self):
        for second_interface in (False, True):
            self.links, self.addresses = physical()
            if second_interface:
                links, addresses = physical("wlp2s0", 3)
                self.links += links
                self.addresses += addresses
            else:
                self.addresses[0]["addr_info"].append(
                    copy.deepcopy(self.addresses[0]["addr_info"][0]))
            self.write()
            with self.subTest(second_interface=second_interface), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_malformed_rows_are_rejected_before_inventory_is_trusted(self):
        for links, addresses in ((["not an interface"], self.addresses),
                                 (self.links, [None]),
                                 (self.links, [{"addr_info": "not an array"}])):
            self.write(links, addresses)
            with self.subTest(links=links, addresses=addresses), self.assertRaises(ValueError):
                local_lan.inventory(self.m)

    def test_malformed_interface_identity_or_flags_cannot_prove_physical_assignment(self):
        for changes in ({"ifindex": None}, {"ifindex": "2"},
                        {"ifname": ""}, {"flags": "UP"}):
            self.links, self.addresses = physical()
            self.links[0].update(changes)
            for key in ("ifindex", "ifname"):
                if key in changes:
                    self.addresses[0][key] = changes[key]
            self.write()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_inventory_and_assigned_do_not_read_missing_hardware_file(self):
        (self.work / "local-lan-devices.tsv").unlink()
        self.assertEqual(local_lan.inventory(self.m), (self.links, self.addresses))
        self.assertTrue(local_lan.assigned(self.m, LAN_IP))
        self.assertFalse(local_lan.assigned(self.m, "192.168.42.20"))
        (self.work / "local-lan-addresses.json").write_text("[]")
        self.assertFalse(local_lan.assigned(self.m, LAN_IP))
        with self.assertRaises(ValueError):
            local_lan.evidence(self.m, LAN_IP)

    def test_physical_looking_interface_requires_nonempty_hardware_evidence(self):
        for interface in ("enp3s0", "wlan0"):
            self.write(*physical(interface), devices="")
            with self.subTest(interface=interface), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_hardware_proof_must_match_interface_name_and_index(self):
        for row in (device_row("wlp2s0", 2), device_row("enp3s0", 3)):
            self.write(devices=row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_hardware_evidence_is_owned_mode_0600_and_not_a_symlink(self):
        path = self.work / "local-lan-devices.tsv"
        for mode in (0o644, 0o640, 0o660, 0o400):
            path.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)
        path.chmod(0o600)
        uid = os.getuid()
        with patch.object(local_lan.os, "getuid", return_value=uid + 1), self.assertRaises(ValueError):
            local_lan.evidence(self.m, LAN_IP)
        target = self.work / "other-devices.tsv"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            local_lan.evidence(self.m, LAN_IP)

    def test_device_evidence_requires_six_fields_and_unique_positive_identity(self):
        valid = device_row()
        for row in ("enp3s0\t2\n", valid.rstrip("\n") + "\textra\n", valid + valid,
                    valid.replace("\t2\t", "\t0\t", 1),
                    valid.replace("\t2\t", "\t02\t", 1),
                    valid.replace("\t2\t", "\t-1\t", 1)):
            self.write(devices=row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_device_paths_require_canonical_ancestry_and_matching_bus_driver(self):
        for changes in (
                {"interface": "/sys/devices/elsewhere/net/wlp2s0"},
                {"device": "/sys/devices/another-device"},
                {"device": "/tmp/outside"},
                {"interface": "/sys/devices/../devices/elsewhere/net/enp3s0"},
                {"interface": hardware()["interface"].replace("/net/", "//net/")},
                {"driver": "/sys/bus/usb/drivers/igc"},
                {"driver": "/sys/bus/pci/drivers/igc/nested"},
                {"bus": "/sys/bus/pci/nested"},
                {"bus": "/outside/pci"},
                {"driver": ""}):
            paths = {**hardware(), **changes}
            self.write(devices=device_row(paths=paths))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)

    def test_kindless_hwsim_and_simulated_device_driver_or_bus_are_rejected(self):
        self.links, self.addresses = physical("wlan0")
        for bus, driver, device in (
                ("platform", "mac80211_hwsim", "/sys/devices/platform/mac80211_hwsim/hwsim0"),
                ("pci", "igc", "/sys/devices/virtual/test-device"),
                ("netdevsim", "netdevsim", "/sys/devices/platform/sim0"),
                ("virtio", "virtio_net", "/sys/devices/pci0000:00/virtio0"),
                ("platform", "igc", "/sys/devices/platform/dummy_hcd.0/usb0")):
            paths = {"interface": device + "/net/wlan0", "device": device,
                     "driver": "/sys/bus/" + bus + "/drivers/" + driver,
                     "bus": "/sys/bus/" + bus}
            self.write(devices=device_row("wlan0", 2, paths))
            with self.subTest(bus=bus, driver=driver), self.assertRaises(ValueError):
                local_lan.evidence(self.m, LAN_IP)


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.sequence = 0

    def fixture(self, bus="pci", driver="igc", device_path=None, interface="enp3s0", index="2"):
        self.sequence += 1
        root = self.work / str(self.sequence) / "sys"
        (root / "class/net").mkdir(parents=True)
        driver_path = root / "bus" / bus / "drivers" / driver
        driver_path.mkdir(parents=True)
        if device_path is None:
            device_path = "devices/pci0000:00/0000:00:1f.2"
        device = root / device_path
        net = device / "net" / interface
        net.mkdir(parents=True)
        (net / "ifindex").write_text(index + "\n")
        (net / "device").symlink_to(device)
        (device / "driver").symlink_to(driver_path)
        (device / "subsystem").symlink_to(root / "bus" / bus)
        class_net = root / "class/net" / interface
        class_net.symlink_to(net)
        return SimpleNamespace(root=root, device=device, net=net, class_net=class_net,
                               driver=driver_path, bus=root / "bus" / bus)

    def export(self, fixture):
        # Always provide the synthetic root. Never inspect the container or host /sys.
        result = subprocess.run(["sh", str(EXPORTER), str(fixture.root)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertNotIn(str(self.work), result.stdout)
        return result.stdout

    def test_real_symlink_chains_on_multiple_hardware_buses_are_exported_and_consumed(self):
        for bus, driver, device, interface in (
                ("pci", "iwlwifi", "devices/pci0000:00/0000:00:1f.2", "wlp2s0"),
                ("usb", "r8152", "devices/pci0000:00/usb1/1-1/1-1:1.0", "enx000000000002"),
                ("sdio", "brcmfmac", "devices/platform/mmc_host/mmc0/mmc0:0001:1", "wlan0"),
                ("platform", "stmmac", "devices/platform/soc/ethernet", "eth0"),
                ("amba", "ethernet", "devices/platform/soc/amba/device0", "eth1")):
            fixture = self.fixture(bus, driver, device, interface)
            output = self.export(fixture)
            paths = {"interface": "/sys/" + device + "/net/" + interface,
                     "device": "/sys/" + device, "driver": "/sys/bus/" + bus + "/drivers/" + driver,
                     "bus": "/sys/bus/" + bus}
            with self.subTest(bus=bus):
                self.assertEqual(output, device_row(interface, 2, paths))
                links, addresses = physical(interface)
                for name, data in (("local-lan-links.json", links),
                                   ("local-lan-addresses.json", addresses)):
                    (self.work / name).write_text(json.dumps(data))
                path = self.work / "local-lan-devices.tsv"
                path.write_text(output)
                path.chmod(0o600)
                self.assertEqual(local_lan.evidence(SimpleNamespace(WORK=self.work), LAN_IP),
                                 {"ip": LAN_IP, "interface": interface, "ifindex": 2, "hardware": paths})

    def test_missing_device_driver_or_subsystem_emits_no_proof(self):
        for missing in ("device", "driver", "subsystem"):
            fixture = self.fixture()
            path = fixture.net / missing if missing == "device" else fixture.device / missing
            path.unlink()
            with self.subTest(missing=missing):
                self.assertEqual(self.export(fixture), "")

    def test_nondirectory_driver_or_bus_emits_no_proof(self):
        for field in ("driver", "subsystem"):
            fixture = self.fixture()
            link = fixture.device / field
            link.unlink()
            target = fixture.root / "not-a-directory"
            target.write_text("not sysfs")
            link.symlink_to(target)
            with self.subTest(field=field):
                self.assertEqual(self.export(fixture), "")

    def test_simulated_hardware_without_link_kind_emits_no_proof(self):
        for bus, driver, device in (
                ("platform", "mac80211_hwsim", "devices/platform/mac80211_hwsim/hwsim0"),
                ("pci", "igc", "devices/virtual/test-device"),
                ("netdevsim", "netdevsim", "devices/platform/sim0"),
                ("virtio", "virtio_net", "devices/pci0000:00/virtio0"),
                ("platform", "igc", "devices/platform/dummy_hcd.0/usb0")):
            fixture = self.fixture(bus, driver, device, "wlan0")
            with self.subTest(bus=bus, driver=driver):
                self.assertEqual(self.export(fixture), "")

    def test_device_must_be_ancestor_of_canonical_network_interface(self):
        fixture = self.fixture()
        elsewhere = fixture.root / "devices/elsewhere"
        elsewhere.mkdir()
        (elsewhere / "driver").symlink_to(fixture.driver)
        (elsewhere / "subsystem").symlink_to(fixture.bus)
        (fixture.net / "device").unlink()
        (fixture.net / "device").symlink_to(elsewhere)
        self.assertEqual(self.export(fixture), "")

    def test_resolved_paths_cannot_escape_synthetic_root(self):
        for field in ("interface", "device", "driver", "subsystem"):
            fixture = self.fixture()
            outside = self.work / ("outside-" + str(self.sequence))
            outside.mkdir()
            if field == "interface":
                link = fixture.class_net
            elif field == "device":
                link = fixture.net / field
            else:
                link = fixture.device / field
            link.unlink()
            link.symlink_to(outside)
            with self.subTest(field=field):
                self.assertEqual(self.export(fixture), "")

    def test_exporter_requires_positive_unambiguous_integer_ifindex(self):
        for index in ("", "0", "-1", "02", "2x", "2\n3"):
            with self.subTest(index=index):
                self.assertEqual(self.export(self.fixture(index=index)), "")

    def test_driver_bus_and_class_name_must_match_canonical_paths(self):
        fixture = self.fixture()
        wrong_bus = fixture.root / "bus/usb"
        wrong_bus.mkdir()
        (fixture.device / "subsystem").unlink()
        (fixture.device / "subsystem").symlink_to(wrong_bus)
        self.assertEqual(self.export(fixture), "")
        fixture = self.fixture()
        renamed = fixture.class_net.with_name("wlan0")
        fixture.class_net.rename(renamed)
        self.assertEqual(self.export(fixture), "")

    def test_environment_cannot_override_explicit_fixture_root(self):
        fixture = self.fixture()
        with patch.dict(os.environ, {"SYSFS_ROOT": "/does-not-exist", "SYS_ROOT": "/does-not-exist"}):
            self.assertEqual(self.export(fixture), device_row())


if __name__ == "__main__":
    unittest.main()

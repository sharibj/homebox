"""Local, physical-interface evidence for the explicit second HTTPS identity."""
import ipaddress
import json
import os
from pathlib import PurePosixPath
import re
import stat

PRIVATE = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
VIRTUAL_NAMES = re.compile(r"^(?:lo$|docker|br-|veth|virbr|tailscale|tun|tap|wg|zt|dummy|cni|flannel|vnet|ifb|vxlan|geneve)")
SIMULATED = re.compile(r"virtual|hwsim|netdevsim|virtio|vmbus|xen|dummy_hcd", re.IGNORECASE)


def _devices(m):
    path = m.WORK / "local-lan-devices.tsv"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("Physical LAN device evidence must be a regular, nonsymlink file.") from None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()):
            raise ValueError("Physical LAN device evidence must be an owned mode-0600 regular file.")
        body = stream.read()
    devices = {}
    for line in body.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            raise ValueError("Physical LAN device evidence requires exactly six tab-separated fields.")
        name, index, interface, device, driver, bus = fields
        if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", name)
                or not re.fullmatch(r"[1-9][0-9]*", index)):
            raise ValueError("Physical LAN device evidence has an invalid interface identity.")
        paths = {"interface": interface, "device": device, "driver": driver, "bus": bus}
        if any(not value.startswith("/sys/") or str(PurePosixPath(value)) != value
               or ".." in PurePosixPath(value).parts
               or any(ord(char) < 32 or ord(char) == 127 for char in value)
               or SIMULATED.search(value) for value in paths.values()):
            raise ValueError("Physical LAN device evidence has a noncanonical or simulated sysfs path.")
        bus_name = bus.removeprefix("/sys/bus/")
        driver_name = driver.removeprefix(bus + "/drivers/")
        if (not interface.startswith("/sys/devices/") or not device.startswith("/sys/devices/")
                or not interface.endswith("/net/" + name)
                or not interface.startswith(device + "/")
                or not bus.startswith("/sys/bus/") or not bus_name or "/" in bus_name
                or not driver.startswith(bus + "/drivers/") or not driver_name or "/" in driver_name):
            raise ValueError("Physical LAN device evidence has inconsistent device, driver or bus ancestry.")
        identity = (name, int(index))
        if identity in devices:
            raise ValueError("Physical LAN device evidence contains duplicate interface identities.")
        devices[identity] = paths
    return devices


def address(value):
    ip = ipaddress.ip_address(value)
    if ip.version != 4 or not any(ip in network for network in PRIVATE):
        raise ValueError("Use one locally assigned RFC1918 LAN IPv4, not a Tailnet, public or loopback address.")
    return str(ip)


def inventory(m):
    links = json.loads((m.WORK / "local-lan-links.json").read_text())
    addresses = json.loads((m.WORK / "local-lan-addresses.json").read_text())
    if not isinstance(links, list) or not isinstance(addresses, list):
        raise ValueError("Local interface evidence must come from iproute2 JSON.")
    for row in links + addresses:
        if (not isinstance(row, dict)
                or type(row.get("ifindex")) is not int or row["ifindex"] <= 0
                or not isinstance(row.get("ifname"), str) or not row["ifname"]
                or not isinstance(row.get("flags"), list)
                or any(not isinstance(flag, str) for flag in row["flags"])):
            raise ValueError("Local interface evidence has an invalid identity or flags.")
    for row in links:
        if not isinstance(row.get("linkinfo", {}), dict):
            raise ValueError("Local interface evidence has invalid link details.")
    for row in addresses:
        entries = row.get("addr_info", [])
        if not isinstance(entries, list):
            raise ValueError("Local interface evidence has invalid address details.")
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("flags", []), list)
                    or any(not isinstance(flag, str) for flag in entry.get("flags", []))):
                raise ValueError("Local interface evidence has an invalid address row.")
    return links, addresses


def assigned(m, value):
    _, addresses = inventory(m)
    return any(entry.get("family") == "inet" and entry.get("local") == value
               for item in addresses for entry in item.get("addr_info", []))


def evidence(m, value):
    value = address(value)
    links, addresses = inventory(m)
    devices = _devices(m)
    matches = []
    for item in addresses:
        for entry in item.get("addr_info", []):
            if entry.get("family") != "inet" or entry.get("local") != value:
                continue
            link = next((row for row in links if row.get("ifindex") == item.get("ifindex")
                         and row.get("ifname") == item.get("ifname")), None)
            if (link is None or link.get("link_type") != "ether"
                    or link.get("linkinfo", {}).get("info_kind")
                    or link.get("operstate") != "UP" or "UP" not in link.get("flags", [])
                    or VIRTUAL_NAMES.match(link.get("ifname", ""))
                    or entry.get("scope") != "global" or entry.get("tentative")
                    or "tentative" in entry.get("flags", [])):
                raise ValueError("The LAN IPv4 must belong to an UP physical Ethernet or Wi-Fi interface, not a virtual network.")
            hardware = devices.get((link["ifname"], link["ifindex"]))
            if hardware is None:
                raise ValueError("The LAN interface requires positive physical device, driver and bus evidence.")
            matches.append({"ip": value, "interface": link["ifname"], "ifindex": link["ifindex"],
                            "hardware": hardware})
    if len(matches) != 1:
        raise ValueError("The requested LAN IPv4 must be uniquely assigned to this host's physical LAN interface.")
    return matches[0]

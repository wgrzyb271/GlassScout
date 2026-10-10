"""Local interfaces and cancellable, full-range TCP discovery through Nmap."""

from __future__ import annotations

import ipaddress
import json
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from threading import Lock
from typing import Callable

from discovery.budget import Budget, LimitReached
from discovery.models import Network


def detect_networks() -> list[Network]:
    import psutil
    primary = ""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, timeout=2)
            match = re.search(r"interface:\s*(\S+)", result.stdout)
            primary = match[1] if match else ""
        elif shutil.which("ip"):
            result = subprocess.run(["ip", "-j", "route", "show", "default"], capture_output=True, text=True, timeout=2)
            primary = next((r.get("dev", "") for r in json.loads(result.stdout)), "")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    stats = psutil.net_if_stats()
    found = []
    for interface, addresses in psutil.net_if_addrs().items():
        if interface in stats and not stats[interface].isup:
            continue
        kind = "virtual / VPN" if re.match(r"(utun|tun|tap|wg|docker|veth|br-|virbr|vmnet|bridge)", interface, re.I) else "LAN"
        for address in addresses:
            if address.family != socket.AF_INET or not address.netmask:
                continue
            ip = ipaddress.IPv4Address(address.address)
            if ip.is_loopback or not ip.is_private:
                continue
            network = ipaddress.IPv4Network(f"{ip}/{address.netmask}", strict=False)
            found.append(Network(interface=interface, address=str(ip), cidr=str(network), kind=kind, default=interface == primary))
    return sorted(found, key=lambda n: (not n.default, n.kind != "LAN", n.interface))


def in_ranges(address: str, ranges: list[str]) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
        return any(ip in ipaddress.IPv4Network(n) for n in ranges)
    except ValueError:
        return False


def parse_host(xml: str, ranges: list[str], *, filter_services: bool = False) -> list[tuple[str, int]]:
    host = ET.fromstring(xml)
    address = host.find("address[@addrtype='ipv4']")
    if address is None or not in_ranges(address.get("addr", ""), ranges):
        return []
    return [(address.get("addr", ""), int(port.get("portid", "0")))
            for port in host.findall("./ports/port")
            if port.get("protocol") == "tcp" and port.find("state") is not None
            and port.find("state").get("state") == "open"
            and (not filter_services or not confirmed_non_web(port))]


def confirmed_non_web(port) -> bool:
    service = port.find("service")
    if service is None or service.get("method") != "probed":
        return False
    # Never exclude a service merely from its conventional port number.
    return service.get("conf", "0").isdigit() and int(service.get("conf", "0")) >= 7 and service.get("name") in {
        "ssh", "microsoft-ds", "netbios-ssn", "ftp", "smtp", "pop3", "imap",
        "mysql", "postgresql", "redis", "vnc", "ms-wbt-server", "jetdirect", "rtsp", "airplay", "raop",
    }


def chunk_ports(spec: str, size: int = 1024) -> list[str]:
    """Split a validated Nmap port expression into resumable ranges."""
    ports: set[int] = set()
    for part in spec.split(","):
        limits = [int(value) for value in part.split("-")]
        ports.update(range(limits[0], limits[-1] + 1))
    ordered = sorted(ports)
    chunks = []
    for offset in range(0, len(ordered), size):
        group = ordered[offset:offset + size]
        ranges = []
        start = previous = group[0]
        for port in group[1:]:
            if port != previous + 1:
                ranges.append(str(start) if start == previous else f"{start}-{previous}")
                start = port
            previous = port
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        chunks.append(",".join(ranges))
    return chunks


def scan_tcp(budget: Budget, found: Callable[[str, int], None], progress: Callable[[str], None], *, ports: str | None = None) -> None:
    """No ICMP filter: every address in the selected range is considered.

    Nmap writes XML incrementally; completed hosts survive cancellation. A run
    deadline is reported as partial, never as proof that the network is empty.
    """
    executable = shutil.which("nmap")
    if not executable:
        raise RuntimeError("Nmap is missing. Install Nmap for subnet scans, or use a test URL.")
    settings = budget.settings
    port_spec = ports or settings.ports
    args = [executable, "-sT", "-Pn", "-n", "--open", "-p", port_spec,
            "--max-retries", "1", "--max-hostgroup", "8", "-T3", "--stats-every", "2s", "-oX", "-", *settings.networks]
    if settings.service_detection:
        args[1:1] = ["-sV", "--version-light"]
    with tempfile.TemporaryDirectory(prefix="glassscout-scan-") as directory:
        output = Path(directory) / "nmap.xml"
        errors = Path(directory) / "nmap.err"
        process = None
        offset = 0
        pending = ""
        hosts = 0
        def consume() -> None:
            nonlocal offset, pending, hosts
            with output.open(encoding="utf-8", errors="replace") as stream:
                stream.seek(offset)
                pending += stream.read()
                offset = stream.tell()
            while True:
                match = re.search(r"<host\b[^>]*>.*?</host>", pending, re.S)
                if not match:
                    break
                for address, port in parse_host(match[0], settings.networks, filter_services=settings.service_detection):
                    found(address, port)
                pending = pending[match.end():]
                hosts += 1
                progress(f"TCP scan: {hosts} hosts completed; ports {port_spec}")
        try:
            with output.open("w") as out, errors.open("w") as err:
                process = subprocess.Popen(args, stdout=out, stderr=err)
                while process.poll() is None:
                    budget.check()
                    consume()
                    budget.stop.wait(0.2)
                consume()
                if process.returncode:
                    raise RuntimeError("Nmap did not complete successfully. Check the Nmap installation and network access.")
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                consume()


def discover_mdns(budget: Budget) -> list[tuple[str, int]]:
    """DNS-SD is a hint source; TCP scanning still handles unadvertised services."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf, ZeroconfServiceTypes
    endpoints: set[tuple[str, int]] = set()
    lock = Lock()
    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            if budget.stop.is_set():
                return
            info = zc.get_service_info(type_, name, timeout=400)
            if info:
                with lock:
                    for ip in info.parsed_addresses():
                        if in_ranges(ip, budget.settings.networks):
                            endpoints.add((ip, info.port))
        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)
        def remove_service(self, zc, type_, name):
            pass
    zc = Zeroconf()
    browsers = []
    try:
        budget.check()
        types = ZeroconfServiceTypes.find(zc=zc, timeout=1)
        for type_ in types[:32]:
            budget.check()
            browsers.append(ServiceBrowser(zc, type_, Listener()))
        budget.stop.wait(1)
        budget.check()
        with lock:
            return sorted(endpoints)
    finally:
        for browser in browsers:
            browser.cancel()
        zc.close()

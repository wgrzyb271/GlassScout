"""Read-only, same-origin HTTP evidence collection with fixed target IPs."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import re
import socket
import ssl
from threading import Timer
from time import monotonic
from html.parser import HTMLParser
from ipaddress import IPv4Address
from urllib.parse import unquote, urljoin, urlsplit

from discovery.budget import LimitReached, ServiceBudget
from discovery.models import Observation, clean_url


def observation_for_model(observation: Observation) -> dict:
    """Return bounded evidence for the model while retaining full local records."""
    data = observation.model_dump(exclude={"instance_id", "digest", "observed_at"})
    data["text"] = data["text"][:900]
    data["links"] = data["links"][:12]
    return data


def redact(text: str) -> str:
    text = re.sub(r"AIza[\w-]{25,}", "[redacted]", text)
    text = re.sub(r"\borg_[A-Za-z0-9]+\b", "[organization]", text)
    text = re.sub(r"(?i)\b(bearer\s+)[\w.\-]+", r"\1[redacted]", text)
    text = re.sub(r'''(?i)(["']?(?:api[_-]?key|token|password|passwd|secret|authorization|cookie|csrf)[\w-]*["']?\s*[:=]\s*)(["'][^"']*["']|[^\s,;}<]+)''', r"\1[redacted]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    return text


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.text: list[str] = []
        self.links: list[str] = []
        self.in_title = False
        self.hidden = 0
        self.redirects: list[str] = []
        self.in_script = False
        self.script_text = ""
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "script":
            self.in_script = True
            self.script_text = ""
        if tag in {"a", "link", "script"}:
            link = attrs.get("href") or attrs.get("src")
            if link:
                self.links.append(link)
        if tag == "meta":
            content = attrs.get("content") or ""
            if (attrs.get("http-equiv") or "").lower() == "refresh":
                match = re.search(r"(?:^|;)\s*url\s*=\s*['\"]?([^'\"]+)", content, re.I)
                if match:
                    self.redirects.append(match[1].strip())
            if (attrs.get("name") or "").lower() in {"application-name", "generator", "description"}:
                self.text.append(content[:500])
        if tag == "form" and (attrs.get("method") or "get").lower() == "get":
            if attrs.get("action"):
                self.links.append(attrs["action"])
        if tag == "img" and attrs.get("alt"):
            self.text.append(attrs["alt"][:200])
    def handle_endtag(self, tag):
        if tag == "script":
            # Literal navigations only; never evaluate fetched JavaScript.
            patterns = (
                r'''(?:window\.|document\.)?location(?:\.href)?\s*=\s*(['"])([^'"\s]+)\1\s*;''',
                r'''(?:window\.|document\.)?location\.(?:replace|assign)\(\s*(['"])([^'"\s]+)\1\s*\)''',
            )
            for pattern in patterns:
                self.redirects.extend(m[2] for m in re.finditer(pattern, self.script_text))
            self.in_script = False
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if self.in_script:
            self.script_text = (self.script_text + data)[:8192]
        if self.in_title:
            self.title += data
        if not self.hidden and data.strip():
            self.text.append(data.strip())


def origin(url: str) -> tuple[str, str | None, int]:
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def allowed_path(url: str) -> bool:
    path = unquote(urlsplit(url).path).lower()
    return not re.search(r"(?:^|[/_.-])(logout|delete|remove|shutdown|reboot|restart|reset|activate|disable|enable|exec|download_backup)(?:[/_.-]|$)", path)


async def resolve_local(url: str) -> tuple[str, str]:
    canonical = clean_url(url)
    host = urlsplit(canonical).hostname
    try:
        addresses = [str(IPv4Address(host))]
    except ValueError:
        records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET), 3)
        addresses = list(dict.fromkeys(r[4][0] for r in records))
    permitted = []
    for address in addresses:
        ip = IPv4Address(address)
        if (ip.is_private or ip.is_loopback) and not ip.is_unspecified and not ip.is_multicast and not ip.is_reserved:
            permitted.append(address)
    if not permitted or len(permitted) != len(addresses):
        raise ValueError("The target must resolve only to local IPv4 addresses.")
    return canonical, permitted[0]


class Probe:
    def __init__(self, endpoint: str, address: str, budget: ServiceBudget):
        self.endpoint, self.address, self.budget = clean_url(endpoint), address, budget
        self.cache: dict[str, Observation] = {}
        self.observations: list[Observation] = []
        self.allowed = {self.endpoint}
        self.redirects: dict[str, list[str]] = {}
        # Protocol-specific read-only probes complement open-ended link discovery.
        self.allowed.update(urljoin(self.endpoint, p) for p in ("/api2/json/version", "/control/status", "/manifest.json", "/api/info"))

    def validate(self, value: str) -> str:
        url = clean_url(value)
        if origin(url) != origin(self.endpoint) or url not in self.allowed or not allowed_path(url):
            raise ValueError("Only observed same-origin URLs and read-only verification paths are allowed.")
        return url

    async def fetch(self, value: str, *, fresh: bool = False) -> Observation:
        url = self.validate(value)
        self.budget.check()
        if not fresh and url in self.cache:
            return self.cache[url]
        self.budget.tool()
        timeout = min(self.budget.run.settings.request_seconds, self.budget.remaining())
        obs = await asyncio.to_thread(self._request, url, timeout)
        self.budget.check()
        self.observations.append(obs)
        self.cache[url] = obs
        return obs

    def _request(self, url: str, timeout: float) -> Observation:
        return self._request_payload(url, timeout)[0]

    def _request_payload(self, url: str, timeout: float, max_bytes: int = 65536):
        """Pinned transport shared with the browser; never forwards cookies."""
        obs = Observation(url=url)
        raw = bytearray()
        headers = {}
        p = urlsplit(url)
        connection = http.client.HTTPConnection(self.address, p.port or (443 if p.scheme == "https" else 80), timeout=timeout)
        deadline = min(self.budget.deadline, monotonic() + timeout)
        transport = None
        def abort():
            for sock in (connection.sock, transport):
                if sock:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        timer = Timer(timeout, abort)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            transport = connection.sock
            if monotonic() >= deadline:
                raise TimeoutError()
            if p.scheme == "https":
                context = ssl.create_default_context()
                if self.budget.run.settings.allow_self_signed:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                connection.sock = context.wrap_socket(connection.sock, server_hostname=p.hostname)
                transport = connection.sock
            connection.request("GET", p.path or "/", headers={"Host": p.netloc, "User-Agent": "GlassScout/1.0", "Accept": "text/html,application/json,text/plain", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            obs.status = response.status
            obs.content_type = response.getheader("content-type", "").split(";")[0].lower()
            headers = {key: response.getheader(key, "") for key in ("content-type", "location")}
            while len(raw) < max_bytes and not getattr(response, "isclosed", lambda: False)():
                self.budget.check()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                if transport:
                    transport.settimeout(remaining)
                chunk = response.read1(min(4096, max_bytes - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            self._extract(obs, bytes(raw), response)
        except LimitReached:
            raise
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            # Exception strings can contain URLs, request headers, and secrets.
            obs.error = type(exc).__name__
        finally:
            timer.cancel()
            connection.close()
        return obs, bytes(raw), headers

    def _extract(self, obs: Observation, raw: bytes, response) -> None:
        body = raw.decode("utf-8", errors="replace")
        obs.digest = hashlib.sha256(raw).hexdigest()
        links = []
        redirects = []
        if 300 <= obs.status < 400:
            redirects.append(response.getheader("location", ""))
        if "html" in obs.content_type:
            page = Page()
            page.feed(body)
            obs.title = redact(page.title)[:160]
            obs.text = redact(" ".join(page.text))[:6000]
            links.extend(page.links)
            redirects.extend(page.redirects)
        elif "json" in obs.content_type:
            try:
                document = json.loads(body)
            except ValueError:
                document = {}
            if isinstance(document, dict):
                # Keep only public software identity fields, not session/config data.
                info = document.get("data", document)
                if not isinstance(info, dict):
                    info = document
                obs.product = redact(str(info.get("product") or info.get("application") or info.get("name") or ""))[:100]
                obs.version = redact(str(info.get("version", "")))[:60]
                obs.instance_id = redact(str(info.get("instance_id", "")))[:100]
                keys = ("product", "application", "name", "version", "release", "repoid", "dns_addresses", "dns_port", "protection_enabled", "running", "language")
                public = {k: info[k] for k in keys if k in info}
                # Redact nested values too before persistence or model input.
                serialized = redact(json.dumps(public, ensure_ascii=False))
                try:
                    obs.facts = json.loads(serialized) if len(serialized) <= 6000 else {}
                except ValueError:
                    obs.facts = {}
                obs.text = serialized[:6000]
        elif any(t in obs.content_type for t in ("text/", "javascript")):
            obs.text = redact(body)[:6000]
        for header in ("server", "www-authenticate", "x-powered-by"):
            value = redact(response.getheader(header, ""))[:160]
            if value:
                obs.text = f"{header.title()}: {value}\n{obs.text}"
        self.redirects[obs.url] = []
        for link in (redirects + links)[:40]:
            try:
                linked = clean_url(urljoin(obs.url, link))
                if origin(linked) == origin(self.endpoint) and allowed_path(linked):
                    self.allowed.add(linked)
                    obs.links.append(linked)
                    if link in redirects:
                        self.redirects[obs.url].append(linked)
            except ValueError:
                continue
        if self.redirects[obs.url]:
            observed = list(dict.fromkeys(self.redirects[obs.url]))
            self.redirects[obs.url] = observed
            obs.facts["redirects"] = observed
            redirect_text = "\n".join(f"Observed redirect: {target}" for target in observed)
            obs.text = f"{obs.text}\n{redirect_text}".strip()[:6000]

"""Validated contracts shared by collectors, the model, and the UI."""

from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import IPv4Network
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_url(value: str) -> str:
    p = urlsplit(value.strip())
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Use an HTTP(S) URL without credentials.")
    if p.query or p.fragment:
        raise ValueError("Use a URL without a query string or fragment.")
    port = p.port  # Validate the port, including malformed values.
    if port == 0:
        raise ValueError("Port zero is not a service port.")
    host = p.hostname.lower()
    if ":" in host:
        raise ValueError("This discovery version supports IPv4 targets only.")
    default = 443 if p.scheme == "https" else 80
    authority = host if port in (None, default) else f"{host}:{port}"
    return urlunsplit((p.scheme, authority, p.path or "/", "", ""))


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Network(Contract):
    interface: str
    address: str
    cidr: str
    kind: str
    default: bool = False


class Settings(Contract):
    networks: list[str] = Field(default_factory=list, max_length=16)
    seed_urls: list[str] = Field(default_factory=list, max_length=512)
    model: str = Field(default="gemini-3.6-flash", min_length=1, max_length=100)
    provider: Literal["gemini", "groq"] = "gemini"
    ports: str = "1-65535"
    run_seconds: int = Field(default=600, ge=30, le=3600)
    service_seconds: int = Field(default=90, ge=5, le=300)
    model_calls: int = Field(default=6, ge=1, le=12)
    tool_calls: int = Field(default=10, ge=4, le=24)
    total_model_calls: int = Field(default=60, ge=1, le=300)
    input_chars: int = Field(default=600_000, ge=1000, le=2_000_000)
    request_seconds: float = Field(default=5, ge=0.2, le=10)
    max_endpoints: int = Field(default=64, ge=1, le=256)
    allow_self_signed: bool = False
    mdns: bool = True
    service_detection: bool = True
    browser_fallback: bool = False
    skip_active_services: bool = True
    seed_urls_are_manual: bool = True
    scan_chunks: list[str] = Field(default_factory=list, max_length=256)
    unlimited_run: bool = False

    @field_validator("seed_urls")
    @classmethod
    def urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(clean_url(v) for v in values))

    @field_validator("networks")
    @classmethod
    def ranges(cls, values: list[str]) -> list[str]:
        ranges = list(dict.fromkeys(str(IPv4Network(v, strict=False)) for v in values))
        if sum(IPv4Network(v).num_addresses for v in ranges) > 4096:
            raise ValueError("At most 4096 IPv4 addresses per run; select a smaller subnet.")
        allowed = [IPv4Network(v) for v in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")]
        if any(not any(IPv4Network(v).subnet_of(a) for a in allowed) for v in ranges):
            raise ValueError("Choose local IPv4 subnets, not a default or public route.")
        return ranges

    @field_validator("ports")
    @classmethod
    def port_range(cls, value: str) -> str:
        import re
        if not re.fullmatch(r"\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*", value):
            raise ValueError("Ports must look like 1-65535 or 80,443,8000-9000.")
        for part in value.split(","):
            limits = [int(n) for n in part.split("-")]
            if not all(1 <= n <= 65535 for n in limits) or limits[0] > limits[-1]:
                raise ValueError("Invalid TCP port range.")
        return value

    @field_validator("scan_chunks")
    @classmethod
    def scan_chunk_ranges(cls, values: list[str]) -> list[str]:
        return [cls.port_range(value) for value in values]

    @model_validator(mode="after")
    def target_required(self) -> Settings:
        if not self.networks and not self.seed_urls:
            raise ValueError("Select a local subnet or enter a test URL.")
        return self


class Observation(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    url: str
    observed_at: str = Field(default_factory=now)
    status: int = 0
    content_type: str = ""
    title: str = ""
    text: str = ""
    links: list[str] = Field(default_factory=list)
    product: str = ""
    version: str = ""
    instance_id: str = ""
    facts: dict = Field(default_factory=dict)
    digest: str = ""
    error: str = ""
    source: Literal["http", "browser"] = "http"


class EvidenceRef(Contract):
    observation_id: str
    quote: str = Field(min_length=2, max_length=240)


class Proposal(Contract):
    name: str = Field(min_length=2, max_length=60)
    category: str = Field(default="DISCOVERED SERVICE", max_length=60)
    description: str = Field(default="Discovered on the local network.", max_length=240)
    url: str
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=6)
    url_check = field_validator("url")(clean_url)


class ReActActionInput(Contract):
    summary: str = Field(min_length=1, max_length=240, description="One short user-facing reason for this action: what it will check, or why to finish. Not private chain-of-thought.")

    @field_validator("summary", mode="before")
    @classmethod
    def trim_summary(cls, value):
        return value.strip() if isinstance(value, str) else value


class FetchServiceInput(ReActActionInput):
    url: str = Field(description="An exact URL from the current allowed_urls list.")
    url_check = field_validator("url")(clean_url)


class FinishServiceInput(ReActActionInput):
    proposal: Proposal | None = Field(default=None, description="Evidence-backed service proposal, or null if unknown. Local code will verify it.")


class Finding(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    endpoint: str
    proposal: Proposal | None = None
    state: Literal["verified", "review", "approved", "rejected"] = "review"
    reason: str
    observations: list[Observation] = Field(default_factory=list)
    fingerprint: str = ""
    service_id: str = ""
    updated_at: str = Field(default_factory=now)


class RunRecord(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    started_at: str = Field(default_factory=now)
    finished_at: str = ""
    status: Literal["running", "completed", "partial", "stopped", "failed"] = "running"
    phase: str = "Starting"
    detail: str = ""
    endpoints: int = 0
    processed: int = 0
    model_calls: int = 0
    input_chars: int = 0
    added: int = 0
    warnings: list[str] = Field(default_factory=list)
    timeout_seconds: float = 0
    remaining_seconds: float | None = None
    scan_timeout_seconds: float = 0
    scan_remaining_seconds: float | None = None
    pending_urls: list[str] = Field(default_factory=list)
    scan_networks: list[str] = Field(default_factory=list)
    scan_ports: str = ""
    pending_port_ranges: list[str] = Field(default_factory=list)
    skipped_active_services: int = 0
    scan_batches_total: int = 0
    scan_batches_completed: int = 0
    scan_detail: str = ""

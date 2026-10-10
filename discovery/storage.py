"""Atomic JSON persistence, review memory, and service upserts under one lock."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import traceback
from urllib.parse import urlsplit
from uuid import uuid4

from discovery.models import Finding, RunRecord, clean_url
from dashboard.persistence import atomic_json, data_lock


def fingerprint(finding: Finding) -> str:
    # Ignore clocks, timestamps, and dynamic response bodies. A changed product
    # identity, path, status, or proposal brings the candidate back for review.
    observations = sorted({(o.url, o.status, o.title, o.product, o.version, o.instance_id, o.error) for o in finding.observations})
    payload = [finding.endpoint, finding.proposal.name if finding.proposal else "", observations]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def normalized_service_url(value: str) -> str:
    return clean_url(value if "://" in value else f"http://{value}")


def is_https_service_url(value: str) -> bool:
    try:
        return urlsplit(normalized_service_url(value)).scheme == "https"
    except ValueError:
        return False


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.directory = data_dir / "discovery"
        self.state_file = self.directory / "state.json"

    def read(self) -> dict:
        if not self.state_file.exists():
            return {"findings": {}, "run": None, "events": []}
        # Fail visibly on corruption instead of discarding review history.
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def clear_history(self) -> None:
        """Clear discovery diagnostics and reviews without touching services."""
        with data_lock(self.data_dir):
            atomic_json(self.state_file, {"findings": {}, "run": None, "events": []})
            for error_file in (self.directory / "errors").glob("*.err"):
                error_file.unlink(missing_ok=True)

    def save_run(self, run: RunRecord) -> None:
        with data_lock(self.data_dir):
            data = self.read()
            data["run"] = run.model_dump()
            atomic_json(self.state_file, data)

    def event(self, kind: str, message: str) -> None:
        from discovery.models import now
        from discovery.probe import redact
        with data_lock(self.data_dir):
            data = self.read()
            data["events"] = (data["events"] + [{"time": now(), "kind": kind, "message": redact(message)[:600]}])[-50:]
            atomic_json(self.state_file, data)

    def error(self, category: str, error: Exception, context: dict[str, str] | None = None) -> Path:
        """Persist a sanitized diagnostic as a JSON-formatted .err file."""
        from discovery.models import now
        from discovery.probe import redact

        created = datetime.now(timezone.utc)
        safe_category = re.sub(r"[^a-z0-9-]+", "-", category.lower()).strip("-") or "error"
        target = self.directory / "errors" / f"{created:%Y%m%dT%H%M%S%fZ}_{safe_category}_{uuid4().hex[:8]}.err"
        safe_context = {str(key): redact(str(value))[:1000] for key, value in (context or {}).items()}
        stack = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        payload = {
            "time": now(),
            "category": safe_category,
            "exception": type(error).__name__,
            "context": safe_context,
            "message": redact(str(error))[:8000],
            "traceback": redact(stack)[-16000:],
        }
        with data_lock(self.data_dir):
            atomic_json(target, payload)
        self.event("error_log", f"Saved diagnostic: {target.name}")
        return target

    def _upsert(self, finding: Finding, *, manual: bool = False) -> bool:
        from dashboard.services import clean_service
        proposal = finding.proposal
        if proposal is None:
            return False
        target = self.data_dir / "services.json"
        if target.exists():
            services = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(services, list):
                raise ValueError("Invalid services file; refusing to replace it.")
        else:
            services = []
        matches = []
        proposal_host = urlsplit(proposal.url).hostname
        proposal_name = " ".join(proposal.name.casefold().split())
        for service in services:
            try:
                existing_url = service["url"]
                normalized = clean_url(existing_url if "://" in existing_url else f"http://{existing_url}")
                same_url = normalized == proposal.url
                same_identity = (
                    urlsplit(normalized).hostname == proposal_host
                    and " ".join(str(service.get("name", "")).casefold().split()) == proposal_name
                )
            except (ValueError, KeyError):
                same_url = same_identity = False
            if same_url or same_identity or (finding.service_id and service.get("id") == finding.service_id):
                matches.append(service)
        match = matches[0] if matches else None
        added = not matches
        if match is None:
            match = clean_service({"id": str(uuid4()), "name": proposal.name, "kind": proposal.category, "description": proposal.description, "url": proposal.url, "icon": "◇", "tone": "mint"})
            services.append(match)
        else:
            # One card per named service and device. Prefer HTTPS whenever the
            # same panel is reachable through both transports or ports.
            existing_https = next((service["url"] for service in matches if is_https_service_url(service["url"])), "")
            match["url"] = proposal.url if urlsplit(proposal.url).scheme == "https" else (existing_https or match["url"])
            if manual:
                match["name"] = proposal.name
            duplicate_ids = {service["id"] for service in matches[1:]}
            if duplicate_ids:
                services = [service for service in services if service["id"] not in duplicate_ids]
        finding.service_id = match["id"]
        atomic_json(target, services)
        return added

    def save_finding(self, finding: Finding) -> tuple[Finding, bool]:
        finding.fingerprint = fingerprint(finding)
        with data_lock(self.data_dir):
            data = self.read()
            previous = data["findings"].get(finding.endpoint)
            if previous:
                old = Finding.model_validate(previous)
                finding.id = old.id
                finding.service_id = old.service_id
                if old.fingerprint == finding.fingerprint and old.state in {"approved", "rejected"}:
                    finding.state, finding.reason = old.state, old.reason
                    finding.proposal = old.proposal
            added = self._upsert(finding) if finding.state == "verified" else False
            data["findings"][finding.endpoint] = finding.model_dump()
            atomic_json(self.state_file, data)
            return finding, added

    def review(self, endpoint: str, *, approve: bool, name: str = "", url: str = "") -> Finding:
        from discovery.models import Proposal, now
        with data_lock(self.data_dir):
            data = self.read()
            finding = Finding.model_validate(data["findings"][endpoint])
            if approve:
                finding.proposal = Proposal(name=name.strip(), url=clean_url(url))
                finding.state = "approved"
                finding.reason = "Approved by user"
                self._upsert(finding, manual=True)
            else:
                finding.state = "rejected"
                finding.reason = "Rejected by user"
            finding.updated_at = now()
            data["findings"][endpoint] = finding.model_dump()
            atomic_json(self.state_file, data)
            return finding

"""Atomic JSON persistence, review memory, and service upserts under one lock."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from discovery.models import Finding, RunRecord, clean_url
from dashboard.persistence import atomic_json, data_lock


def fingerprint(finding: Finding) -> str:
    # Ignore clocks, timestamps, and dynamic response bodies. A changed product
    # identity, path, status, or proposal brings the candidate back for review.
    observations = sorted({(o.url, o.status, o.title, o.product, o.version, o.instance_id, o.error) for o in finding.observations})
    payload = [finding.endpoint, finding.proposal.name if finding.proposal else "", observations]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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
        match = None
        for service in services:
            try:
                existing_url = service["url"]
                same = clean_url(existing_url if "://" in existing_url else f"http://{existing_url}") == proposal.url
            except (ValueError, KeyError):
                same = False
            if same or (finding.service_id and service.get("id") == finding.service_id):
                match = service
                break
        added = match is None
        if match is None:
            match = clean_service({"id": str(uuid4()), "name": proposal.name, "kind": proposal.category, "description": proposal.description, "url": proposal.url, "icon": "◇", "tone": "mint"})
            services.append(match)
        else:
            # Preserve user-authored presentation and only refresh the endpoint.
            match["url"] = proposal.url
            if manual:
                match["name"] = proposal.name
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

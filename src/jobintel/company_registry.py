from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from jobintel.connectors.ashby import AshbyConnector
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.lever import LeverConnector
from jobintel.connectors.base import JobSourceConnector
from jobintel.connectors.smartrecruiters import SmartRecruitersConnector
from jobintel.connectors.welcome_to_the_jungle import WelcomeToTheJungleConnector
from jobintel.connectors.workable import WorkableConnector


@dataclass(frozen=True)
class TargetCompany:
    company_name: str
    industry: str
    priority: int
    greenhouse_board_token: str | None = None
    lever_site_token: str | None = None
    ashby_board_name: str | None = None
    workable_account: str | None = None
    smartrecruiters_company_identifier: str | None = None
    welcome_to_the_jungle_organization_reference: str | None = None
    wttj_organization_reference: str | None = None
    organization_reference: str | None = None
    careers_url: str | None = None
    source_connector_type: str = "pending_verification"
    enabled: bool = False
    notes: str = ""
    verification_status: str = "pending_verification"
    connector_type: str | None = None
    connector_token: str | None = None
    verification_url: str | None = None
    verified_at: str | None = None
    jobs_available: int | None = None
    last_successful_sync: str | None = None
    last_error: str | None = None
    jobs_seen: int | None = None
    jobs_active: int | None = None
    ats_discovery_error: str | None = None
    ats_verification_error: str | None = None
    verification_requested_url: str | None = None
    verification_exception_type: str | None = None
    verification_http_status_code: int | None = None
    verification_failure_kind: str | None = None
    verification_message: str | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TargetCompany:
        known_fields = {item.name for item in fields(cls) if item.name != "extra_metadata"}
        known = {key: value for key, value in payload.items() if key in known_fields}
        extras = {key: value for key, value in payload.items() if key not in known_fields}
        known.setdefault("greenhouse_board_token", None)
        known.setdefault("lever_site_token", None)
        known.setdefault("ashby_board_name", None)
        known.setdefault("workable_account", None)
        known.setdefault("smartrecruiters_company_identifier", None)
        known.setdefault("welcome_to_the_jungle_organization_reference", None)
        known.setdefault("wttj_organization_reference", None)
        known.setdefault("organization_reference", None)
        known.setdefault("careers_url", None)
        known.setdefault("source_connector_type", known.get("connector_type") or "pending_verification")
        known.setdefault("enabled", False)
        known.setdefault("notes", "")
        known.setdefault("verification_status", "pending_verification")
        return cls(**known, extra_metadata=extras)


def load_company_registry(path: Path | str = "config/target_companies.json") -> list[TargetCompany]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [TargetCompany.from_dict(item) for item in payload]


def connectors_from_registry(companies: list[TargetCompany]) -> list[JobSourceConnector]:
    greenhouse_tokens = tuple(
        _connector_token(company, "greenhouse")
        for company in companies
        if company.enabled and _connector_token(company, "greenhouse")
    )
    lever_sites = tuple(
        _connector_token(company, "lever")
        for company in companies
        if company.enabled and _connector_token(company, "lever")
    )
    ashby_boards = tuple(
        _connector_token(company, "ashby")
        for company in companies
        if company.enabled and _connector_token(company, "ashby")
    )
    workable_accounts = tuple(
        _connector_token(company, "workable")
        for company in companies
        if company.enabled and _connector_token(company, "workable")
    )
    smartrecruiters_companies = tuple(
        _connector_token(company, "smartrecruiters")
        for company in companies
        if company.enabled and _connector_token(company, "smartrecruiters")
    )
    wttj_organization_references = tuple(
        _connector_token(company, "welcome_to_the_jungle")
        for company in companies
        if company.enabled and _connector_token(company, "welcome_to_the_jungle")
    )
    connectors: list[JobSourceConnector] = []
    if greenhouse_tokens:
        connectors.append(GreenhouseConnector(greenhouse_tokens))
    if lever_sites:
        connectors.append(LeverConnector(lever_sites))
    if ashby_boards:
        connectors.append(AshbyConnector(ashby_boards))
    if workable_accounts:
        connectors.append(WorkableConnector(workable_accounts))
    if smartrecruiters_companies:
        connectors.append(SmartRecruitersConnector(smartrecruiters_companies))
    if wttj_organization_references:
        import os

        connectors.append(WelcomeToTheJungleConnector(wttj_organization_references, os.getenv("WTTJ_API_KEY")))
    return connectors


def registry_summary(companies: list[TargetCompany]) -> dict[str, int]:
    enabled = [company for company in companies if company.enabled]
    pending = [company for company in companies if company.verification_status != "verified"]
    return {
        "companies_configured": len(companies),
        "enabled_companies": len(enabled),
        "pending_verification_companies": len(pending),
        "greenhouse_connections": len([company for company in enabled if _connector_token(company, "greenhouse")]),
        "lever_connections": len([company for company in enabled if _connector_token(company, "lever")]),
        "ashby_connections": len([company for company in enabled if _connector_token(company, "ashby")]),
        "workable_connections": len([company for company in enabled if _connector_token(company, "workable")]),
        "smartrecruiters_connections": len([company for company in enabled if _connector_token(company, "smartrecruiters")]),
        "welcome_to_the_jungle_connections": len([company for company in enabled if _connector_token(company, "welcome_to_the_jungle")]),
    }


def _connector_token(company: TargetCompany, connector_type: str) -> str | None:
    if company.connector_type == connector_type and company.connector_token:
        return company.connector_token
    if connector_type == "greenhouse":
        return company.greenhouse_board_token
    if connector_type == "lever":
        return company.lever_site_token
    if connector_type == "ashby":
        return company.ashby_board_name
    if connector_type == "workable":
        return company.workable_account
    if connector_type == "smartrecruiters":
        return company.smartrecruiters_company_identifier
    if connector_type == "welcome_to_the_jungle":
        return company.welcome_to_the_jungle_organization_reference or company.wttj_organization_reference or company.organization_reference
    return None

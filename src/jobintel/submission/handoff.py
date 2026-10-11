"""Programmatic handoff layer: Authoritative Answer Bank -> Package -> AirTap Task Instructions.

Maintains the exact four-stage pipeline:
1. Canonical Authoritative Answer Bank (data/local/profile/authoritative_answers.json)
2. Job-specific question / application context
3. Resolved application answers (deterministic facts only; unknown/human-review excluded)
4. ApprovedSubmissionPackage / execution handoff -> AirTap task instructions (airtap_task.md)

Enforces:
- Zero manual copying of Answer Bank values into chat or prompt templates.
- Complete inclusion of safely deterministic candidate facts (address, RTW, education, etc.).
- Strict preservation of semantic distinctions (ordinary RTW vs permanent RTW vs sponsorship).
- Fail-closed handling of unknown/human-review fields (move-in date, UCL award date, Gaokao conversion).
- Full address propagation without '...' placeholders.
- AirTap CV upload limitation protocol (manual supervisor intervention).
- Hard stop before final submit (STOP_BEFORE_FINAL_SUBMIT).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jobintel.models.answer_bank import AnswerRecord, AnswerType, ResolutionMode
from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage, fingerprint
from jobintel.submission.airtap import generate_airtap_task, supervised_review_lines
from jobintel.submission.authoritative_answers import (
    AuthoritativeAnswerBank,
    CANONICAL_VERIFIED_DATE,
)
from jobintel.submission.question_resolution import (
    QuestionResolutionResult,
    resolve_application_question,
    resolve_application_questions,
)


def get_authoritative_answer_bank(root: Path | str | None = None) -> AuthoritativeAnswerBank:
    """Load or instantiate the canonical Authoritative Answer Bank."""
    if root is not None:
        p = Path(root).resolve()
        profile_json = p / "profile" / "authoritative_answers.json"
        if profile_json.exists():
            bank = AuthoritativeAnswerBank(root=p)
            bank.load_from_file(profile_json)
            return bank

    # Default canonical store lookup
    default_json = Path("data/local/profile/authoritative_answers.json").resolve()
    if default_json.exists():
        bank = AuthoritativeAnswerBank(root=default_json.parent.parent)
        bank.load_from_file(default_json)
        return bank

    return AuthoritativeAnswerBank()


def build_authoritative_snapshots(
    bank: AuthoritativeAnswerBank | None = None,
    root: Path | str | None = None,
) -> tuple[AnswerSnapshot, ...]:
    """Convert safely deterministic records from AuthoritativeAnswerBank into package AnswerSnapshots.

    Unknown / HUMAN_REQUIRED fields (e.g. move-in date, UCL completion date,
    Gaokao A-level conversion, complex criminal checks) are strictly EXCLUDED.
    """
    if bank is None:
        bank = get_authoritative_answer_bank(root)

    snapshots: list[AnswerSnapshot] = []

    # Map of standard / friendly field aliases to authoritative field IDs
    field_mappings: list[tuple[str, str]] = [
        # Identity & Contact
        ("name", "identity.legal_full_name"),
        ("legal_first_name", "identity.legal_first_name"),
        ("first_name", "identity.legal_first_name"),
        ("legal_last_name", "identity.legal_last_name"),
        ("last_name", "identity.legal_last_name"),
        ("legal_full_name", "identity.legal_full_name"),
        ("email", "identity.email"),
        ("phone", "identity.phone_uk"),
        ("phone_uk", "identity.phone_uk"),
        ("city", "address.city"),
        ("address_line_1", "address.address_line_1"),
        ("postcode", "address.postcode"),
        ("country", "address.country"),
        ("address", "address.full_address"),
        ("full_address", "address.full_address"),
        ("nationality", "identity.nationality"),
        ("citizenship", "identity.citizenship"),
        ("linkedin_url", "identity.linkedin_url"),
        ("github_url", "identity.github_url"),
        ("website_url", "identity.github_url"),  # a general professional/project URL field takes GitHub
        # Right to work & Immigration
        ("visa_status", "immigration.current_uk_immigration_status"),
        ("current_uk_visa_status", "immigration.current_uk_immigration_status"),
        ("visa_expiry", "immigration.current_visa_expiry"),
        ("current_visa_expiry", "immigration.current_visa_expiry"),
        ("right_to_work_uk", "work_auth.current_legal_right_to_work_uk_answer"),
        ("permitted_to_work_uk", "work_auth.current_legal_right_to_work_uk_answer"),
        ("permanent_or_unrestricted_right_to_work", "work_auth.permanent_or_unrestricted_right_to_work_answer"),
        ("require_sponsorship_now_or_future", "sponsorship.now_or_future_sponsorship_required_answer"),
        ("require_skilled_worker_sponsorship", "sponsorship.skilled_worker_sponsorship_future_answer"),
        # Driving
        ("has_driving_licence", "driving.driving_licence"),
        ("driving_licence_country", "driving.driving_licence_country"),
        ("full_uk_driving_licence", "driving.full_uk_driving_licence_answer"),
        # Preferences
        ("notice_period", "preferences.notice_period"),
        ("relocation", "preferences.relocate_within_uk"),
        ("travel", "preferences.travel_for_work"),
        ("work_mode", "preferences.hybrid_and_onsite"),
        # Education: Huddersfield
        ("undergraduate_institution", "education.huddersfield.institution"),
        ("undergraduate_degree", "education.huddersfield.degree"),
        ("undergraduate_classification", "education.huddersfield.classification"),
        ("undergraduate_start_date", "education.huddersfield.start_date"),
        ("undergraduate_award_date", "education.huddersfield.award_date"),
        # Education: UCL
        ("postgraduate_institution", "education.ucl.institution"),
        ("postgraduate_degree", "education.ucl.degree"),
        ("postgraduate_start_date", "education.ucl.start_date"),
        # Secondary Education
        ("secondary_school", "secondary_education.school_name"),
        ("secondary_school_dates", "secondary_education.dates"),
        ("gaokao_score", "secondary_education.gaokao_score"),
        # Employment History
        ("employment_1_organisation", "employment.tianfeng.organisation"),
        ("employment_1_title", "employment.tianfeng.title"),
        ("employment_1_dates", "employment.tianfeng.dates"),
        ("employment_1_country", "employment.tianfeng.country"),
        ("employment_2_organisation", "employment.howell.organisation"),
        ("employment_2_title", "employment.howell.title"),
        ("employment_2_dates", "employment.howell.dates"),
        ("employment_2_country", "employment.howell.country"),
        # Background Checks & Declarations
        ("criminal_convictions", "criminal.direct_convictions_answer"),
        ("security_clearance_held", "security.current_clearance_answer"),
        ("disability", "disability.disabled_answer"),
        ("reasonable_adjustments", "disability.adjustments_answer"),
        ("diversity_ethnicity", "diversity.default_policy"),
        ("diversity_religion", "diversity.default_policy"),
        ("diversity_sexual_orientation", "diversity.default_policy"),
        ("diversity_gender_identity", "diversity.default_policy"),
    ]

    seen_fields: set[str] = set()

    for friendly_field, canonical_fid in field_mappings:
        rec = bank.get(canonical_fid)
        if rec is None:
            continue
        # Strict requirement: HUMAN_REQUIRED and null values are NOT converted into answers
        if rec.resolution_mode == ResolutionMode.HUMAN_REQUIRED or rec.value is None:
            continue

        val_str = str(rec.value) if not isinstance(rec.value, bool) else ("Yes" if rec.value else "No")
        # Ensure clean formatting
        val_str = val_str.strip()

        ver_fp = fingerprint({"field": friendly_field, "canonical_id": canonical_fid, "value": val_str})
        snapshots.append(
            AnswerSnapshot(
                field=friendly_field,
                value=val_str,
                source=f"authoritative_answer_bank:{canonical_fid}",
                version=ver_fp,
            )
        )
        seen_fields.add(friendly_field)

    # Ensure full address is present (derived if not explicitly in bank)
    full_addr_str = ""
    full_addr_rec = bank.get("address.full_address")
    if full_addr_rec and full_addr_rec.resolution_mode == ResolutionMode.DETERMINISTIC and full_addr_rec.value:
        full_addr_str = str(full_addr_rec.value).strip()
    else:
        addr1 = str(bank.get("address.address_line_1").value) if bank.get("address.address_line_1") and bank.get("address.address_line_1").value else ""
        city = str(bank.get("address.city").value) if bank.get("address.city") and bank.get("address.city").value else ""
        postcode = str(bank.get("address.postcode").value) if bank.get("address.postcode") and bank.get("address.postcode").value else ""
        country = str(bank.get("address.country").value) if bank.get("address.country") and bank.get("address.country").value else ""
        full_addr_str = ", ".join(filter(None, [addr1, city, postcode, country]))

    # Current location: "<city>, <country>" from the two authoritative records (both required).
    city_rec, country_rec = bank.get("address.city"), bank.get("address.country")
    if ("location" not in seen_fields and city_rec and country_rec and city_rec.value and country_rec.value
            and city_rec.resolution_mode == ResolutionMode.DETERMINISTIC
            and country_rec.resolution_mode == ResolutionMode.DETERMINISTIC):
        location = f"{str(city_rec.value).strip()}, {str(country_rec.value).strip()}"
        snapshots.append(AnswerSnapshot(
            field="location", value=location, source="authoritative_answer_bank:address.city+address.country",
            version=fingerprint({"field": "location", "canonical_id": "address.city+address.country", "value": location})))
        seen_fields.add("location")

    if full_addr_str:
        for f in ("address", "full_address"):
            if f not in seen_fields:
                ver_fp = fingerprint({"field": f, "canonical_id": "address.full_address", "value": full_addr_str})
                snapshots.append(
                    AnswerSnapshot(
                        field=f,
                        value=full_addr_str,
                        source="authoritative_answer_bank:address.full_address",
                        version=ver_fp,
                    )
                )
                seen_fields.add(f)

    return tuple(snapshots)


# Resolution rules that map a question onto one canonical answer-bank field
# without naming it in their provenance. The VALUE always comes from the bank.
_RULE_BANK_FIELDS: dict[str, str] = {
    "current_legal_right_to_work_uk_yes": "work_auth.current_legal_right_to_work_uk_answer",
    "permanent_status_do_not_map_from_ordinary_rtw": "work_auth.permanent_or_unrestricted_right_to_work_answer",
    "require_sponsorship_future_yes_truthful": "sponsorship.now_or_future_sponsorship_required_answer",
    "driving_licence_full_uk_no": "driving.full_uk_driving_licence_answer",
    "driving_licence_held_yes": "driving.driving_licence",
    "criminal_convictions_direct_no": "criminal.direct_convictions_answer",
    "security_clearance_held_no": "security.current_clearance_answer",
    "disability_direct_no": "disability.disabled_answer",
    "reasonable_adjustments_no": "disability.adjustments_answer",
    "diversity_prefer_not_to_say": "diversity.default_policy",
    "canonical_address_resolution": "address.full_address",
}
_FIELD_ID = re.compile(r"\b[a-z_]+(?:\.[a-z_]+)+\b")


def _bank_field_for(result: QuestionResolutionResult, stored: dict[str, str]) -> str | None:
    if result.rule_applied in _RULE_BANK_FIELDS:
        return _RULE_BANK_FIELDS[result.rule_applied]
    ids = [fid for fid in _FIELD_ID.findall(result.provenance or "") if fid in stored]
    return ids[0] if len(ids) == 1 else None


def resolve_application_form(
    questions: list[str],
    bank: AuthoritativeAnswerBank | None = None,
    root: Path | str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Resolve an explicit set of application form questions against the Authoritative Answer Bank.

    Resolution rules only decide WHICH canonical bank field a question asks for;
    the answer value always comes from the bank. A question whose field is not
    in the bank (or not deterministic) is unresolved -- never filled from a
    literal embedded in a rule.

    Returns:
    - resolved: dict mapping question string to resolved answer value
    - unresolved: list of question strings that failed closed or require human review
    """
    if bank is None:
        bank = get_authoritative_answer_bank(root)

    resolved: dict[str, str] = {}
    unresolved: list[str] = []

    stored: dict[str, str] = {}
    by_field: dict[str, str] = {}
    if bank is not None:
        for rec in bank.all_records():
            if rec.resolution_mode == ResolutionMode.DETERMINISTIC and rec.value is not None:
                val = str(rec.value) if not isinstance(rec.value, bool) else ("Yes" if rec.value else "No")
                by_field[rec.field_id] = val
                stored[rec.field_id] = val
                stored[rec.field_id.split(".")[-1]] = val
                for alias in rec.aliases:
                    stored[alias] = val
                    stored[" ".join(alias.split()).casefold()] = val

    # The full address is derived only from the bank's own address parts.
    parts = [by_field.get(f) for f in ("address.address_line_1", "address.city", "address.postcode", "address.country")]
    if "address.full_address" not in by_field and all(parts):
        by_field["address.full_address"] = ", ".join(parts)
    if "address.full_address" in by_field:
        full_addr = by_field["address.full_address"]
        for key in ("full address", "current address", "address", "what is your current address?", "what is your address?"):
            stored[key] = full_addr
    if "address.country" in by_field:
        stored["country"] = stored["country of residence"] = by_field["address.country"]

    for q in questions:
        res: QuestionResolutionResult = resolve_application_question(q, stored_answers=stored)
        if not res.resolved or res.requires_human_input or res.answer_value is None:
            unresolved.append(q)
        elif res.from_cache:
            resolved[q] = res.answer_value
        else:
            field = _bank_field_for(res, by_field)
            if field is not None and field in by_field:
                resolved[q] = by_field[field]
            else:
                unresolved.append(q)

    return resolved, unresolved


def generate_airtap_task_markdown(
    package: ApprovedSubmissionPackage,
    bank: AuthoritativeAnswerBank | None = None,
    root: Path | str | None = None,
) -> str:
    """Generate airtap_task.md for an approved package.

    The approved package is the only source of candidate answers after sealing:
    this never re-reads the answer bank or substitutes a literal value. A
    missing required answer raises; optional missing answers are listed as
    UNRESOLVED. (`bank`/`root` are accepted for backward compatibility only.)
    """
    return generate_airtap_task(package, {})

"""Shared stop-only safety and form contract for executors that must stop before final submit.

Backend-independent: the permitted form-field vocabulary, sensitive-question
escalation, exact-answer and page-identity checks, required candidate answers, the
stop mode, evidence/status vocabulary, and strict result validation/import
parameterised by the expected executor. No candidate facts (field identifiers
only), no network, environment, secret, store or browser access.

Transport specifics (AirTap task Markdown, authorization/handoff, bundle manifest,
CV upload mode, event names, CLI) stay in the executor's own module.
"""
from pathlib import Path
import re
from urllib.parse import urlsplit

from jobintel.models.submission import ApprovedSubmissionPackage, ExecutionResult, ReceiptEvidence

MODE = "STOP_BEFORE_FINAL_SUBMIT"
LABELS = {
    # Identity & Contact
    "name": {"name", "full name", "your name"},
    "first_name": {"first name", "legal first name", "given name", "forename"},
    "legal_first_name": {"first name", "legal first name", "given name", "forename"},
    "last_name": {"last name", "legal last name", "family name", "surname"},
    "legal_last_name": {"last name", "legal last name", "family name", "surname"},
    "legal_full_name": {"name", "full name", "legal full name"},
    "email": {"email", "email address", "contact email"},
    "phone": {"phone", "phone number", "telephone", "mobile"},
    "phone_uk": {"phone", "phone number", "telephone", "mobile", "uk phone"},
    "location": {"location", "current location", "where are you currently based", "where are you currently located",
                 "where are you based", "where are you located"},
    "city": {"city", "town"},
    "address_line_1": {"address line 1", "street address"},
    "postcode": {"postcode", "postal code", "zip code"},
    "country": {"country"},
    "address": {"address", "current address", "street address", "full address", "full current address"},
    "full_address": {"address", "current address", "street address", "full address", "full current address"},
    "nationality": {"nationality", "country of nationality"},
    "citizenship": {"citizenship", "country of citizenship"},
    # Profile links: each explicitly named profile maps ONLY to its own URL. A general professional /
    # project URL field (website, portfolio, other URL) takes the GitHub URL; anything else that asks
    # for a link without an exact label here escalates (see PROFILE_LINK_TERMS).
    "linkedin_url": {"linkedin", "linkedin url", "linkedin profile", "linkedin profile url", "linkedin profile link",
                     "linkedin link"},
    "github_url": {"github", "github url", "github profile", "github profile url", "github profile link",
                   "github link"},
    "website_url": {"website", "website url", "personal website", "personal website url", "personal site",
                    "portfolio", "portfolio url", "portfolio link", "portfolio website",
                    "portfolio / website", "website / portfolio", "portfolio/website", "website/portfolio",
                    "portfolio or website", "website or portfolio", "other url", "other website"},
    # Right to Work & Immigration
    "visa_status": {"visa status", "immigration status", "current visa", "current uk visa status"},
    "current_uk_visa_status": {"visa status", "immigration status", "current visa", "current uk visa status"},
    "visa_expiry": {"visa expiry", "visa expiration date"},
    "current_visa_expiry": {"visa expiry", "visa expiration date"},
    "right_to_work_uk": {"right to work in the uk", "legal right to work in the uk", "right to work"},
    "permitted_to_work_uk": {"permitted to work in the uk", "permitted to work"},
    "permanent_or_unrestricted_right_to_work": {
        "permanent or unrestricted right to work in the uk",
        "unrestricted right to work in the uk",
        "permanent right to work",
        "unrestricted right to work",
        "indefinite leave to remain",
        "settled status",
    },
    "require_sponsorship_now_or_future": {
        "will you now or in the future require visa sponsorship",
        "require visa sponsorship",
        "require sponsorship",
        "visa sponsorship required",
    },
    "require_skilled_worker_sponsorship": {
        "will you require skilled worker sponsorship in the future",
        "skilled worker sponsorship",
    },
    # Driving
    "has_driving_licence": {"do you have a driving licence", "driving licence", "driving license"},
    "driving_licence_country": {"driving licence country", "country of driving licence"},
    "full_uk_driving_licence": {"full uk driving licence", "do you hold a full uk driving licence", "valid uk driving licence"},
    # Preferences
    "notice_period": {"notice period", "availability"},
    "relocation": {"willing to relocate", "relocation"},
    "travel": {"willing to travel", "travel preferences"},
    "work_mode": {"work mode", "workplace preference", "hybrid"},
    # Education
    "undergraduate_institution": {"undergraduate university", "bachelors university", "undergraduate institution"},
    "undergraduate_degree": {"undergraduate degree", "beng degree", "bachelors degree"},
    "undergraduate_classification": {"undergraduate classification", "undergraduate grade", "degree grade"},
    "undergraduate_start_date": {"undergraduate start date", "beng start date"},
    "undergraduate_award_date": {"undergraduate graduation date", "beng award date", "graduation date"},
    "postgraduate_institution": {"postgraduate university", "masters university", "postgraduate institution"},
    "postgraduate_degree": {"postgraduate degree", "msc degree", "masters degree"},
    "postgraduate_start_date": {"postgraduate start date", "msc start date"},
    "secondary_school": {"secondary school", "high school"},
    "secondary_school_dates": {"secondary school dates", "high school dates"},
    "gaokao_score": {"gaokao", "gaokao score"},
    # Employment
    "employment_1_organisation": {"recent employer", "previous employer 1"},
    "employment_1_title": {"recent job title", "internship title"},
    "employment_1_dates": {"recent employment dates", "internship dates"},
    "employment_1_country": {"employment 1 country"},
    "employment_2_organisation": {"previous employer 2"},
    "employment_2_title": {"internship 2 title"},
    "employment_2_dates": {"employment 2 dates"},
    "employment_2_country": {"employment 2 country"},
    # Declarations
    "criminal_convictions": {"criminal convictions", "any criminal convictions"},
    "security_clearance_held": {"security clearance held", "currently hold security clearance"},
    "disability": {"disability", "disability status"},
    "reasonable_adjustments": {"reasonable adjustments", "adjustments required"},
    "diversity_ethnicity": {"ethnicity", "ethnic origin"},
    "diversity_religion": {"religion", "religious belief"},
    "diversity_sexual_orientation": {"sexual orientation"},
    "diversity_gender_identity": {"gender identity"},
}
# A label that asks for a link but is not an exact LABELS entry (e.g. "Profile URL", "LinkedIn or
# GitHub", "Social media") is ambiguous: a human decides, nothing is substituted.
PROFILE_LINK_TERMS = ("linkedin", "github", "website", "web site", "portfolio", "url", "profile link",
                      "online profile", "social media", "homepage")
# Substring match on the casefolded label; any hit forces human input.
SENSITIVE = ("sponsor", "right to work", "work permit", "visa", "immigration", "nationality", "citizenship",
             "clearance", "salary", "compensation", "notice period", "relocat", "criminal", "conviction",
             "compliance", "disab", "adjustment", "accommodation", "conflict of interest", "cover letter",
             "motivation", "why do you", "why are you", "free text", "gender", "ethnic", "religio",
             "sexual orientation")
_UK = r"(?:the )?(?:uk|united kingdom)"
_VISA = r"(?:visa |employment visa |work visa |immigration |employer )?"
_WITHOUT_SPONSORSHIP = r"without (?:the need for |requiring |needing )?(?:any )?" + _VISA + r"sponsorship"
# Work-authorisation questions whose wording is unambiguous, and the ONE canonical field each asks
# for. Matched against the whole normalised question (see normalise_question); only these bypass
# the SENSITIVE escalation. Any other wording that mentions sponsorship / right to work / visa
# (e.g. a bare "Do you have the right to work in the UK?", which can mean current or unrestricted
# permission) still goes to a human.
DETERMINISTIC_QUESTIONS = tuple((re.compile(pattern), field) for pattern, field in (
    # Sponsorship now or in the future -> require_sponsorship_now_or_future
    (rf"(?:will|do) you (?:now or in the future |currently or in the future |now or will you in the future )?"
     rf"(?:require|need) {_VISA}sponsorship(?: to work in {_UK})?(?: now or in the future| in the future)?",
     "require_sponsorship_now_or_future"),
    (rf"do you or will you (?:in the future )?(?:require|need) {_VISA}sponsorship(?: to work in {_UK})?",
     "require_sponsorship_now_or_future"),
    (rf"will you require {_VISA}sponsorship (?:now or )?in the future(?: to work in {_UK})?",
     "require_sponsorship_now_or_future"),
    (r"(?:visa )?sponsorship required", "require_sponsorship_now_or_future"),
    # Permanent / unrestricted permission -> permanent_or_unrestricted_right_to_work
    (rf"do you have (?:the |a |an )?(?:permanent|unrestricted|indefinite|unlimited|permanent (?:and|or) unrestricted) "
     rf"right to work in {_UK}", "permanent_or_unrestricted_right_to_work"),
    (rf"do you have (?:the |a )?right to work in {_UK} {_WITHOUT_SPONSORSHIP}", "permanent_or_unrestricted_right_to_work"),
    (rf"are you (?:legally )?(?:able|eligible|permitted|entitled|authori[sz]ed|allowed) to work in {_UK} "
     rf"{_WITHOUT_SPONSORSHIP}", "permanent_or_unrestricted_right_to_work"),
    (rf"(?:do you have|have you got) indefinite leave to remain(?: in {_UK})?", "permanent_or_unrestricted_right_to_work"),
    # Current permission (explicitly "currently") -> right_to_work_uk
    (rf"do you currently have (?:the |a )?(?:legal )?right to work in {_UK}", "right_to_work_uk"),
    (rf"are you currently (?:legally )?(?:eligible|permitted|entitled|authori[sz]ed|allowed) to work in {_UK}",
     "right_to_work_uk"),
))
PAGE_EVIDENCE = {"screen_state", "screenshot_evidence", "application_portal_status"}
# In stop-only mode a confirmation implies an unexpected submission may have happened.
SUBMISSION_EVIDENCE = {"confirmation_id", "confirmation_page", "confirmation_text", "manual_verified"}
EVIDENCE_KINDS = PAGE_EVIDENCE | SUBMISSION_EVIDENCE | {"task_history"}
STATUSES = {"ready_for_human_review", "needs_human_input", "aborted_wrong_job", "already_applied",
            "duplicate_detected", "outcome_unknown", "failed"}
# Whether the executor's own runner checked the start page's identity. Supplied by a caller
# that observed the run itself; without it the report's observed_* fields are compared.
IDENTITY_STATES = {"verified", "mismatch", "not_observed"}
# Agent self-reports that describe no observable page state.
BARE_ASSERTIONS = {"done", "success", "successful", "succeeded", "completed", "complete", "ok", "okay",
                   "finished", "submitted", "task completed", "completed successfully", "all done", "true", "yes"}


def normalise_question(page_label):
    """Casefolded, single-spaced, without required-markers, leading numbering or trailing ?/:/."""
    label = " ".join(page_label.replace("*", " ").split()).casefold()
    label = re.sub(r"^\d{1,2}[.)]\s*", "", label)
    return label.rstrip(" ?:.").strip()


def deterministic_field(page_label):
    """The canonical field of an unambiguous work-authorisation question, else None."""
    label = normalise_question(page_label)
    fields = {field for pattern, field in DETERMINISTIC_QUESTIONS if pattern.fullmatch(label)}
    return fields.pop() if len(fields) == 1 else None


def label_fields(page_label, *, deterministic=False):
    """Canonical fields a page label may be answered from: with `deterministic` (the browser
    runner) the one unambiguous work-authorisation question field, else the exact LABELS vocabulary."""
    field = deterministic_field(page_label) if deterministic else None
    if field is not None:
        return [field]
    label = normalise_question(page_label)
    return [f for f, labels in LABELS.items() if label in labels]


def escalation_reason(page_label, *, deterministic=False):
    """None only for an exact permitted label; otherwise why a human must answer. `deterministic`
    (opt-in, the browser runner only) also permits the DETERMINISTIC_QUESTIONS wordings; every other
    executor keeps escalating anything sensitive."""
    if deterministic and deterministic_field(page_label) is not None:
        return None
    label = " ".join(page_label.split()).casefold()
    hit = next((term for term in SENSITIVE if term in label), None)
    if hit:
        return "sensitive:" + hit
    if not label_fields(page_label, deterministic=deterministic):
        if any(term in label for term in PROFILE_LINK_TERMS):
            return "ambiguous_profile_link"
        return "unknown_field"
    return None


def question(package, field, page_label, page_context, evidence, *, deterministic=False):
    """Only exact, deterministic approved answers may cross the UI boundary."""
    if (escalation_reason(page_label, deterministic=deterministic) is None and field in LABELS
            and field in label_fields(page_label, deterministic=deterministic)):
        answer = next((a for a in package.answers if a.field == field), None)
        if answer is not None and answer.value.strip():
            return {"value": answer.value}
    return {"status": "needs_human_input", "question": page_label, "page_context": page_context,
            "allowed_known_answers": [], "screenshot_or_evidence": evidence}


def check_page(package, *, company, role, application_url, duplicate=False):
    if duplicate:
        return "duplicate_detected"
    norm = lambda v: " ".join(v.split()).casefold()
    if (norm(company) != norm(package.company) or norm(role) != norm(package.role)
            or application_url != package.application_url):
        return "aborted_wrong_job"
    return "matched"


def result_schema(executor):
    """Stop-only result schema; `executor` is the one expected executor name (a const)."""
    text = {"type": "string", "minLength": 1, "maxLength": 20000}
    evidence = {"type": "object", "additionalProperties": False,
                "required": ["kind", "value", "sha256", "detail"], "properties": {
                    "kind": {"enum": sorted(EVIDENCE_KINDS)}, "value": text,
                    "sha256": {"type": "string"}, "detail": {"type": "string"}}}
    escalation = {"type": "object", "additionalProperties": False,
                  "required": ["status", "question", "page_context", "allowed_known_answers", "screenshot_or_evidence"],
                  "properties": {"status": {"const": "needs_human_input"}, "question": text,
                     "page_context": text, "allowed_known_answers": {"type": "array", "maxItems": 0},
                     "screenshot_or_evidence": text}}
    props = {k: text for k in ("request_id", "approval_id", "package_hash", "application_url", "cv_artifact_id",
                               "cv_sha256", "observed_company", "observed_role", "observed_url", "task_id")}
    props.update({"executor": {"const": executor}, "execution_mode": {"const": MODE},
        "status": {"enum": sorted(STATUSES)}, "final_submit_clicked": {"const": False},
        "cv_upload_confirmed": {"type": "boolean"}, "duplicate_detected": {"type": "boolean"},
        "fields_filled": {"type": "array", "maxItems": 100, "items": {
            "type": "object", "additionalProperties": False, "required": ["field", "value", "page_label"],
            "properties": {"field": {"enum": sorted(LABELS)}, "value": text, "page_label": text}}},
        "questions_escalated": {"type": "array", "maxItems": 100, "items": escalation},
        "evidence": {"type": "array", "maxItems": 20, "items": evidence}})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "additionalProperties": False, "required": list(props), "properties": props}


# Candidate answers come ONLY from the approved package (which was resolved from
# the authoritative answer bank before sealing). This module holds no candidate
# facts: a missing answer is never filled from a literal here.
#
# Required for every handoff: each tuple lists acceptable package fields in
# preference order. If none is present, the handoff refuses to proceed.
REQUIRED_HANDOFF_ANSWERS: tuple[tuple[str, ...], ...] = (
    ("legal_first_name", "first_name"),
    ("legal_last_name", "last_name"),
    ("email",),
    ("phone_uk", "phone"),
    ("full_address", "address"),
    ("right_to_work_uk",),
    ("permanent_or_unrestricted_right_to_work",),
    ("require_sponsorship_now_or_future",),
)


def package_answer(answers: dict[str, str], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = answers.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def missing_required_answers(package: ApprovedSubmissionPackage) -> list[str]:
    """Required candidate answers absent from the approved package (fail closed)."""
    answers = {a.field: a.value for a in package.answers}
    return [" / ".join(fields) for fields in REQUIRED_HANDOFF_ANSWERS if package_answer(answers, fields) is None]


def validate_schema(value, schema):
    """Validate the deliberately small JSON Schema vocabulary emitted above."""
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError("Result violates fixed execution policy")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Result contains unsupported value")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise ValueError("Result has missing or unexpected fields")
        for k, v in value.items():
            validate_schema(v, schema["properties"][k])
    elif kind == "array":
        if not isinstance(value, list) or len(value) > schema["maxItems"]:
            raise ValueError("Result array is invalid")
        for v in value:
            validate_schema(v, schema["items"])
    elif kind == "boolean" and type(value) is not bool:
        raise ValueError("Result requires boolean")
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 20000):
            raise ValueError("Result string is invalid")


def _bare(text):
    return re.sub(r"[^a-z ]", "", " ".join(text.split()).casefold()).strip() in BARE_ASSERTIONS


def _observes_page(e, package):
    """Concrete, checkable page observations; agent status text never qualifies."""
    value = e["value"].strip()
    if _bare(value) or _bare(e["detail"]):
        return False
    if e["kind"] == "screenshot_evidence":
        return (re.fullmatch(r"[0-9a-f]{64}", e["sha256"]) is not None and not Path(value).is_absolute()
                and ".." not in Path(value).parts)
    if e["kind"] == "application_portal_status":
        parts = urlsplit(value)
        return (parts.scheme == "https" and parts.hostname == urlsplit(package.application_url).hostname
                and not parts.username and not parts.password)
    return e["kind"] == "screen_state" and len(value) >= 40


def import_result(value, *, package, request_id, approval_id, executor, display_name, identity_state=None,
                  deterministic_questions=False):
    """Normalize a stop-only report; never manufacture a submitted receipt.

    READY_FOR_HUMAN_REVIEW is an execution result, not a submission receipt.

    `identity_state` (verified / mismatch / not_observed) comes from a caller whose runner
    checked the start page itself. "not_observed" means the run stopped before that check:
    the observed_* placeholders are then not compared, so a safe early stop is not turned
    into a wrong-job failure. A mismatch, or any check_page mismatch, still fails closed.
    """
    if not isinstance(executor, str) or not executor.strip():
        raise ValueError("Expected executor is required")
    if identity_state is not None and identity_state not in IDENTITY_STATES:
        raise ValueError("Unknown identity state")
    validate_schema(value, result_schema(executor))
    expected = {"request_id": request_id, "approval_id": approval_id, "package_hash": package.package_hash,
                "application_url": package.application_url, "cv_artifact_id": package.cv_artifact_id,
                "cv_sha256": package.cv_pdf_sha256}
    if any(value[k] != v for k, v in expected.items()):
        raise ValueError(f"{display_name} result identity/hash mismatch")
    for field in value["fields_filled"]:
        # Only a caller whose runner resolves the unambiguous work-authorisation wordings opts in.
        answer = question(package, field["field"], field["page_label"], "result", "result",
                          deterministic=deterministic_questions)
        if answer.get("value") != field["value"]:
            raise ValueError(f"{display_name} filled a non-approved or ambiguous answer")
    status = value["status"]
    identity = identity_state
    if identity == "not_observed":
        # The run stopped before the page was checked: observed_* are placeholders, not a
        # mismatch. Such a run cannot have acted on the page or reached a reviewable final state.
        if value["fields_filled"] or value["cv_upload_confirmed"] or status == "ready_for_human_review":
            raise ValueError(f"{display_name} reported page actions without verified page identity")
        state = "duplicate_detected" if value["duplicate_detected"] else "not_observed"
    else:
        state = check_page(package, company=value["observed_company"], role=value["observed_role"],
                           application_url=value["observed_url"], duplicate=value["duplicate_detected"])
        if identity == "mismatch" and state == "matched":
            state = "aborted_wrong_job"  # a self-reported mismatch always fails closed
    if status in {"ready_for_human_review", "needs_human_input"} and state not in {"matched", "not_observed"}:
        status = state
    if value["questions_escalated"] and status == "ready_for_human_review":
        status = "needs_human_input"
    if status == "ready_for_human_review" and not value["cv_upload_confirmed"]:
        status = "needs_human_input"
    if status == "ready_for_human_review" and not any(_observes_page(e, package) for e in value["evidence"]):
        status = "outcome_unknown"
    if any(e["kind"] in SUBMISSION_EVIDENCE for e in value["evidence"]):
        status = "outcome_unknown"
    unanswered = [q["question"] for q in value["questions_escalated"]]
    if state == "not_observed" and status == "needs_human_input":
        unanswered.append("identity_not_observed: human must confirm this is the approved job page before any action")
    if status in {"already_applied", "duplicate_detected"}:
        # A prior application may exist; a human must check the portal. Never "failed",
        # which would let a fresh approval start another attempt.
        unanswered.append(status + ": human must check the ATS portal before any further attempt")
    if status == "needs_human_input" and not unanswered:
        unanswered.append("cv_upload_unconfirmed" if not value["cv_upload_confirmed"] else "unexpected_page_or_workflow")
    # A self-reported agent failure after operating a live form cannot prove nothing was sent.
    outcome = {"ready_for_human_review": "ready_for_human_review", "aborted_wrong_job": "failed",
               "already_applied": "needs_human_input", "duplicate_detected": "needs_human_input",
               "needs_human_input": "needs_human_input", "failed": "outcome_unknown",
               "outcome_unknown": "outcome_unknown"}[status]
    evidence = tuple(ReceiptEvidence(**e) for e in value["evidence"]
                     if e["kind"] not in {"screen_state", "task_history"} and not _bare(e["value"]))
    return ExecutionResult(request_id, package.package_hash, package.job_id, package.cv_artifact_id,
                           package.application_url, executor, outcome, evidence, tuple(unanswered))

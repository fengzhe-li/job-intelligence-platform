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
import unicodedata
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
    "phone": {"phone", "phone number", "telephone", "mobile", "mobile number"},
    "phone_uk": {"phone", "phone number", "telephone", "mobile", "mobile number", "uk phone"},
    "location": {"location", "current location", "where are you currently based", "where are you currently located",
                 "where are you based", "where are you located", "where do you currently live", "where do you live",
                 # a city-level location picker whose options are "<city>, <region>, <country>" (hosted job boards)
                 "location (city)", "please can you confirm your current location", "please confirm your current location",
                 "can you confirm your current location", "what is your current location"},
    "city": {"city", "town", "current city", "city of residence", "which city are you based in",
             "which city are you currently based in"},
    "address_line_1": {"address line 1", "street address"},
    "postcode": {"postcode", "postal code", "zip code"},
    "country": {"country", "country of residence", "current country", "country of current residence",
                "current country of residence", "country where you currently reside",
                "country you're currently residing in", "country you are currently residing in"},
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
    "employment_1_organisation": {"recent employer", "previous employer 1", "current employer", "current company",
                                  "current or previous employer", "who is your current or previous employer",
                                  "current or most recent employer"},
    "employment_1_title": {"recent job title", "internship title", "current job title", "current title",
                           "current or previous job title", "what is your current or previous job title"},
    "employment_1_dates": {"recent employment dates", "internship dates"},
    "employment_1_country": {"employment 1 country"},
    "employment_2_organisation": {"previous employer 2"},
    "employment_2_title": {"internship 2 title"},
    "employment_2_dates": {"employment 2 dates"},
    "employment_2_country": {"employment 2 country"},
    "employment_3_organisation": {"previous employer 3"},
    "employment_3_title": {"internship 3 title"},
    "employment_3_dates": {"employment 3 dates"},
    "employment_3_country": {"employment 3 country"},
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
# Named-profile link labels beyond the exact LABELS entries: punctuation and case are ignored, a polite
# lead-in ("Link to your", "Your", "Please provide", "URL for", ...) is stripped, and what remains must
# be exactly ONE named profile with an optional "profile" / "url" / "link" suffix. The field is the
# named profile's own URL only; two names ("LinkedIn or GitHub") or none ("Profile URL") never match.
_PROFILE_BRANDS = {"github": "github_url", "linkedin": "linkedin_url"}
_PROFILE_LEAD_IN = re.compile(r"^(?:please (?:provide|enter|share|add|include)|provide|enter|share|add|include"
                              r"|(?:a |the )?(?:link|url) (?:to|for)|your|a|the)\s+")
_PROFILE_REST = re.compile(r"(?P<brand>github|linkedin)(?: profile)?(?: (?:url|link|page|address))?")


def profile_link_field(page_label):
    """"github_url" / "linkedin_url" for a label naming exactly that one profile, else None."""
    text = " ".join(re.sub(r"[^0-9a-z]+", " ", normalise_question(page_label)).split())
    previous = None
    while text != previous:
        previous, text = text, _PROFILE_LEAD_IN.sub("", text, count=1)
    match = _PROFILE_REST.fullmatch(text)
    return _PROFILE_BRANDS[match["brand"]] if match else None


# A label that asks for a link but is not an exact LABELS entry (e.g. "Profile URL", "LinkedIn or
# GitHub", "Social media") is ambiguous: a human decides, nothing is substituted.
PROFILE_LINK_TERMS = ("linkedin", "github", "website", "web site", "portfolio", "url", "profile link",
                      "online profile", "social media", "social profile", "professional profile", "homepage")
# Substring match on the casefolded label; any hit forces human input.
SENSITIVE = ("sponsor", "right to work", "work permit", "visa", "immigration", "nationality", "citizenship",
             "leave to remain", "settled status", "permitted to work", "eligible to work", "eligibility to work",
             "work eligibility", "authorised to work", "authorized to work", "work authoris", "work authoriz",
             "entitled to work", "allowed to work", "legally work",
             "clearance", "salary", "compensation", "notice period", "relocat", "criminal", "conviction",
             "compliance", "disab", "adjustment", "accommodation", "conflict of interest", "cover letter",
             "motivation", "why do you", "why are you", "free text", "gender", "ethnic", "religio",
             "sexual orientation")
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'"})
_UK = r"(?:the )?(?:uk|united kingdom)"
_VISA = r"(?:visa |employment visa |work visa |immigration |employer )?"
_WITHOUT_SPONSORSHIP = r"without (?:the need for |requiring |needing )?(?:any )?" + _VISA + r"sponsorship"
# Work-authorisation questions whose wording is unambiguous, and the ONE canonical field each asks
# for. Matched against the whole normalised question (see normalise_question). Recognition only:
# since 2026-10-13 every work-authorisation question (right to work, permanent/unrestricted right,
# visa status/expiry, sponsorship now/future, basis of eligibility, immigration status) is
# HUMAN-ONLY -- a recognised wording escalates as "human_only:work_authorisation:<field>", any other
# wording via SENSITIVE. No executor ever answers one, whatever the approved package holds.
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
HUMAN_ONLY_WORK_AUTHORISATION = "human_only:work_authorisation"
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


# Required-field markers forms append to labels: ASCII/fullwidth asterisks and the star glyphs some
# hosted forms use (Lever: U+2731 "✱"). Removed wherever they appear; never part of a question.
_REQUIRED_MARKERS = str.maketrans({c: " " for c in "*\uff0a\u2217\u204e\u2731\u2732\u2733\u273d"})


def strip_required_markers(text):
    return (text or "").translate(_REQUIRED_MARKERS)


def normalise_question(page_label):
    """Casefolded, single-spaced, without required-markers, leading numbering or trailing ?/:/."""
    label = " ".join(strip_required_markers(page_label).translate(_APOSTROPHES).split()).casefold()
    label = re.sub(r"^\d{1,2}[.)]\s*", "", label)
    return label.rstrip(" ?:.").strip()


# --- Location-family semantics (the deterministic browser runner only) -----------------------------
# A control whose primary label is exactly a LABELS location / city / country wording asks for one of
# three approved facts. Its OWN helper text (description, aria-describedby, the field's own container;
# never page or section text) may narrow an ambiguous label. Precedence:
#     country-only signal > city-only signal > general current location.
# A helper that contradicts an explicit "country" or "city" label is a conflict: a human decides.
# Controls whose label is not location-family ignore helper text entirely.
LOCATION_FAMILY = ("country", "city", "location")  # precedence order
_RESIDE = r"(?:reside|residing|live|living|based|located)"
_HELPER_SIGNALS = {
    "country": re.compile(r"(?:^country$|\bcurrent country\b|\bcountry of (?:current )?residence\b"
                          rf"|\bcountry (?:where |in which |that )?you(?:'re| are)? (?:currently )?{_RESIDE}\b"
                          rf"|\bwhich country (?:are|do) you (?:currently )?{_RESIDE}\b)"),
    "city": re.compile(r"(?:^(?:city|town)$|\bcurrent city\b|\bcity of (?:current )?residence\b"
                       rf"|\bcity (?:where |in which |that )?you(?:'re| are)? (?:currently )?{_RESIDE}\b"
                       rf"|\bwhich city (?:are|do) you (?:currently )?{_RESIDE}\b)"),
}
# Separates a primary label from the helper text that narrowed it, in a filled field's page_label, so
# the import-time re-check (question / label_fields) sees exactly what the runner resolved.
HELPER_SEPARATOR = " || "
CONFLICTING_LOCATION = "conflicting_location_semantics"


def _location_label_class(label):
    label = normalise_question(label)
    return next((field for field in LOCATION_FAMILY if label in LABELS[field]), None)


def location_field(label, helper=""):
    """For a location-family control: "country", "city" or "location" from its label narrowed by its
    own helper text; CONFLICTING_LOCATION when the helper contradicts an explicit label; None when
    the label is not location-family (helper text is then never consulted)."""
    base = _location_label_class(label)
    if base is None:
        return None
    text = normalise_question(helper or "")
    signals = [field for field in ("country", "city") if text and _HELPER_SIGNALS[field].search(text)]
    if base == "location":
        return signals[0] if signals else "location"
    return base if all(signal == base for signal in signals) else CONFLICTING_LOCATION


def semantic_label(label, helper=""):
    """The label to resolve: the primary label itself unless its own helper text changes (or
    contradicts) the location-family field; then "<label> || <helper>"."""
    helper = " ".join((helper or "").split())
    field = location_field(label, helper)
    if field is None or field == _location_label_class(label):
        return label
    return f"{label}{HELPER_SEPARATOR}{helper}"


def deterministic_field(page_label):
    """The canonical field of an unambiguous work-authorisation question, else None."""
    label = normalise_question(page_label)
    fields = {field for pattern, field in DETERMINISTIC_QUESTIONS if pattern.fullmatch(label)}
    return fields.pop() if len(fields) == 1 else None


def label_fields(page_label, *, deterministic=False):
    """Canonical fields a page label may be answered from: with `deterministic` (the browser
    runner) the one unambiguous work-authorisation question field, else the exact LABELS vocabulary."""
    if deterministic and HELPER_SEPARATOR in page_label:
        field = location_field(*page_label.split(HELPER_SEPARATOR, 1))
        return [field] if field in LOCATION_FAMILY else []
    if deterministic_field(page_label) is not None:
        return []  # work authorisation is human-only
    label = normalise_question(page_label)
    exact = [f for f, labels in LABELS.items() if label in labels]
    profile = profile_link_field(page_label)
    return exact or ([profile] if profile else [])


def escalation_reason(page_label, *, deterministic=False):
    """None only for an exact permitted label; otherwise why a human must answer. Work-authorisation
    questions are always human-only, for every executor."""
    work_authorisation = deterministic_field(page_label)
    if work_authorisation is not None:
        return f"{HUMAN_ONLY_WORK_AUTHORISATION}:{work_authorisation}"
    label = " ".join(page_label.split()).casefold()
    hit = next((term for term in SENSITIVE if term in label), None)
    if hit:
        return "sensitive:" + hit
    if deterministic and HELPER_SEPARATOR in page_label and location_field(
            *page_label.split(HELPER_SEPARATOR, 1)) == CONFLICTING_LOCATION:
        return CONFLICTING_LOCATION
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


def _identity_norm(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def expected_company_names(package):
    """The sealed company names this posting's page may show: package.company (the stored ATS
    slug) plus the package's sealed expected_company_names. A name shorter than 3 characters or
    without a letter is never a meaningful identity and is ignored."""
    names = []
    for name in (getattr(package, "company", ""), *getattr(package, "expected_company_names", ())):
        norm = _identity_norm(name)
        if len(norm) >= 3 and any(ch.isalpha() for ch in norm) and norm not in names:
            names.append(norm)
    return tuple(names)


def company_seen(package, page_text):
    """The sealed expected company name the page text shows as a whole word/phrase, else None.
    Case-insensitive after NFKC + whitespace normalisation; never a prefix/suffix of a longer word
    ("Fanvue" does not match "Fanvuetastic"). The longest matching name wins."""
    text = _identity_norm(page_text)
    hits = [n for n in expected_company_names(package)
            if re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", text)]
    return max(hits, key=len) if hits else None


def check_page(package, *, company, role, application_url, duplicate=False):
    """Matched only when the observed company is exactly one of the sealed expected company names,
    the observed role is exactly the package role and the URL is exactly the application URL."""
    if duplicate:
        return "duplicate_detected"
    if (_identity_norm(company) not in expected_company_names(package)
            or _identity_norm(role) != _identity_norm(package.role)
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

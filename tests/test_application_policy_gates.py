from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import tempfile
import pytest

from jobintel.analysis.eligibility import (
    IneligibleJobError,
    determine_submission_policy,
    evaluate_job_eligibility,
    extract_hard_eligibility_requirements,
)
from jobintel.fixtures.sample_data import sample_candidate
from jobintel.matching.cv_generation import generate_cv
from jobintel.models.candidate import CandidateProfile
from jobintel.models.eligibility import EligibilityState, SubmissionPolicy
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.submission import ApprovedSubmissionPackage
from jobintel.models.taxonomy import ApplicationStatus
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.airtap import MODE
from jobintel.submission.packages import prepare_package
from jobintel.submission.question_resolution import (
    AuthoritativeWorkAuthFacts,
    QuestionResolutionResult,
    resolve_application_question,
    resolve_application_questions,
)
from jobintel.submission.service import SubmissionService

# A synthetic candidate's work-authorisation facts (the real candidate's live in a git-ignored module).
SYNTHETIC_WORK_AUTH = AuthoritativeWorkAuthFacts(
    current_uk_visa_status="Student visa", uk_visa_expiry="2030-01-01", has_current_legal_right_to_work_uk=True,
    student_visa_conditions_apply=True, intends_graduate_route=True,
    may_require_skilled_worker_sponsorship_future=True, has_permanent_or_unrestricted_right_to_work=False)


def _make_job(
    job_id: str = "job-test-1",
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    description: str = "Build Python and PostgreSQL backend services.",
) -> Job:
    from datetime import datetime, timezone
    from jobintel.analysis.job_enrichment import enrich_job
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    job = Job(
        id=job_id,
        title=title,
        company=company,
        description=description,
        locations=[Location("London", "UK", raw="London, UK")],
        source_observations=[
            SourceObservation(
                source_name="greenhouse",
                source_job_id=job_id,
                original_url="https://boards.greenhouse.io/acme/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=description,
                canonical_application_url="https://boards.greenhouse.io/acme/jobs/1",
            )
        ],
    )
    return enrich_job(job, 2026)


class TestApplicationPolicyGates:
    """Regression test suite for the two independent gates:
    1. Eligibility Gate (evaluated before CV generation / package approval)
    2. Career-Stage Submission Policy (protected stages require human review)
    """

    # -------------------------------------------------------------------------
    # CASE 1 — Explicit nationality hard failure
    # -------------------------------------------------------------------------
    def test_case_1_palantir_nationality_hard_failure(self):
        """Role: Palantir Forward Deployed Infrastructure Engineer, New Grad - UK Government
        Requirement: "Sole British National"
        Candidate: authoritative profile does not satisfy sole-British-national requirement
        Expected: HARD_INELIGIBLE, no CV generation, no execution, grounded reason preserved.
        """
        job = _make_job(
            job_id="palantir-fdie-1",
            title="Forward Deployed Infrastructure Engineer, New Grad - UK Government",
            company="Palantir",
            description=(
                "Join our UK Government team as a Forward Deployed Infrastructure Engineer.\n"
                "Requirements: Candidates must be a Sole British National due to government security requirements.\n"
                "Strong coding skills in Python, Go, or Java."
            ),
        )
        candidate = sample_candidate()
        candidate.nationality = "Chinese"
        candidate.citizenship = "Chinese"

        decision = evaluate_job_eligibility(job, candidate)

        assert decision.state == EligibilityState.HARD_INELIGIBLE
        assert decision.is_hard_ineligible is True
        assert decision.reason == "Role explicitly requires sole British nationality."
        assert "Sole British National" in decision.extracted_requirement
        assert "Sole British National" in decision.source_text
        assert "Chinese" in decision.candidate_fact
        assert "sole British nationality" in decision.decision_rule

        # Verify side effect: CV generation is completely blocked
        with tempfile.TemporaryDirectory() as tmp_dir:
            with pytest.raises(IneligibleJobError) as exc_info:
                generate_cv(candidate, job, store_root=tmp_dir)
            assert "HARD_INELIGIBLE" in str(exc_info.value)
            assert "Role explicitly requires sole British nationality." in str(exc_info.value)

    # -------------------------------------------------------------------------
    # CASE 2 — Ambiguous clearance eligibility
    # -------------------------------------------------------------------------
    def test_case_2_ambiguous_clearance_eligibility(self):
        """Requirement: "must be eligible to obtain SC"
        No deterministic authoritative evidence proving eligibility or ineligibility.
        Expected: NEEDS_ELIGIBILITY_REVIEW, NOT HARD_INELIGIBLE, NOT automatically ELIGIBLE.
        """
        job = _make_job(
            job_id="defense-eng-1",
            title="Backend Engineer",
            company="Defense Dynamics",
            description=(
                "Build secure distributed systems.\n"
                "The applicant must be eligible to obtain SC clearance.\n"
                "Experience with Linux and Python."
            ),
        )
        candidate = sample_candidate()
        candidate.security_clearance_eligible = None  # ambiguous / unverified

        decision = evaluate_job_eligibility(job, candidate)

        assert decision.state == EligibilityState.NEEDS_ELIGIBILITY_REVIEW
        assert decision.is_hard_ineligible is False
        assert decision.is_eligible is False
        assert decision.needs_review is True
        assert "requires manual eligibility review" in decision.reason
        assert "must be eligible to obtain SC" in decision.source_text

    # -------------------------------------------------------------------------
    # CASE 3 — Graduate role, otherwise eligible
    # -------------------------------------------------------------------------
    def test_case_3_graduate_role_requires_human_review(self):
        """Graduate role, otherwise eligible -> REQUIRE_HUMAN_REVIEW."""
        job = _make_job(
            job_id="grad-eng-1",
            title="Graduate Software Engineer",
            company="Bloomberg",
            description="Our Graduate Programme welcomes candidates completing their degree.",
        )
        candidate = sample_candidate()

        eligibility = evaluate_job_eligibility(job, candidate)
        assert eligibility.state == EligibilityState.ELIGIBLE

        policy = determine_submission_policy(job)
        assert policy.policy == SubmissionPolicy.REQUIRE_HUMAN_REVIEW
        assert policy.is_protected_career_stage is True

    # -------------------------------------------------------------------------
    # CASE 4 — New Grad role, otherwise eligible
    # -------------------------------------------------------------------------
    def test_case_4_new_grad_role_requires_human_review(self):
        """New Grad role, otherwise eligible -> REQUIRE_HUMAN_REVIEW."""
        job = _make_job(
            job_id="new-grad-1",
            title="New Grad Software Engineer",
            company="Amazon",
            description="Join our team as a New Grad Software Development Engineer.",
        )
        candidate = sample_candidate()

        eligibility = evaluate_job_eligibility(job, candidate)
        assert eligibility.state == EligibilityState.ELIGIBLE

        policy = determine_submission_policy(job)
        assert policy.policy == SubmissionPolicy.REQUIRE_HUMAN_REVIEW
        assert policy.is_protected_career_stage is True

    # -------------------------------------------------------------------------
    # CASE 5 — Early Career role, otherwise eligible
    # -------------------------------------------------------------------------
    def test_case_5_early_career_role_requires_human_review(self):
        """Early Career role, otherwise eligible -> REQUIRE_HUMAN_REVIEW."""
        job = _make_job(
            job_id="early-career-1",
            title="Early Career Developer",
            company="Cisco",
            description="Our Early Careers programme offers mentoring and software development projects.",
        )
        candidate = sample_candidate()

        eligibility = evaluate_job_eligibility(job, candidate)
        assert eligibility.state == EligibilityState.ELIGIBLE

        policy = determine_submission_policy(job)
        assert policy.policy == SubmissionPolicy.REQUIRE_HUMAN_REVIEW
        assert policy.is_protected_career_stage is True

    # -------------------------------------------------------------------------
    # CASE 6 — Ordinary experienced / junior role, eligible
    # -------------------------------------------------------------------------
    def test_case_6_ordinary_role_auto_application_allowed_with_stop_guard(self):
        """Ordinary experienced/junior role, eligible -> AUTO_APPLICATION_ALLOWED,
        but current executor still enforces STOP_BEFORE_FINAL_SUBMIT.
        """
        job = _make_job(
            job_id="junior-dev-1",
            title="Junior Software Engineer",
            company="GSF Car Parts",
            description="Build internal web tools using Python, JavaScript, and SQL.",
        )
        candidate = sample_candidate()

        eligibility = evaluate_job_eligibility(job, candidate)
        assert eligibility.state == EligibilityState.ELIGIBLE

        policy = determine_submission_policy(job)
        assert policy.policy == SubmissionPolicy.AUTO_APPLICATION_ALLOWED
        assert policy.is_protected_career_stage is False

        # Current executor runtime guard MUST remain strictly active
        assert MODE == "STOP_BEFORE_FINAL_SUBMIT"

    # -------------------------------------------------------------------------
    # CASE 7 — Unknown eligibility (fail closed)
    # -------------------------------------------------------------------------
    def test_case_7_unknown_eligibility_fails_closed(self):
        """Unknown eligibility -> fail closed, no autonomous execution."""
        job = _make_job(
            job_id="gov-role-1",
            title="Platform Engineer",
            company="Civil Service",
            description="Role requires UK citizenship required for all candidates.",
        )
        candidate = sample_candidate()
        candidate.nationality = None
        candidate.citizenship = None

        decision = evaluate_job_eligibility(job, candidate)

        assert decision.state == EligibilityState.UNKNOWN
        assert decision.needs_review is True
        assert decision.is_eligible is False
        assert decision.is_hard_ineligible is False
        assert "unrecorded" in decision.reason

    # -------------------------------------------------------------------------
    # CASE 8 — No false generalization
    # -------------------------------------------------------------------------
    def test_case_8_no_false_generalization_on_clearance(self):
        """A role mentions security clearance but does NOT state British citizenship
        or sole British nationality.
        Expected: must NOT become HARD_INELIGIBLE merely because the candidate is not British.
        """
        job = _make_job(
            job_id="cleared-role-1",
            title="Software Engineer",
            company="Tech Consulting Ltd",
            description=(
                "Develop cloud microservices in Python.\n"
                "The successful candidate will undergo security clearance vetting.\n"
                "Experience with Docker and Kubernetes."
            ),
        )
        candidate = sample_candidate()
        candidate.nationality = "Chinese"
        candidate.citizenship = "Chinese"
        candidate.security_clearance = None
        candidate.security_clearance_eligible = None

        decision = evaluate_job_eligibility(job, candidate)

        # CRITICAL: Must NOT be HARD_INELIGIBLE
        assert decision.state != EligibilityState.HARD_INELIGIBLE
        # Must fail-closed to manual review because clearance eligibility is unverified
        assert decision.state == EligibilityState.NEEDS_ELIGIBILITY_REVIEW
        assert decision.needs_review is True

    # -------------------------------------------------------------------------
    # SIDE-EFFECT BOUNDARIES
    # -------------------------------------------------------------------------
    def test_side_effect_boundary_hard_ineligible_blocks_package_and_enqueue(self):
        """Verify that HARD_INELIGIBLE blocks:
        1. Package preparation
        2. Enqueuing for execution
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            job = _make_job(
                job_id="job-hard-fail",
                title="Systems Engineer",
                company="Restricted Gov",
                description="Candidates must be a Sole British National. Python required.",
            )
            candidate = sample_candidate()
            candidate.nationality = "Chinese"
            candidate.citizenship = "Chinese"
            candidate.email = "test@example.com"
            candidate.phone = "07000000000"

            # Set up local store
            LocalJobStore(root).write_jobs([job])
            app_store = ApplicationStore(root)
            from jobintel.models.application import Application
            app = app_store.create_application(
                Application("app:job-hard-fail", job.id, job.company, job.title, application_url="https://boards.greenhouse.io/gov/jobs/1")
            )

            # 1. Package preparation must raise ValueError on HARD_INELIGIBLE
            # Even if a CV artifact somehow existed:
            from jobintel.models.cv_artifact import GeneratedCVArtifact
            from datetime import datetime, timezone
            from jobintel.matching.cv_generation import resolve_cv_pdf
            import hashlib

            artifact_id = "cv-0123456789ab"
            pdf_path = root / "cv_artifacts/pdfs" / f"{artifact_id}.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(b"%PDF-1.4 dummy")
            sha = hashlib.sha256(b"%PDF-1.4 dummy").hexdigest()

            from jobintel.models.taxonomy import ReviewGateStatus
            dummy_art = GeneratedCVArtifact(
                id=artifact_id,
                job_id=job.id,
                candidate_id=candidate.id,
                generated_at=datetime.now(timezone.utc),
                jd_snapshot=job.description,
                selected_project_ids=[],
                evidence_ids_used=[],
                bullets=[],
                skills_included=[],
                cv_text="CV Text",
                pdf_path=str(pdf_path),
                pdf_sha256=sha,
                page_count=1,
                fits_one_page=True,
                render_margin_mm=18.0,
                render_body_pt=10.0,
                review_status=ReviewGateStatus.BLOCK_AUTO_SUBMISSION,
            )
            CVArtifactStore(root).save(dummy_art)
            app_store.attach_cv(app.id, artifact_id, [], [])

            # Persist candidate profile
            from jobintel.profile_ingestion import _write_profile
            _write_profile(root, candidate)

            cfg_path = root / "strategy.json"
            cfg_path.write_text("{}")

            # Attempt prepare_package
            with pytest.raises(ValueError) as exc:
                prepare_package(root, app.id, cfg_path)
            assert "HARD_INELIGIBLE" in str(exc.value)

            # 2. Enqueue must reject any package marked hard_ineligible
            service = SubmissionService(root=root, config_path=cfg_path)
            fake_package = ApprovedSubmissionPackage(
                application_id=app.id,
                job_id=job.id,
                application_url="https://boards.greenhouse.io/gov/jobs/1",
                cv_artifact_id=artifact_id,
                cv_pdf_sha256=sha,
                cv_path=str(pdf_path),
                answers=(),
                input_hash="fakehash",
                gate_status="block_auto_submission",
                gate_reasons=("Role explicitly requires sole British nationality.",),
                eligibility_state="hard_ineligible",
            )
            # Record an approval in the ledger with hard_ineligible package
            aid = "approval:test"
            service.store.append(
                "approved",
                aid,
                {
                    "package": asdict(fake_package),
                    "package_hash": fake_package.package_hash,
                    "reviewer": "tester",
                    "human_reviewed": True,
                    "acknowledged_reasons": list(fake_package.gate_reasons),
                    "approved_at": service.clock().isoformat(),
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
                event_id=aid,
            )

            # Attempt to enqueue must fail (either invalidated by preview or rejected directly)
            with pytest.raises(ValueError) as exc:
                service.enqueue(aid)
            assert "Approval invalidated" in str(exc.value) or "HARD_INELIGIBLE" in str(exc.value) or "blocked" in str(exc.value)

    # -------------------------------------------------------------------------
    # CASE 9 — Corrected Sponsorship Regression: "No sponsorship" is NOT HARD_INELIGIBLE
    # -------------------------------------------------------------------------
    def test_case_9_candidate_requires_sponsorship_with_no_sponsorship_jd_is_eligible(self):
        """Update the previous incorrect regression:
        Candidate requires sponsorship + 'No sponsorship available' in JD
        must NOT produce HARD_INELIGIBLE.
        CV generation and application preparation remain allowed.
        """
        job = _make_job(
            job_id="fintech-no-sponsor-1",
            title="Software Developer",
            company="FinTech Ltd",
            description=(
                "Build payment integrations with Python and FastAPI.\n"
                "Applicants must have the right to work in the UK without sponsorship. "
                "No visa sponsorship is available for this role.\n"
                "Requirements: 2+ years of Python experience."
            ),
        )
        candidate = sample_candidate()
        candidate.nationality = "Chinese"
        candidate.citizenship = "Chinese"
        candidate.requires_visa_sponsorship = True

        # 1. Eligibility evaluation must NOT be HARD_INELIGIBLE
        decision = evaluate_job_eligibility(job, candidate)
        assert decision.state != EligibilityState.HARD_INELIGIBLE
        assert decision.is_hard_ineligible is False
        assert decision.state == EligibilityState.ELIGIBLE

        # 2. CV generation MUST be allowed (no IneligibleJobError)
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifact = generate_cv(candidate, job, store_root=tmp_dir)
            assert artifact is not None
            assert artifact.job_id == job.id

    # -------------------------------------------------------------------------
    # CASE 10 — Sponsorship Offers / Silent JD Policy
    # -------------------------------------------------------------------------
    @pytest.mark.parametrize("desc", [
        "Visa sponsorship is available for this role. Python required.",
        "We may offer visa sponsorship for exceptional candidates. Python required.",
        "Build distributed backend systems using Python and PostgreSQL.",
    ])
    def test_case_10_sponsorship_offers_or_silent_jd_policy(self, desc):
        """1. JD explicitly offers sponsorship -> APPLY -> no human review merely because of sponsorship
        2. JD may offer sponsorship -> APPLY -> no human review merely because of sponsorship
        3. JD is silent on sponsorship -> APPLY -> no human review merely because the JD is silent
        """
        job = _make_job(
            job_id="experienced-swe-sponsor",
            title="Backend Software Engineer",
            company="Global Tech",
            description=desc,
        )
        candidate = sample_candidate()
        candidate.requires_visa_sponsorship = True

        # Eligibility is ELIGIBLE
        decision = evaluate_job_eligibility(job, candidate)
        assert decision.state == EligibilityState.ELIGIBLE

        # Role is experienced / ordinary -> AUTO_APPLICATION_ALLOWED (no review required merely for sponsorship)
        policy = determine_submission_policy(job)
        assert policy.policy == SubmissionPolicy.AUTO_APPLICATION_ALLOWED

    # -------------------------------------------------------------------------
    # CASE 11 — Question Resolution: Legal Right to Work -> YES
    # -------------------------------------------------------------------------
    @pytest.mark.parametrize("question_text", [
        "Do you currently have the legal right to work in the UK?",
        "Do you have the legal right to work in the UK?",
        "Are you currently permitted to work in the UK?",
        "Are you legally authorized to work in the UK?",
        "Right to work in the UK",
    ])
    def test_case_11_question_resolution_right_to_work_yes(self, question_text):
        """'Do you currently have the legal right to work in the UK?' -> YES
        'Are you currently permitted to work in the UK?' -> YES
        (where wording does not require unrestricted/permanent status).
        """
        candidate = sample_candidate()
        result = resolve_application_question(question_text, candidate=candidate, facts=SYNTHETIC_WORK_AUTH, answers={})
        assert result.resolved is True
        assert result.answer_value == "Yes"
        assert result.requires_human_input is False
        assert "Student visa" in result.reason or "right to work" in result.reason

    # -------------------------------------------------------------------------
    # CASE 12 — Question Resolution: Sponsorship Required -> YES
    # -------------------------------------------------------------------------
    @pytest.mark.parametrize("question_text", [
        "Will you now or in the future require sponsorship?",
        "Will you require visa sponsorship now or in the future?",
        "Do you now or in the future require sponsorship?",
        "Will you require Skilled Worker sponsorship in the future?",
        "Do you require Skilled Worker sponsorship?",
    ])
    def test_case_12_question_resolution_sponsorship_yes(self, question_text):
        """'Will you now or in the future require sponsorship?' -> YES
        'Will you require Skilled Worker sponsorship in the future?' -> YES
        """
        candidate = sample_candidate()
        result = resolve_application_question(question_text, candidate=candidate, facts=SYNTHETIC_WORK_AUTH, answers={})
        assert result.resolved is True
        assert result.answer_value == "Yes"
        assert result.requires_human_input is False
        assert "Skilled Worker" in result.reason or "sponsorship" in result.reason

    # -------------------------------------------------------------------------
    # CASE 13 — Permanent / Unrestricted Right to Work -> Do NOT Map to YES
    # -------------------------------------------------------------------------
    @pytest.mark.parametrize("question_text", [
        "Do you have permanent/unrestricted right to work in the UK?",
        "Do you have permanent right to work in the UK?",
        "Do you have unrestricted right to work in the UK?",
        "Do you hold indefinite leave to remain or settled status?",
        "Are you permitted to work in the UK without any restrictions?",
    ])
    def test_case_13_permanent_right_to_work_does_not_map_from_ordinary_rtw(self, question_text):
        """'Do you have permanent/unrestricted right to work in the UK?'
        -> do not map from ordinary right-to-work (answer is 'No', never 'Yes').
        """
        candidate = sample_candidate()
        result = resolve_application_question(question_text, candidate=candidate, facts=SYNTHETIC_WORK_AUTH, answers={})
        assert result.answer_value != "Yes"
        assert result.answer_value == "No"
        assert "ordinary right-to-work must not be mapped" in result.reason or "permanent" in result.reason

    # -------------------------------------------------------------------------
    # CASE 14 — Integrity Invariant: Never Transform YES to NO on No-Sponsor JD
    # -------------------------------------------------------------------------
    def test_case_14_never_transform_yes_to_no_for_no_sponsorship_jd(self):
        """Never transform an authoritative YES-to-sponsorship fact into NO merely
        because the employer says sponsorship is unavailable.
        """
        job = _make_job(
            job_id="strict-no-sponsor",
            title="Software Engineer",
            company="Strict Corp",
            description="We strictly do not offer visa sponsorship. No sponsorship available under any circumstances.",
        )
        candidate = sample_candidate()
        candidate.requires_visa_sponsorship = True

        result = resolve_application_question(
            "Will you now or in the future require sponsorship?",
            candidate=candidate,
            job=job,
            facts=SYNTHETIC_WORK_AUTH,
            answers={},
        )
        # MUST remain YES truthful to candidate facts; never changed to NO
        assert result.resolved is True
        assert result.answer_value == "Yes"
        assert "never transformed into NO" in result.reason

    # -------------------------------------------------------------------------
    # CASE 15 — Efficiency Rule: Stored Answers Reused Without Re-asking
    # -------------------------------------------------------------------------
    def test_case_15_efficiency_rule_reuses_stored_answers(self):
        """Efficiency rule: Do not repeatedly ask the user questions whose
        authoritative answer is already stored.
        """
        stored = {
            "Notice period": "1 month",
            "Salary expectation": "£45,000",
        }
        res1 = resolve_application_question("Notice period", stored_answers=stored)
        assert res1.resolved is True
        assert res1.answer_value == "1 month"
        assert res1.from_cache is True
        assert res1.requires_human_input is False

        res2 = resolve_application_question("Salary expectation", stored_answers=stored)
        assert res2.resolved is True
        assert res2.answer_value == "£45,000"
        assert res2.from_cache is True

    # -------------------------------------------------------------------------
    # CASE 16 — Unresolved Required Application Field Blocks Autonomous Submission
    # -------------------------------------------------------------------------
    def test_case_16_unresolved_required_field_blocks_autonomous_submission(self):
        """A no-sponsorship JD alone must not interrupt application preparation.
        Only an unresolved required application field may block autonomous final submission.
        """
        job = _make_job(
            job_id="experienced-swe-1",
            title="Senior Backend Engineer",
            company="Acme Corp",
            description="Build scalable distributed services. No visa sponsorship provided.",
        )
        candidate = sample_candidate()

        # 1. When all required fields are resolved: AUTO_APPLICATION_ALLOWED
        policy_all_resolved = determine_submission_policy(job, unresolved_fields=None)
        assert policy_all_resolved.policy == SubmissionPolicy.AUTO_APPLICATION_ALLOWED

        # 2. When an unresolved required question is present: blocks autonomous submission
        resolutions, unresolved = resolve_application_questions(
            ["Do you currently have the legal right to work in the UK?", "Why do you want to join our company?"],
            candidate=candidate,
            job=job,
            facts=SYNTHETIC_WORK_AUTH,
            answers={},
        )
        assert "Why do you want to join our company?" in unresolved
        assert resolutions["Do you currently have the legal right to work in the UK?"].resolved is True

        policy_with_unresolved = determine_submission_policy(job, unresolved_fields=unresolved)
        assert policy_with_unresolved.policy == SubmissionPolicy.REQUIRE_HUMAN_REVIEW
        assert "Unresolved required application field(s)" in policy_with_unresolved.reason

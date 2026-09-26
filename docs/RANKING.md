# Ranking And Matching

## Components

The matcher returns component scores instead of only an opaque match score:

- technical match
- role preference
- sponsorship
- graduation-year fit
- location preference
- source quality
- freshness

The final `overall_priority` is a weighted sum of these components. Weights and preferences are configuration-driven in `jobintel.config`.

Freshness is computed independently from source quality using the configured half-life decay and the best available `posted_at` timestamp. Raw source freshness is also retained through `first_seen_at` and `last_seen_at`.

## Configurable Defaults

- primary market: United Kingdom
- preferred city: London
- location mode: London first, UK-wide
- candidate graduation year: 2026 (the candidate's actual degree completion year -- see "Graduation Year vs. Intake Year" below)
- sponsorship filter modes supported by taxonomy: sponsor only, no sponsor only, all
- excluded occupations: sales, front desk, marketing, accounting, investment banking analyst, civil/mechanical/chemical/power roles

## Graduation Year vs. Intake Year

`jobintel.analysis.evidence.detect_graduation_year` distinguishes two concepts that must not be conflated:

- **Candidate graduation year** (`RankingConfig.candidate_graduation_year`) -- when the candidate actually completed their degree. This is the *only* value ranking compares against a JD's explicit graduation-year eligibility text: a strict single-year restriction ("2026 graduates only", "class of 2026") or an explicit range ("2025-2027 graduates").
- **Job intake/start year** -- a job may independently state an intake or programme-start year ("2027 Graduate Programme", "2028 intake"). This is extracted as `GraduationYearEvidence.intake_year` for display/context only and is **never used to filter or downrank a job**. There is deliberately no single "target intake year" config value: graduate opportunities from any intake year are relevant if the candidate is otherwise eligible, so a job with a 2027 or 2028 intake is not penalised relative to one with a 2026 intake.

The matcher is permissive by default to avoid silent false negatives: a job is only classified as excluding the candidate when the JD gives explicit, unambiguous graduation-year evidence (a strict restriction to a different year, or a range that excludes the candidate's year). No year mentioned, an intake year with no separate graduation-year restriction, or generic "graduate programme" phrasing all fall through to `NO_YEAR_STATED` or `GRADUATE_FRIENDLY` -- both neutral-to-positive states that never filter a job out. See `tests/test_graduation_year_distinction.py` for worked examples, including explicit regression tests that jobs with unknown or differing intake years are retained rather than dropped.

Source quality weights are configurable for direct company/ATS, Greenhouse, Lever, Adzuna, and future sources such as Welcome to the Jungle, LinkedIn, and Indeed. They are personal ranking preferences, not universal claims about source quality.

## CV Recommendation

The matcher recommends the closest available CV category and marks `hybrid_cv_recommended` when primary and secondary role tracks map to different CV families.

Version 1 recommends emphasis only. It does not rewrite CVs.

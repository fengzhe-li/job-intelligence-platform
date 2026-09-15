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
- graduation year: 2026
- sponsorship filter modes supported by taxonomy: sponsor only, no sponsor only, all
- excluded occupations: sales, front desk, marketing, accounting, investment banking analyst, civil/mechanical/chemical/power roles

Source quality weights are configurable for direct company/ATS, Greenhouse, Lever, Adzuna, and future sources such as Welcome to the Jungle, LinkedIn, and Indeed. They are personal ranking preferences, not universal claims about source quality.

## CV Recommendation

The matcher recommends the closest available CV category and marks `hybrid_cv_recommended` when primary and secondary role tracks map to different CV families.

Version 1 recommends emphasis only. It does not rewrite CVs.

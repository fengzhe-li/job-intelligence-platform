# Live Job Validation

This report was generated offline because the current environment could not reach public Greenhouse/Lever ATS hosts. It does not claim that live jobs were fetched.

## Completed Offline Validation

- registry companies configured: 62
- enabled live connectors: 0
- pending source verifications: 62
- imported candidate evidence sources: 8 profile records from 8 evidence sources
- extracted candidate capabilities: 21
- fixture jobs normalised/ranked: 7
- fixture cross-track opportunities: 6

## Live Validation Not Executed

- Public ATS network access failed in this environment during Greenhouse/Lever probing.
- Therefore raw live jobs fetched: 0.
- No live source quality conclusions are claimed.

## Offline Fixture Metrics

- London vs rest-of-UK: {'london': 3, 'rest_of_uk': 4}
- role-track distribution: {'software_engineering': 5, 'backend_engineering': 2, 'data_platform': 1, 'frontend_engineering': 2, 'full_stack_engineering': 2, 'network_software': 1, 'iot': 1, 'embedded_software': 1, 'infrastructure_engineering': 1, 'telecommunications': 1}
- sponsorship distribution: {'explicit_sponsor': 1, 'unknown': 5, 'likely_no_sponsor': 1}
- graduation-year distribution: {'no_year_stated': 5, 'graduate_friendly': 1, 'year_2027_only_strict': 1}

## Fixture-Based Ranking Examples

### Software Engineer - Data Platform - Fixture FinTech

- source-shaped fixture: greenhouse
- location: London, United Kingdom
- work mode: unknown
- direct application URL: https://fixtures.example/greenhouse/gh-data-platform
- posted / first seen: 2026-08-25T09:00:00+00:00 / 2026-08-25T09:00:00+00:00
- role tracks: ['software_engineering', 'backend_engineering', 'data_platform']
- sponsorship: explicit_sponsor
- graduation-year: no_year_stated
- technical match: 1.0
- application priority: 6.365
- recommended CV: Software/Backend
- hybrid CV: True
- strongest evidence: ['Python: Graduate Software Engineer CV One Page - Graduate Software Engineer with experience building backend systems, cloud applications and AI-powered platforms using Python, AWS and Docker', 'PostgreSQL: Graduate Software Engineer CV One Page - Databases: PostgreSQL, SQL Server, DynamoDB, Data Modelling, Joins, CTEs', 'SQL: Graduate Software Engineer CV One Page - Programming Languages: Python, C, C++, SQL, Bash']
- missing/weak skills: []
- explanation: Matched candidate evidence for Python, PostgreSQL, SQL, Docker; Primary software_engineering; cross-track signals: backend_engineering, data_platform; Sponsorship: explicit_sponsor from 'Visa sponsorship is available'; Graduation year: no_year_stated; London role; Source quality based on configurable source weights; Freshness based on configurable half-life decay

### Full-stack Engineer - Fixture SaaS

- source-shaped fixture: lever
- location: London, United Kingdom
- work mode: unknown
- direct application URL: https://fixtures.example/lever/lv-fullstack/apply
- posted / first seen: 2026-08-25T09:00:00+00:00 / 2026-08-25T09:00:00+00:00
- role tracks: ['frontend_engineering', 'full_stack_engineering', 'backend_engineering']
- sponsorship: unknown
- graduation-year: no_year_stated
- technical match: 0.855
- application priority: 5.221
- recommended CV: Software/Backend
- hybrid CV: True
- strongest evidence: ['FastAPI: Graduate Software Engineer CV One Page - Backend & APIs: REST APIs, FastAPI, API Design, Backend Architecture', 'PostgreSQL: Graduate Software Engineer CV One Page - Databases: PostgreSQL, SQL Server, DynamoDB, Data Modelling, Joins, CTEs', 'TypeScript: Meet Eat README - A["React + TypeScript + Vite"] -->|"HTTP API"| B["FastAPI Backend"]']
- missing/weak skills: []
- explanation: Matched candidate evidence for TypeScript, React, FastAPI, PostgreSQL; Primary frontend_engineering; cross-track signals: full_stack_engineering, backend_engineering; Sponsorship: unknown; Graduation year: no_year_stated; London role; Source quality based on configurable source weights; Freshness based on configurable half-life decay

### Network Software Engineer - Fixture Internet Infra

- source-shaped fixture: greenhouse
- location: Cambridge, United Kingdom
- work mode: unknown
- direct application URL: https://fixtures.example/greenhouse/gh-network
- posted / first seen: 2026-08-25T09:00:00+00:00 / 2026-08-25T09:00:00+00:00
- role tracks: ['network_software', 'software_engineering']
- sponsorship: unknown
- graduation-year: graduate_friendly
- technical match: 0.797
- application priority: 4.814
- recommended CV: Software/Backend
- hybrid CV: False
- strongest evidence: ['Python: Graduate Software Engineer CV One Page - Graduate Software Engineer with experience building backend systems, cloud applications and AI-powered platforms using Python, AWS and Docker', 'TCP/IP: Graduate Software Engineer CV One Page - UCL MSc Dissertation Project | Python, Docker, CRIU, Linux, TCP/IP, Distributed Systems']
- missing/weak skills: []
- explanation: Matched candidate evidence for Python, TCP/IP; Primary network_software; cross-track signals: software_engineering; Sponsorship: unknown; Graduation year: graduate_friendly from 'Recent graduates welcome'; UK role outside London; Source quality based on configurable source weights; Freshness based on configurable half-life decay

### Frontend Engineer - Fixture Product

- source-shaped fixture: adzuna
- location: Remote - UK
- work mode: remote
- direct application URL: https://fixtures.example/adzuna/adz-frontend
- posted / first seen: 2026-08-25T09:00:00+00:00 / 2026-08-25T09:00:00+00:00
- role tracks: ['frontend_engineering']
- sponsorship: unknown
- graduation-year: no_year_stated
- technical match: 0.71
- application priority: 4.372
- recommended CV: Frontend
- hybrid CV: False
- strongest evidence: ['TypeScript: Meet Eat README - A["React + TypeScript + Vite"] -->|"HTTP API"| B["FastAPI Backend"]', 'React: Meet Eat README - The project is positioned as a portfolio-grade consumer product engineering project: React frontend, FastAPI backend, PostgreSQL persistence, reproducible Docker setup, CI, and an optional grounded Z']
- missing/weak skills: []
- explanation: Matched candidate evidence for TypeScript, React; Primary frontend_engineering; Sponsorship: unknown; Graduation year: no_year_stated; UK role outside London; Source quality based on configurable source weights; Freshness based on configurable half-life decay

### IoT Embedded Software Engineer - Fixture Connected Systems

- source-shaped fixture: greenhouse
- location: Oxford, United Kingdom
- work mode: unknown
- direct application URL: https://fixtures.example/greenhouse/gh-embedded
- posted / first seen: 2026-08-25T09:00:00+00:00 / 2026-08-25T09:00:00+00:00
- role tracks: ['iot', 'embedded_software', 'software_engineering', 'infrastructure_engineering']
- sponsorship: unknown
- graduation-year: no_year_stated
- technical match: 0.547
- application priority: 4.135
- recommended CV: Software/Backend
- hybrid CV: False
- strongest evidence: ['Linux: Graduate Software Engineer CV One Page - Software Engineering: Object-Oriented Programming, Data Structures & Algorithms, Software Design, Unit Testing, Git, Linux', 'C: Graduate Software Engineer CV One Page - Programming Languages: Python, C, C++, SQL, Bash', 'C++: Graduate Software Engineer CV One Page - Programming Languages: Python, C, C++, SQL, Bash']
- missing/weak skills: ['MQTT']
- explanation: Matched candidate evidence for C, Linux, C++; Primary iot; cross-track signals: embedded_software, software_engineering, infrastructure_engineering; Sponsorship: unknown; Graduation year: no_year_stated; UK role outside London; Source quality based on configurable source weights; Freshness based on configurable half-life decay

### 2027 Software Engineering Graduate Programme - Fixture Bank

- source-shaped fixture: lever
- location: London, United Kingdom
- work mode: unknown
- direct application URL: https://fixtures.example/lever/lv-2027/apply
- posted / first seen: 2026-08-25T09:00:00+00:00 / 2026-08-25T09:00:00+00:00
- role tracks: ['software_engineering', 'full_stack_engineering']
- sponsorship: unknown
- graduation-year: year_2027_only_strict
- technical match: 1.0
- application priority: 4.095
- recommended CV: Software/Backend
- hybrid CV: True
- strongest evidence: ['Python: Graduate Software Engineer CV One Page - Graduate Software Engineer with experience building backend systems, cloud applications and AI-powered platforms using Python, AWS and Docker']
- missing/weak skills: []
- explanation: Matched candidate evidence for Python; Primary software_engineering; cross-track signals: full_stack_engineering; Sponsorship: unknown; Graduation year: year_2027_only_strict from '2027 graduates only'; London role; Source quality based on configurable source weights; Freshness based on configurable half-life decay

### Telecommunications Software Engineer - Fixture Telecom

- source-shaped fixture: adzuna
- location: Manchester, United Kingdom
- work mode: unknown
- direct application URL: https://fixtures.example/adzuna/adz-telecom
- posted / first seen: 2026-08-25T09:00:00+00:00 / 2026-08-25T09:00:00+00:00
- role tracks: ['telecommunications', 'software_engineering']
- sponsorship: likely_no_sponsor
- graduation-year: no_year_stated
- technical match: 0.5
- application priority: 3.312
- recommended CV: Software/Backend
- hybrid CV: False
- strongest evidence: ['Linux: Graduate Software Engineer CV One Page - Software Engineering: Object-Oriented Programming, Data Structures & Algorithms, Software Design, Unit Testing, Git, Linux']
- missing/weak skills: ['5G']
- explanation: Matched candidate evidence for Linux; Primary telecommunications; cross-track signals: software_engineering; Sponsorship: likely_no_sponsor from 'Must have right to work in the UK without sponsorship'; Graduation year: no_year_stated; UK role outside London; Source quality based on configurable source weights; Freshness based on configurable half-life decay

## Exact Live Commands To Run On A Networked Machine

After verifying a Greenhouse board token or Lever site token, set `enabled` to `true`, set `source_connector_type` to `greenhouse` or `lever`, set `verification_status` to `verified`, and fill the token field in `config/target_companies.json`.

```bash
PYTHONPATH=src python3 -m jobintel.cli.main registry-summary
PYTHONPATH=src python3 -m jobintel.cli.main ingest-registry --limit 100
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --limit 30
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --sponsorship sponsor_only --limit 30
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --location-mode uk_wide --limit 30
PYTHONPATH=src python3 -m jobintel.cli.main validation-report --output docs/PHASE3_VALIDATION.md
PYTHONPATH=src python3 -m jobintel.cli.main new-jobs --since 2026-08-25T00:00:00+00:00 --limit 30
```

## Expected Artifacts After Live Run

- `data/local/raw_snapshots/*.jsonl`: raw source payloads
- `data/local/processed/canonical_jobs.json`: deduplicated canonical jobs
- `docs/PHASE3_VALIDATION.md`: measured live validation metrics
- CLI shortlist output from `list-ranked`

## Issues To Watch During Live Run

- Some company careers pages may not use public Greenhouse/Lever feeds.
- Greenhouse/Lever tokens may differ from company names and must be verified before enabling.
- Some feeds may expose global roles; UK relevance depends on location normalisation.
- Sponsorship is often unknown because many descriptions do not mention it.
- Graduate-year wording can be sparse; unknown should remain visible rather than being treated as rejection.

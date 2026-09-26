# Company Registry

The target-company registry lives at `config/target_companies.json`.

It is designed for a personal UK graduate job search, not full-market ingestion. Companies can be added, removed, enabled, or disabled without changing code.

Example entry:

```json
{
  "company_name": "Monzo",
  "industry": "FinTech",
  "priority": 5,
  "greenhouse_board_token": "monzo",
  "lever_site_token": null,
  "careers_url": "https://monzo.com/careers/",
  "source_connector_type": "greenhouse",
  "enabled": true,
  "notes": "FinTech, backend, data, platform roles"
}
```

Supported connector-backed fields:

- `greenhouse_board_token`, `lever_site_token`, `ashby_board_name`, `workable_account`, `smartrecruiters_company_identifier`, `welcome_to_the_jungle_organization_reference`
- Generic `connector_type`/`connector_token` (used for every ATS `ats-discover` finds, and the only fields Workday uses -- `connector_token` for Workday is `"{tenant}.{wdN}/{site}"`, e.g. `"darktrace.wd3/DarktaceExternal"`, since a bare tenant name alone isn't enough to build a working URL)

Tracked but not scraped:

- `careers_url`

Current registry categories include FinTech, banking technology, payments, SaaS, AI/technology, cloud/infrastructure, telecom, internet/network infrastructure, electronics/communications, and IoT/connected systems.

## Coverage state (Phase 2.7)

"An entry exists in this registry" is not the same as "this company is monitored." Every entry is classified into exactly one of five states by `src/jobintel/company_coverage.py` (run `company-coverage-report` for the live numbers): `AUTO_VERIFIED` (a real, live-verified working connector), `PARTIAL_AUTOMATION`, `MANUAL_REQUIRED` (no reasonable automated path found, or a candidate deliberately excluded -- see `docs/SOURCE_COVERAGE.md`'s Juniper Networks UK/HPE example), `TEMPORARILY_FAILED` (should work, failed this cycle), or `UNKNOWN`. Never describe a company as "covered" or "monitored" based on having a `careers_url` alone.

## Growing the registry without code changes

`company-discovery-scan` logs companies seen through non-registry sources (Prospects, Adzuna, manual imports) that aren't already registered, with provenance (which source, an example job, and an auto-detected ATS suggestion when the job's own application URL reveals one). `company-discovery-list` shows what's pending review, and `company-discovery-promote --company <name>` adds a reviewed candidate into this same registry file through the normal `ats-add`-style pipeline -- see `src/jobintel/company_discovery.py`. Nothing is auto-trusted or auto-added; promotion is always an explicit step.

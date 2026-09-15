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

- `greenhouse_board_token`
- `lever_site_token`

Tracked but not scraped:

- `careers_url`

Current registry categories include FinTech, banking technology, payments, SaaS, AI/technology, cloud/infrastructure, telecom, internet/network infrastructure, electronics/communications, and IoT/connected systems.

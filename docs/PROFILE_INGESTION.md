# Personal Profile Ingestion

The profile ingestion workflow builds one unified candidate profile from imported evidence files:

- CV text or PDF-extracted text
- Markdown/plain-text project descriptions
- GitHub README exports
- education descriptions
- internship/work experience descriptions

Capabilities retain provenance through `EvidenceSource` and `CapabilityEvidence`.

Supported source type values:

- `cv_text`
- `cv_pdf_extracted_text`
- `readme_markdown`
- `manual_project_description`
- `github_readme_export`
- `education_description`
- `experience_description`

The extractor is deterministic in Phase 3. It uses known technical skill patterns and role-track keyword inference. Later NLP/LLM enrichment can improve recall and phrasing without changing the provenance model.

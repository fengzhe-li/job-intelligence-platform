from __future__ import annotations

from enum import StrEnum


class RoleTrack(StrEnum):
    SOFTWARE_ENGINEERING = "software_engineering"
    BACKEND_ENGINEERING = "backend_engineering"
    FRONTEND_ENGINEERING = "frontend_engineering"
    FULL_STACK_ENGINEERING = "full_stack_engineering"
    DATA_ENGINEERING = "data_engineering"
    DATA_PLATFORM = "data_platform"
    CLOUD_ENGINEERING = "cloud_engineering"
    PLATFORM_ENGINEERING = "platform_engineering"
    INFRASTRUCTURE_ENGINEERING = "infrastructure_engineering"
    DEVOPS_SRE = "devops_sre"
    AI_ML_ENGINEERING = "ai_ml_engineering"
    TELECOMMUNICATIONS = "telecommunications"
    NETWORK_ENGINEERING = "network_engineering"
    NETWORK_SOFTWARE = "network_software"
    INTERNET_ENGINEERING = "internet_engineering"
    ELECTRONIC_COMMUNICATION = "electronic_communication_engineering"
    IOT = "iot"
    EMBEDDED_SOFTWARE = "embedded_software"
    EDGE_CONNECTED_SYSTEMS = "edge_connected_systems"


class EvidenceSourceType(StrEnum):
    CV_TEXT = "cv_text"
    CV_PDF_EXTRACTED_TEXT = "cv_pdf_extracted_text"
    README_MARKDOWN = "readme_markdown"
    MANUAL_PROJECT_DESCRIPTION = "manual_project_description"
    GITHUB_README_EXPORT = "github_readme_export"
    EDUCATION_DESCRIPTION = "education_description"
    EXPERIENCE_DESCRIPTION = "experience_description"


class SkillCategory(StrEnum):
    LANGUAGE = "language"
    FRAMEWORK = "framework"
    DATABASE = "database"
    CLOUD = "cloud"
    DATA = "data"
    ML_AI = "ml_ai"
    BACKEND = "backend"
    FRONTEND = "frontend"
    NETWORK = "network"
    EMBEDDED = "embedded"
    DEVOPS = "devops"
    DOMAIN = "domain"
    TOOLING = "tooling"


class SponsorshipState(StrEnum):
    EXPLICIT_SPONSOR = "explicit_sponsor"
    LIKELY_SPONSOR = "likely_sponsor"
    UNKNOWN = "unknown"
    LIKELY_NO_SPONSOR = "likely_no_sponsor"
    EXPLICIT_NO_SPONSOR = "explicit_no_sponsor"


class SponsorshipFilterMode(StrEnum):
    SPONSOR_ONLY = "sponsor_only"
    NO_SPONSOR_ONLY = "no_sponsor_only"
    ALL = "all"


class GraduationYearState(StrEnum):
    NO_YEAR_STATED = "no_year_stated"
    GRADUATE_FRIENDLY = "graduate_friendly"
    YEAR_2026_ACCEPTED = "year_2026_accepted"
    YEAR_2027_MENTIONED = "year_2027_mentioned"
    YEAR_2027_ONLY_STRICT = "year_2027_only_strict"
    OTHER_YEAR_RESTRICTION = "other_year_restriction"


class LocationMode(StrEnum):
    LONDON_ONLY = "london_only"
    LONDON_FIRST_UK_WIDE = "london_first_uk_wide"
    UK_WIDE = "uk_wide"


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class WorkflowStatus(StrEnum):
    NEW = "new"
    SAVED = "saved"
    APPLIED = "applied"
    OA = "oa"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    IGNORE = "ignore"


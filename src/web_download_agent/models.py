from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CrawlConfig:
    start_url: str
    output_dir: Path
    max_depth: int = 1
    max_pages: int = 50
    same_origin_only: bool = True
    prefer_browser: bool = True
    user_agent: str = "web-download-agent/0.1"
    request_timeout_seconds: int = 20
    page_worker_count: int = 3
    asset_concurrency: int = 8
    save_network_log: bool = True
    cookie_header: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    storage_state_path: Path | None = None
    page_retry_limit: int = 2
    retry_backoff_seconds: float = 1.0
    use_llm_strategy: bool = False
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_api_key_env: str = "DEEPSEEK_API_KEY"
    llm_base_url: str | None = None
    llm_max_output_tokens: int = 300
    llm_timeout_seconds: int = 30
    use_llm_site_profile: bool = False
    llm_site_profile_html_chars: int = 4000
    use_llm_seed_discovery: bool = False
    llm_seed_html_chars: int = 5000
    llm_seed_candidate_limit: int = 12
    use_llm_interaction_planner: bool = False
    llm_interaction_html_chars: int = 5000
    llm_interaction_action_limit: int = 4
    llm_interaction_max_depth: int = 1
    use_llm_failure_diagnosis: bool = False
    use_llm_validation: bool = False
    llm_validation_html_chars: int = 6000


@dataclass(slots=True)
class SiteProfile:
    start_url: str
    browser_required: bool
    reasons: list[str] = field(default_factory=list)
    site_type: str = "unknown"
    interaction_hints: list[str] = field(default_factory=list)
    profile_source: str = "heuristic"
    strategy_source: str = "heuristic"


@dataclass(slots=True)
class PageSnapshot:
    url: str
    final_url: str
    html: str
    status_code: int | None
    used_browser: bool
    content_type: str | None = None
    discovered_assets: list[str] = field(default_factory=list)
    network_log: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PageResult:
    url: str
    saved_path: str
    depth: int
    asset_count: int
    used_browser: bool
    issues: list[str] = field(default_factory=list)
    validation_source: str = "heuristic"
    validation_summary: str | None = None
    validation_guidance: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PageTask:
    url: str
    depth: int
    attempt: int = 0


@dataclass(slots=True)
class FailureRecord:
    url: str
    depth: int
    stage: str
    category: str
    message: str
    attempts: int
    retryable: bool
    diagnosis_source: str = "heuristic"
    analysis: str | None = None
    guidance: list[str] = field(default_factory=list)


@dataclass(slots=True)
class JobManifest:
    start_url: str
    output_dir: str
    pages: list[PageResult] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

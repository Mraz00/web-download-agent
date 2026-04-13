from __future__ import annotations

import argparse
from pathlib import Path

from .models import CrawlConfig
from .pipeline import run_pipeline


def parse_header_arguments(parser: argparse.ArgumentParser, values: list[str] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_value in values or []:
        if ":" not in raw_value:
            parser.error(f"Invalid --header value: {raw_value!r}. Expected 'Name: Value'.")
        name, value = raw_value.split(":", 1)
        header_name = name.strip()
        header_value = value.strip()
        if not header_name:
            parser.error(f"Invalid --header value: {raw_value!r}. Header name cannot be empty.")
        headers[header_name] = header_value
    return headers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download a website into a local mirror.")
    parser.add_argument("--url", required=True, help="Start URL to mirror.")
    parser.add_argument(
        "--output-dir",
        default="./output/jobs",
        help="Directory used to store generated mirror jobs.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=1,
        help="Maximum crawl depth, where 0 means only the start page.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Maximum number of pages to schedule in a single job.",
    )
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Allow following external-domain links and assets.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Disable browser-first rendering and use plain HTTP fetching only.",
    )
    parser.add_argument(
        "--page-workers",
        type=int,
        default=3,
        help="Number of concurrent page workers in the coordinator queue.",
    )
    parser.add_argument(
        "--asset-concurrency",
        type=int,
        default=8,
        help="Maximum concurrent asset downloads per page.",
    )
    parser.add_argument(
        "--cookie",
        help="Raw Cookie header used for authenticated requests, for example 'session=abc; user=demo'.",
    )
    parser.add_argument(
        "--header",
        action="append",
        help="Extra HTTP header in 'Name: Value' form. Can be provided multiple times.",
    )
    parser.add_argument(
        "--storage-state",
        help="Path to a Playwright storage state JSON file for authenticated browser sessions.",
    )
    parser.add_argument(
        "--page-retries",
        type=int,
        default=2,
        help="How many times a page task may be retried after retryable failures.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="Base backoff seconds between page retries. Delay grows with each attempt.",
    )
    parser.add_argument(
        "--use-llm-strategy",
        action="store_true",
        help="Let the strategy agent call an LLM to refine crawl decisions before workers start.",
    )
    parser.add_argument(
        "--llm-provider",
        default="deepseek",
        choices=["deepseek", "openai"],
        help="LLM provider used by the strategy agent.",
    )
    parser.add_argument(
        "--llm-model",
        default="deepseek-chat",
        help="Model name used by the LLM strategy agent.",
    )
    parser.add_argument(
        "--llm-api-key-env",
        default="DEEPSEEK_API_KEY",
        help="Environment variable name that stores the LLM API key.",
    )
    parser.add_argument(
        "--llm-base-url",
        help="Optional base URL for OpenAI-compatible LLM APIs. Defaults to the provider's official endpoint.",
    )
    parser.add_argument(
        "--llm-max-output-tokens",
        type=int,
        default=300,
        help="Maximum output tokens reserved for the LLM strategy response.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for the LLM strategy request.",
    )
    parser.add_argument(
        "--use-llm-site-profile",
        action="store_true",
        help="Let the site profiler call an LLM on a homepage excerpt before strategy selection.",
    )
    parser.add_argument(
        "--llm-site-profile-html-chars",
        type=int,
        default=4000,
        help="Maximum homepage HTML characters sent to the LLM site profiler.",
    )
    parser.add_argument(
        "--use-llm-seed-discovery",
        action="store_true",
        help="Let an LLM infer additional same-site seed URLs from the start page.",
    )
    parser.add_argument(
        "--llm-seed-html-chars",
        type=int,
        default=5000,
        help="Maximum start-page HTML characters sent to the LLM seed discovery agent.",
    )
    parser.add_argument(
        "--llm-seed-candidate-limit",
        type=int,
        default=12,
        help="Maximum candidate URLs the LLM seed discovery agent may suggest.",
    )
    parser.add_argument(
        "--use-llm-interaction-planner",
        action="store_true",
        help="Let an LLM plan a few safe Playwright interactions such as scrolling or clicking 'more/next/expand' controls.",
    )
    parser.add_argument(
        "--llm-interaction-html-chars",
        type=int,
        default=5000,
        help="Maximum rendered HTML characters sent to the interaction planner.",
    )
    parser.add_argument(
        "--llm-interaction-action-limit",
        type=int,
        default=4,
        help="Maximum interaction steps the planner may return for one page.",
    )
    parser.add_argument(
        "--llm-interaction-max-depth",
        type=int,
        default=1,
        help="Only pages at or above this depth may invoke the interaction planner.",
    )
    parser.add_argument(
        "--use-llm-failure-diagnosis",
        action="store_true",
        help="Let the failure diagnosis agent call an LLM for explanation and recovery suggestions on final failures.",
    )
    parser.add_argument(
        "--use-llm-validation",
        action="store_true",
        help="Let the validator call an LLM to assess offline page quality for the start page and pages with issues.",
    )
    parser.add_argument(
        "--llm-validation-html-chars",
        type=int,
        default=6000,
        help="Maximum HTML characters sent to the LLM quality validator per page.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    extra_headers = parse_header_arguments(parser, args.header)
    config = CrawlConfig(
        start_url=args.url,
        output_dir=Path(args.output_dir).resolve(),
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        same_origin_only=not args.allow_external,
        prefer_browser=not args.no_browser,
        page_worker_count=args.page_workers,
        asset_concurrency=args.asset_concurrency,
        cookie_header=args.cookie,
        extra_headers=extra_headers,
        storage_state_path=Path(args.storage_state).resolve() if args.storage_state else None,
        page_retry_limit=args.page_retries,
        retry_backoff_seconds=args.retry_backoff,
        use_llm_strategy=args.use_llm_strategy,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_api_key_env=args.llm_api_key_env,
        llm_base_url=args.llm_base_url,
        llm_max_output_tokens=args.llm_max_output_tokens,
        llm_timeout_seconds=args.llm_timeout,
        use_llm_site_profile=args.use_llm_site_profile,
        llm_site_profile_html_chars=args.llm_site_profile_html_chars,
        use_llm_seed_discovery=args.use_llm_seed_discovery,
        llm_seed_html_chars=args.llm_seed_html_chars,
        llm_seed_candidate_limit=args.llm_seed_candidate_limit,
        use_llm_interaction_planner=args.use_llm_interaction_planner,
        llm_interaction_html_chars=args.llm_interaction_html_chars,
        llm_interaction_action_limit=args.llm_interaction_action_limit,
        llm_interaction_max_depth=args.llm_interaction_max_depth,
        use_llm_failure_diagnosis=args.use_llm_failure_diagnosis,
        use_llm_validation=args.use_llm_validation,
        llm_validation_html_chars=args.llm_validation_html_chars,
    )
    manifest = run_pipeline(config)

    print(f"job_dir={manifest.output_dir}")
    print(f"pages={len(manifest.pages)}")
    print(f"failures={len(manifest.failures)}")
    if manifest.notes:
        print("notes=" + " | ".join(manifest.notes))


if __name__ == "__main__":
    main()

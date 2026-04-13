from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .models import CrawlConfig, FailureRecord, PageSnapshot, SiteProfile

RESOURCE_ATTRIBUTES = {"src", "href", "poster", "background", "data-src", "data-href"}
PAGE_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
ASSET_EXTENSIONS = {
    ".css",
    ".js",
    ".mjs",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".webm",
    ".pdf",
    ".zip",
}
NETWORK_ASSET_TYPES = {"stylesheet", "script", "image", "font", "media"}
CSS_URL_PATTERN = re.compile(r"""url\(\s*(?P<quote>['"]?)(?P<url>[^)"']+)(?P=quote)\s*\)""", re.IGNORECASE)
CSS_IMPORT_PATTERN = re.compile(
    r"""@import\s+(?:url\(\s*)?(?P<quote>['"]?)(?P<url>[^)"';]+)(?P=quote)\s*\)?(?P<tail>\s*[^;]*)?;""",
    re.IGNORECASE,
)
CODE_FENCE_PATTERN = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
SAFE_INTERACTION_TERMS = (
    "更多",
    "下一页",
    "下页",
    "展开",
    "加载更多",
    "more",
    "next",
    "load more",
    "expand",
)


def build_request_headers(config: CrawlConfig) -> dict[str, str]:
    headers = {"User-Agent": config.user_agent}
    headers.update(config.extra_headers)
    if config.cookie_header:
        headers["Cookie"] = config.cookie_header
    return headers


def cookie_header_to_playwright_cookies(cookie_header: str, target_url: str) -> list[dict[str, str]]:
    parsed = urllib.parse.urlparse(target_url)
    if not parsed.scheme or not parsed.netloc:
        return []

    cookies: list[dict[str, str]] = []
    for chunk in cookie_header.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        cookie_name = name.strip()
        cookie_value = value.strip()
        if not cookie_name:
            continue
        cookies.append(
            {
                "name": cookie_name,
                "value": cookie_value,
                "domain": parsed.hostname or parsed.netloc,
                "path": "/",
            }
        )
    return cookies


def resolve_llm_base_url(config: CrawlConfig) -> str | None:
    if config.llm_base_url:
        return config.llm_base_url
    if config.llm_provider == "deepseek":
        return "https://api.deepseek.com"
    return None


def build_openai_compatible_client(config: CrawlConfig) -> Any:
    from openai import OpenAI

    api_key = os.getenv(config.llm_api_key_env)
    if not api_key:
        raise EnvironmentError(f"{config.llm_api_key_env} is not set")

    client_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": config.llm_timeout_seconds,
    }
    base_url = resolve_llm_base_url(config)
    if base_url:
        client_kwargs["base_url"] = base_url

    return OpenAI(**client_kwargs)


def extract_first_json_value(raw_text: str) -> str:
    text = CODE_FENCE_PATTERN.sub("", raw_text.strip()).strip()
    if not text:
        raise ValueError("empty LLM response")

    if text[0] in "{[":
        return text

    start_positions = [index for index, char in enumerate(text) if char in "{["]
    for start in start_positions:
        opening = text[start]
        closing = "}" if opening == "{" else "]"
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == opening:
                depth += 1
                continue
            if char == closing:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
    raise ValueError("no JSON object or array found in LLM response")


def load_llm_json(raw_text: str, source: str) -> dict[str, Any]:
    json_text = extract_first_json_value(raw_text)
    payload = json.loads(json_text)
    if not isinstance(payload, dict):
        raise ValueError(f"{source} response was not a JSON object")
    return payload


class LinkAndAssetParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: set[str] = set()
        self.assets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for attr_name, attr_value in attrs:
            if not attr_value or attr_name not in RESOURCE_ATTRIBUTES:
                continue
            absolute_url = urllib.parse.urljoin(self.base_url, attr_value)
            parsed = urllib.parse.urlparse(absolute_url)
            if parsed.scheme not in {"http", "https"}:
                continue

            extension = Path(parsed.path).suffix.lower()
            if tag == "a" and extension in PAGE_EXTENSIONS:
                self.links.add(absolute_url)
            elif extension in ASSET_EXTENSIONS or tag in {"img", "script", "link", "source", "video"}:
                self.assets.add(absolute_url)
            elif tag == "a":
                self.links.add(absolute_url)


class SafeInteractionTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_stack: list[str] = []
        self._current_chunks: list[str] = []
        self._current_tag: str | None = None
        self._candidates: list[str] = []

    @property
    def candidates(self) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for item in self._candidates:
            normalized = normalize_visible_text(item)
            if normalized and normalized not in seen:
                seen.add(normalized)
                deduped.append(item)
        return deduped

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self._ignored_stack.append(lowered)
            return
        if lowered in {"a", "button"}:
            self._current_tag = lowered
            self._current_chunks = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._ignored_stack and self._ignored_stack[-1] == lowered:
            self._ignored_stack.pop()
            return
        if self._current_tag == lowered:
            candidate = normalize_visible_text(" ".join(self._current_chunks))
            if is_safe_interaction_text(candidate):
                self._candidates.append(candidate)
            self._current_tag = None
            self._current_chunks = []

    def handle_data(self, data: str) -> None:
        if self._ignored_stack or self._current_tag is None:
            return
        text = normalize_visible_text(data)
        if text:
            self._current_chunks.append(text)


def normalize_visible_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def is_safe_interaction_text(value: str) -> bool:
    if not value or len(value) > 40:
        return False
    lowered = value.lower()
    return any(term in value or term in lowered for term in SAFE_INTERACTION_TERMS)


def extract_safe_interaction_candidates(html: str, limit: int) -> list[str]:
    parser = SafeInteractionTextParser()
    parser.feed(html)
    return parser.candidates[: max(1, limit)]


def resolve_click_candidate(action_text: str, candidates: list[str]) -> str | None:
    normalized_action = normalize_visible_text(action_text)
    if not normalized_action:
        return None

    lowered_action = normalized_action.lower()
    ranked: list[tuple[int, int, str]] = []
    for candidate in candidates:
        normalized_candidate = normalize_visible_text(candidate)
        lowered_candidate = normalized_candidate.lower()
        if (
            normalized_candidate == normalized_action
            or lowered_candidate == lowered_action
            or normalized_action in normalized_candidate
            or lowered_action in lowered_candidate
            or normalized_candidate in normalized_action
            or lowered_candidate in lowered_action
        ):
            score = 0 if normalized_candidate == normalized_action or lowered_candidate == lowered_action else 1
            ranked.append((score, len(normalized_candidate), candidate))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


class HTMLRewriterParser(HTMLParser):
    def __init__(
        self,
        base_url: str,
        page_relative_path: Path,
        asset_map: dict[str, str],
        page_link_map: dict[str, Path],
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        # HTML files live under output/pages/, while assets live under output/assets/.
        # Compute relative paths from the page's real on-disk location inside output/pages.
        self.current_dir = Path("pages") / page_relative_path.parent
        self.asset_map = asset_map
        self.page_link_map = page_link_map
        self.output: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.output.append(self._render_tag(tag, attrs, self_closing=False))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.output.append(self._render_tag(tag, attrs, self_closing=True))

    def handle_endtag(self, tag: str) -> None:
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(data)

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.output.append(f"<!{decl}>")

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_pi(self, data: str) -> None:
        self.output.append(f"<?{data}>")

    def get_html(self) -> str:
        return "".join(self.output)

    def _render_tag(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> str:
        rendered_attrs: list[str] = []
        for attr_name, attr_value in attrs:
            rewritten_value = self._rewrite_attribute(tag, attr_name, attr_value)
            if rewritten_value is None:
                rendered_attrs.append(attr_name)
            else:
                rendered_attrs.append(f'{attr_name}="{escape(rewritten_value, quote=True)}"')

        joined_attrs = f" {' '.join(rendered_attrs)}" if rendered_attrs else ""
        closing = " /" if self_closing else ""
        return f"<{tag}{joined_attrs}{closing}>"

    def _rewrite_attribute(self, tag: str, attr_name: str, attr_value: str | None) -> str | None:
        if attr_value is None:
            return None

        absolute_url = urllib.parse.urljoin(self.base_url, attr_value)
        if tag == "a" and attr_name == "href":
            local_page = self.page_link_map.get(absolute_url)
            if local_page is not None:
                return self._relative_reference(Path("pages") / local_page, absolute_url)

        if attr_name in RESOURCE_ATTRIBUTES:
            local_asset = self.asset_map.get(absolute_url)
            if local_asset is not None:
                return self._relative_reference(Path("assets") / local_asset, absolute_url)

        return attr_value

    def _relative_reference(self, target_path: Path, source_url: str) -> str:
        rel = os.path.relpath(str(target_path), start=str(self.current_dir))
        parsed = urllib.parse.urlparse(source_url)
        suffix = f"#{parsed.fragment}" if parsed.fragment else ""
        return rel.replace("\\", "/") + suffix


class RemoteAssetReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.remote_assets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name: value for name, value in attrs}
        for attr_name, attr_value in attrs:
            if not attr_value or attr_name not in RESOURCE_ATTRIBUTES:
                continue
            parsed = urllib.parse.urlparse(attr_value)
            if parsed.scheme not in {"http", "https"}:
                continue

            if tag == "a":
                continue
            if tag == "link" and (attrs_dict.get("rel") or "").lower() not in {"stylesheet", "icon", "preload"}:
                continue
            self.remote_assets.add(attr_value)


@dataclass(slots=True)
class CoordinatorDecision:
    profile: SiteProfile
    notes: list[str]


class FailureDiagnosisAgent:
    name = "failure_diagnosis"

    def classify(self, url: str, depth: int, stage: str, attempt: int, exc: Exception) -> FailureRecord:
        category = "unexpected_error"
        retryable = False
        message = str(exc) or exc.__class__.__name__

        if isinstance(exc, urllib.error.HTTPError):
            status = exc.code
            if status in {401, 403}:
                category = "auth_required"
                retryable = False
            elif status == 404:
                category = "not_found"
                retryable = False
            elif status == 429:
                category = "rate_limited"
                retryable = True
            elif 500 <= status <= 599:
                category = "server_error"
                retryable = True
            else:
                category = f"http_{status}"
                retryable = status >= 500
            message = f"HTTP {status}: {exc.reason}"
        elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            category = "timeout"
            retryable = True
        elif isinstance(exc, urllib.error.URLError):
            reason = str(exc.reason)
            lowered = reason.lower()
            if "timed out" in lowered or "timeout" in lowered:
                category = "timeout"
                retryable = True
            elif "refused" in lowered or "reset" in lowered or "unreachable" in lowered:
                category = "network_error"
                retryable = True
            else:
                category = "network_error"
                retryable = True
            message = f"URL error: {reason}"
        elif isinstance(exc, PermissionError):
            category = "io_error"
            retryable = False
        elif isinstance(exc, OSError):
            category = "io_error"
            retryable = False

        return FailureRecord(
            url=url,
            depth=depth,
            stage=stage,
            category=category,
            message=message,
            attempts=attempt + 1,
            retryable=retryable,
        )

    async def enrich(self, config: CrawlConfig, failure: FailureRecord, exc: Exception) -> FailureRecord:
        if not config.use_llm_failure_diagnosis:
            return failure

        if config.llm_provider not in {"deepseek", "openai"}:
            enriched = self._copy_failure(failure)
            enriched.guidance.append(f"unsupported llm provider {config.llm_provider!r}; kept heuristic diagnosis")
            return enriched

        try:
            llm_result = await asyncio.to_thread(self._call_llm_failure_diagnosis, config, failure, exc)
        except ModuleNotFoundError:
            enriched = self._copy_failure(failure)
            enriched.guidance.append("openai package not installed; kept heuristic diagnosis")
            return enriched
        except EnvironmentError as llm_exc:
            enriched = self._copy_failure(failure)
            enriched.guidance.append(f"{llm_exc}; kept heuristic diagnosis")
            return enriched
        except Exception as llm_exc:
            enriched = self._copy_failure(failure)
            enriched.guidance.append(f"llm failure diagnosis request failed: {llm_exc}; kept heuristic diagnosis")
            return enriched

        enriched = self._copy_failure(failure)
        enriched.diagnosis_source = "llm"

        summary = str(llm_result.get("summary", "")).strip()
        if summary:
            enriched.analysis = summary

        guidance = [
            str(item).strip()
            for item in llm_result.get("suggested_actions", [])
            if str(item).strip()
        ]
        if guidance:
            enriched.guidance.extend(guidance)

        confidence = str(llm_result.get("confidence", "unknown")).strip() or "unknown"
        enriched.guidance.append(
            f"llm_failure_diagnosis provider={config.llm_provider} model={config.llm_model} confidence={confidence}"
        )
        return enriched

    def _call_llm_failure_diagnosis(
        self,
        config: CrawlConfig,
        failure: FailureRecord,
        exc: Exception,
    ) -> dict[str, Any]:
        client = build_openai_compatible_client(config)
        payload = {
            "url": failure.url,
            "depth": failure.depth,
            "stage": failure.stage,
            "category": failure.category,
            "message": failure.message,
            "attempts": failure.attempts,
            "retryable": failure.retryable,
            "exception_type": exc.__class__.__name__,
            "provider": config.llm_provider,
            "start_url": config.start_url,
            "cookie_present": bool(config.cookie_header),
            "storage_state_present": bool(config.storage_state_path),
            "prefer_browser": config.prefer_browser,
        }
        prompt = "\n".join(
            [
                "You are a failure diagnosis agent for a website mirroring system.",
                "You will receive a final failure after deterministic retries have finished.",
                "Explain the likely cause and suggest concrete next actions for the operator.",
                "Return strict JSON with keys: summary (string), confidence (low|medium|high), suggested_actions (array of short strings).",
                "Do not include markdown fences or any extra text.",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a failure diagnosis agent. Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=config.llm_max_output_tokens,
        )
        response_text = response.choices[0].message.content if response.choices else ""
        if not response_text:
            raise ValueError("empty response from llm failure diagnosis request")

        return load_llm_json(response_text, "llm failure diagnosis")

    def _copy_failure(self, failure: FailureRecord) -> FailureRecord:
        return FailureRecord(
            url=failure.url,
            depth=failure.depth,
            stage=failure.stage,
            category=failure.category,
            message=failure.message,
            attempts=failure.attempts,
            retryable=failure.retryable,
            diagnosis_source=failure.diagnosis_source,
            analysis=failure.analysis,
            guidance=list(failure.guidance),
        )


class SiteProfilerAgent:
    name = "site_profiler"

    async def run(self, config: CrawlConfig) -> SiteProfile:
        parsed = urllib.parse.urlparse(config.start_url)
        reasons = ["defaulting to browser-first strategy for layout preservation"]
        browser_required = config.prefer_browser
        site_type = "general_html"

        if parsed.path.endswith((".xml", ".json", ".txt")):
            browser_required = False
            reasons = ["non-HTML resource detected from URL path"]
            site_type = "non_html_resource"

        profile = SiteProfile(
            start_url=config.start_url,
            browser_required=browser_required,
            reasons=reasons,
            site_type=site_type,
            interaction_hints=[],
            profile_source="heuristic",
            strategy_source="heuristic",
        )
        if not config.use_llm_site_profile:
            return profile

        if config.llm_provider not in {"deepseek", "openai"}:
            profile.reasons.append(f"unsupported llm provider {config.llm_provider!r}; using heuristic site profile")
            return profile

        try:
            llm_profile = await asyncio.to_thread(self._call_llm_site_profile, config, profile)
        except ModuleNotFoundError:
            profile.reasons.append("openai package not installed; using heuristic site profile")
            return profile
        except EnvironmentError as exc:
            profile.reasons.append(f"{exc}; using heuristic site profile")
            return profile
        except Exception as exc:
            profile.reasons.append(f"llm site profile request failed: {exc}; using heuristic site profile")
            return profile

        merged_reasons = profile.reasons.copy()
        summary = str(llm_profile.get("summary", "")).strip()
        if summary:
            merged_reasons.append(summary)
        confidence = str(llm_profile.get("confidence", "unknown")).strip() or "unknown"
        hints = [
            str(item).strip()
            for item in llm_profile.get("interaction_hints", [])
            if str(item).strip()
        ]
        merged_reasons.append(f"llm site profile confidence={confidence}")
        return SiteProfile(
            start_url=profile.start_url,
            browser_required=bool(llm_profile.get("browser_required", profile.browser_required)),
            reasons=merged_reasons,
            site_type=str(llm_profile.get("site_type", profile.site_type)).strip() or profile.site_type,
            interaction_hints=hints,
            profile_source="llm",
            strategy_source=profile.strategy_source,
        )

    def _call_llm_site_profile(self, config: CrawlConfig, profile: SiteProfile) -> dict[str, Any]:
        client = build_openai_compatible_client(config)
        content_type, excerpt, final_url = self._fetch_profile_excerpt(config)
        payload = {
            "start_url": config.start_url,
            "final_url": final_url,
            "content_type": content_type,
            "heuristic_site_type": profile.site_type,
            "heuristic_browser_required": profile.browser_required,
            "heuristic_reasons": profile.reasons,
            "cookie_present": bool(config.cookie_header),
            "storage_state_present": bool(config.storage_state_path),
            "html_excerpt": excerpt[: max(1000, config.llm_site_profile_html_chars)],
        }
        prompt = "\n".join(
            [
                "You are a site profiling agent for a website mirroring system.",
                "Classify the site and infer whether browser rendering is likely required.",
                "Suggest short interaction hints only when clearly justified.",
                "Return strict JSON with keys: site_type (string), browser_required (boolean), confidence (low|medium|high), summary (string), interaction_hints (array of short strings).",
                "Do not include markdown fences or any extra text.",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a site profiling agent. Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=config.llm_max_output_tokens,
        )
        response_text = response.choices[0].message.content if response.choices else ""
        if not response_text:
            raise ValueError("empty response from llm site profile request")

        return load_llm_json(response_text, "llm site profile")

    def _fetch_profile_excerpt(self, config: CrawlConfig) -> tuple[str | None, str, str]:
        request = urllib.request.Request(config.start_url, headers=build_request_headers(config))
        with urllib.request.urlopen(request, timeout=config.request_timeout_seconds) as response:
            content_type = response.headers.get("content-type")
            final_url = response.geturl()
            excerpt = response.read(max(1000, config.llm_site_profile_html_chars) * 2).decode(
                "utf-8",
                errors="replace",
            )
        return content_type, excerpt, final_url


class SeedDiscoveryAgent:
    name = "seed_discovery"

    async def run(
        self,
        config: CrawlConfig,
        page_url: str,
        depth: int,
        html: str,
        discovered_links: set[str],
    ) -> tuple[set[str], str | None]:
        if not config.use_llm_seed_discovery or depth != 0:
            return set(), None

        if config.llm_provider not in {"deepseek", "openai"}:
            return set(), f"unsupported llm provider {config.llm_provider!r}; skipped llm seed discovery"

        try:
            llm_result = await asyncio.to_thread(
                self._call_llm_seed_discovery,
                config,
                page_url,
                html,
                discovered_links,
            )
        except ModuleNotFoundError:
            return set(), "openai package not installed; skipped llm seed discovery"
        except EnvironmentError as exc:
            return set(), f"{exc}; skipped llm seed discovery"
        except Exception as exc:
            return set(), f"llm seed discovery request failed: {exc}; skipped llm seed discovery"

        raw_candidates = llm_result.get("candidate_urls", [])
        summary = str(llm_result.get("summary", "")).strip() or None
        candidates: set[str] = set()
        for candidate in raw_candidates[: max(1, config.llm_seed_candidate_limit)]:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            absolute = urllib.parse.urljoin(page_url, candidate.strip())
            parsed = urllib.parse.urlparse(absolute)
            if parsed.scheme not in {"http", "https"}:
                continue
            if config.same_origin_only and parsed.netloc.lower() != urllib.parse.urlparse(config.start_url).netloc.lower():
                continue
            candidates.add(absolute)

        if summary:
            summary = (
                f"llm_seed_discovery provider={config.llm_provider} model={config.llm_model}: "
                f"{summary} (candidates={len(candidates)})"
            )
        return candidates, summary

    def _call_llm_seed_discovery(
        self,
        config: CrawlConfig,
        page_url: str,
        html: str,
        discovered_links: set[str],
    ) -> dict[str, Any]:
        client = build_openai_compatible_client(config)
        link_sample = sorted(discovered_links)[:50]
        payload = {
            "start_url": config.start_url,
            "page_url": page_url,
            "same_origin_only": config.same_origin_only,
            "already_discovered_links": link_sample,
            "html_excerpt": html[: max(1200, config.llm_seed_html_chars)],
            "candidate_limit": config.llm_seed_candidate_limit,
        }
        prompt = "\n".join(
            [
                "You are a seed discovery agent for a website mirroring system.",
                "Infer additional same-site entry URLs that are likely useful to crawl, such as section indexes, pagination roots, archive pages, or hidden navigation endpoints.",
                "Prefer URLs that are strongly implied by the homepage excerpt and existing link patterns.",
                "Do not invent domains. Return only same-site paths or absolute URLs.",
                "Return strict JSON with keys: summary (string), confidence (low|medium|high), candidate_urls (array of strings).",
                "Do not include markdown fences or any extra text.",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a seed discovery agent. Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=config.llm_max_output_tokens,
        )
        response_text = response.choices[0].message.content if response.choices else ""
        if not response_text:
            raise ValueError("empty response from llm seed discovery request")

        return load_llm_json(response_text, "llm seed discovery")


class StrategyAgent:
    name = "strategy"

    async def run(self, config: CrawlConfig, profile: SiteProfile) -> CoordinatorDecision:
        notes = profile.reasons.copy()

        if not config.use_llm_strategy:
            return CoordinatorDecision(profile=profile, notes=notes)

        if config.llm_provider not in {"deepseek", "openai"}:
            notes.append(f"unsupported llm provider {config.llm_provider!r}; using heuristic strategy")
            return CoordinatorDecision(profile=profile, notes=notes)

        try:
            llm_decision = await asyncio.to_thread(self._call_openai_compatible_strategy, config, profile)
        except ModuleNotFoundError:
            notes.append("openai package not installed; using heuristic strategy")
            return CoordinatorDecision(profile=profile, notes=notes)
        except EnvironmentError as exc:
            notes.append(f"{exc}; using heuristic strategy")
            return CoordinatorDecision(profile=profile, notes=notes)
        except Exception as exc:
            notes.append(f"llm strategy request failed: {exc}; using heuristic strategy")
            return CoordinatorDecision(profile=profile, notes=notes)

        browser_required = bool(llm_decision.get("browser_required", profile.browser_required))
        if not config.prefer_browser and browser_required:
            browser_required = False
            notes.append("llm requested browser rendering, but --no-browser is enabled")

        llm_reason = str(llm_decision.get("reason", "")).strip()
        confidence = str(llm_decision.get("confidence", "unknown")).strip() or "unknown"
        operator_notes = [
            str(item).strip()
            for item in llm_decision.get("operator_notes", [])
            if str(item).strip()
        ]

        merged_reasons = profile.reasons.copy()
        if llm_reason:
            merged_reasons.append(f"llm strategy ({confidence}): {llm_reason}")
        merged_reasons.extend(operator_notes)

        refined_profile = SiteProfile(
            start_url=profile.start_url,
            browser_required=browser_required,
            reasons=merged_reasons,
            strategy_source="llm",
        )
        notes = refined_profile.reasons.copy()
        notes.append(
            f"llm_strategy provider={config.llm_provider} model={config.llm_model} browser_required={str(browser_required).lower()} confidence={confidence}"
        )
        return CoordinatorDecision(profile=refined_profile, notes=notes)

    def _call_openai_compatible_strategy(self, config: CrawlConfig, profile: SiteProfile) -> dict[str, Any]:
        client = build_openai_compatible_client(config)
        payload = {
            "start_url": config.start_url,
            "provider": config.llm_provider,
            "same_origin_only": config.same_origin_only,
            "prefer_browser": config.prefer_browser,
            "max_depth": config.max_depth,
            "max_pages": config.max_pages,
            "cookie_present": bool(config.cookie_header),
            "storage_state_present": bool(config.storage_state_path),
            "heuristic_browser_required": profile.browser_required,
            "profile_source": profile.profile_source,
            "site_type": profile.site_type,
            "interaction_hints": profile.interaction_hints,
            "heuristic_reasons": profile.reasons,
        }
        prompt = "\n".join(
            [
                "You are a crawl strategy agent for a website mirroring system.",
                "Decide whether browser rendering is required before the crawl starts.",
                "Prefer stability and layout preservation over aggressive optimization.",
                "Return strict JSON with keys: browser_required (boolean), confidence (low|medium|high), reason (string), operator_notes (array of short strings).",
                "Do not include markdown fences or any extra text.",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a crawl strategy agent. Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=config.llm_max_output_tokens,
        )
        response_text = response.choices[0].message.content if response.choices else ""
        if not response_text:
            raise ValueError("empty response from llm strategy request")

        llm_decision = load_llm_json(response_text, "llm strategy")
        if "browser_required" not in llm_decision:
            raise ValueError("llm strategy response missing browser_required")
        return llm_decision


class InteractionPlannerAgent:
    name = "interaction_planner"

    async def run(
        self,
        config: CrawlConfig,
        page_url: str,
        depth: int,
        html: str,
    ) -> tuple[list[dict[str, str]], str | None]:
        if not config.use_llm_interaction_planner:
            return [], None
        if depth > config.llm_interaction_max_depth:
            return [], None
        if config.llm_provider not in {"deepseek", "openai"}:
            return [], f"unsupported llm provider {config.llm_provider!r}; skipped interaction planner"

        try:
            result = await asyncio.to_thread(self._call_llm_interaction_plan, config, page_url, html)
        except ModuleNotFoundError:
            return [], "openai package not installed; skipped interaction planner"
        except EnvironmentError as exc:
            return [], f"{exc}; skipped interaction planner"
        except Exception as exc:
            return [], f"llm interaction planner request failed: {exc}; skipped interaction planner"

        raw_actions = result.get("actions", [])
        summary = str(result.get("summary", "")).strip() or None
        click_candidates = extract_safe_interaction_candidates(html, config.llm_interaction_action_limit * 3)
        actions = self._sanitize_actions(raw_actions, config, click_candidates)
        if summary:
            summary = (
                f"llm_interaction_planner provider={config.llm_provider} model={config.llm_model}: "
                f"{summary} (actions={len(actions)})"
            )
        return actions, summary

    def _call_llm_interaction_plan(
        self,
        config: CrawlConfig,
        page_url: str,
        html: str,
    ) -> dict[str, Any]:
        client = build_openai_compatible_client(config)
        click_candidates = extract_safe_interaction_candidates(html, config.llm_interaction_action_limit * 3)
        payload = {
            "page_url": page_url,
            "html_excerpt": html[: max(1200, config.llm_interaction_html_chars)],
            "action_limit": config.llm_interaction_action_limit,
            "allowed_action_types": ["scroll_bottom", "click_text"],
            "visible_click_candidates": click_candidates,
        }
        prompt = "\n".join(
            [
                "You are an interaction planning agent for a website mirroring system.",
                "Suggest only a few low-risk browser interactions that may reveal more same-page content before the HTML snapshot is saved.",
                "Allowed action types: scroll_bottom, click_text.",
                "Use click_text only for clearly safe controls such as more/next/expand/load more, and prefer the exact strings listed in visible_click_candidates.",
                "If no safe click candidate is listed, prefer scroll_bottom only.",
                "Never suggest login, submit, delete, buy, or navigation to external pages.",
                "Return strict JSON with keys: summary (string), confidence (low|medium|high), actions (array of objects with type and text keys).",
                "For scroll_bottom actions, omit text or leave it empty.",
                "Do not include markdown fences or any extra text.",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are an interaction planner. Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=config.llm_max_output_tokens,
        )
        response_text = response.choices[0].message.content if response.choices else ""
        if not response_text:
            raise ValueError("empty response from llm interaction planner request")

        return load_llm_json(response_text, "llm interaction planner")

    def _sanitize_actions(
        self,
        raw_actions: Any,
        config: CrawlConfig,
        click_candidates: list[str],
    ) -> list[dict[str, str]]:
        if not isinstance(raw_actions, list):
            return []

        actions: list[dict[str, str]] = []
        seen_actions: set[tuple[str, str]] = set()
        for raw_action in raw_actions[: max(1, config.llm_interaction_action_limit)]:
            if not isinstance(raw_action, dict):
                continue
            action_type = str(raw_action.get("type", "")).strip()
            action_text = str(raw_action.get("text", "")).strip()
            if action_type == "scroll_bottom":
                dedupe_key = ("scroll_bottom", "")
                if dedupe_key in seen_actions:
                    continue
                seen_actions.add(dedupe_key)
                actions.append({"type": "scroll_bottom", "text": ""})
                continue
            if action_type != "click_text":
                continue
            if not action_text:
                continue
            matched_candidate = resolve_click_candidate(action_text, click_candidates)
            if not matched_candidate:
                continue
            dedupe_key = ("click_text", matched_candidate)
            if dedupe_key in seen_actions:
                continue
            seen_actions.add(dedupe_key)
            actions.append({"type": "click_text", "text": matched_candidate})
        return actions


class RendererAgent:
    name = "renderer"

    async def run(self, url: str, config: CrawlConfig, browser_required: bool, depth: int = 0) -> PageSnapshot:
        if browser_required:
            try:
                return await self._render_with_playwright(url, config, depth=depth)
            except ModuleNotFoundError:
                pass
            except Exception:
                pass

        return await asyncio.to_thread(self._render_with_urllib, url, config)

    async def _render_with_playwright(self, url: str, config: CrawlConfig, depth: int = 0) -> PageSnapshot:
        from playwright.async_api import async_playwright

        discovered_assets: set[str] = set()
        network_log: list[dict[str, Any]] = []
        interaction_planner = InteractionPlannerAgent()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context_kwargs: dict[str, Any] = {
                "user_agent": config.user_agent,
            }
            if config.extra_headers:
                context_kwargs["extra_http_headers"] = config.extra_headers
            if config.storage_state_path is not None:
                context_kwargs["storage_state"] = str(config.storage_state_path)

            context = await browser.new_context(**context_kwargs)
            if config.cookie_header:
                cookies = cookie_header_to_playwright_cookies(config.cookie_header, url)
                if cookies:
                    await context.add_cookies(cookies)

            page = await context.new_page()

            def handle_response(response) -> None:
                request = response.request
                entry = {
                    "url": response.url,
                    "resource_type": request.resource_type,
                    "status": response.status,
                    "content_type": response.headers.get("content-type"),
                }
                network_log.append(entry)
                parsed = urllib.parse.urlparse(response.url)
                if request.resource_type in NETWORK_ASSET_TYPES and parsed.scheme in {"http", "https"}:
                    discovered_assets.add(response.url)

            page.on("response", handle_response)
            response = await page.goto(url, wait_until="networkidle")
            plan_html = await page.content()
            actions, planner_note = await interaction_planner.run(
                config=config,
                page_url=page.url,
                depth=depth,
                html=plan_html,
            )
            if planner_note:
                network_log.append(
                    {
                        "type": "interaction_planner",
                        "message": planner_note,
                    }
                )
            for action in actions:
                await self._execute_interaction_action(page, action, network_log)
            html = await page.content()
            final_url = page.url
            status_code = response.status if response else None
            content_type = response.headers.get("content-type") if response else None
            await context.close()
            await browser.close()

        return PageSnapshot(
            url=url,
            final_url=final_url,
            html=html,
            status_code=status_code,
            used_browser=True,
            content_type=content_type,
            discovered_assets=sorted(discovered_assets),
            network_log=network_log,
        )

    async def _execute_interaction_action(self, page: Any, action: dict[str, str], network_log: list[dict[str, Any]]) -> None:
        action_type = action.get("type", "")
        action_text = action.get("text", "")
        try:
            if action_type == "scroll_bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1200)
            elif action_type == "click_text":
                locator = page.get_by_text(action_text, exact=False).first
                await locator.click(timeout=2000)
                await page.wait_for_timeout(1200)
            else:
                return
            network_log.append(
                {
                    "type": "interaction_action",
                    "action_type": action_type,
                    "text": action_text,
                    "status": "ok",
                }
            )
        except Exception as exc:
            network_log.append(
                {
                    "type": "interaction_action",
                    "action_type": action_type,
                    "text": action_text,
                    "status": "failed",
                    "message": str(exc),
                }
            )

    def _render_with_urllib(self, url: str, config: CrawlConfig) -> PageSnapshot:
        request = urllib.request.Request(
            url,
            headers=build_request_headers(config),
        )
        with urllib.request.urlopen(request, timeout=config.request_timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
            final_url = response.geturl()
            status_code = getattr(response, "status", None)
            content_type = response.headers.get("content-type")

        return PageSnapshot(
            url=url,
            final_url=final_url,
            html=html,
            status_code=status_code,
            used_browser=False,
            content_type=content_type,
            discovered_assets=[],
            network_log=[],
        )


class CrawlerAgent:
    name = "crawler"

    async def run(
        self,
        page_url: str,
        html: str,
        config: CrawlConfig,
        discovered_assets: list[str] | None = None,
    ) -> tuple[set[str], set[str]]:
        parser = LinkAndAssetParser(page_url)
        parser.feed(html)
        parser.close()

        asset_candidates = set(parser.assets)
        if discovered_assets:
            asset_candidates.update(discovered_assets)

        if not config.same_origin_only:
            return parser.links, asset_candidates

        site_key = self._site_key(config.start_url)
        filtered_links = {link for link in parser.links if self._site_key(link) == site_key}
        filtered_assets = {asset for asset in asset_candidates if self._site_key(asset) == site_key}
        return filtered_links, filtered_assets

    @staticmethod
    def _site_key(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc.lower()


class AssetFetcherAgent:
    name = "asset_fetcher"

    async def run(
        self,
        assets: set[str],
        asset_dir: Path,
        config: CrawlConfig,
    ) -> tuple[dict[str, str], list[str]]:
        asset_map: dict[str, str] = {}
        asset_content_types: dict[str, str] = {}
        failures: list[str] = []
        semaphore = asyncio.Semaphore(config.asset_concurrency)

        async def download_one(asset_url: str) -> None:
            async with semaphore:
                try:
                    relative_path, content_type = await asyncio.to_thread(
                        self._download_asset,
                        asset_url,
                        asset_dir,
                        config,
                    )
                    asset_map[asset_url] = relative_path
                    asset_content_types[asset_url] = content_type
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    failures.append(f"{asset_url} :: {exc}")

        await asyncio.gather(*(download_one(asset_url) for asset_url in sorted(assets)))
        pending_css = [
            asset_url
            for asset_url, content_type in asset_content_types.items()
            if self._is_css_asset(asset_url, content_type)
        ]
        processed_css: set[str] = set()

        while pending_css:
            css_url = pending_css.pop(0)
            if css_url in processed_css or css_url not in asset_map:
                continue
            processed_css.add(css_url)

            css_path = asset_dir / asset_map[css_url]
            try:
                nested_assets = await asyncio.to_thread(self._extract_css_dependencies, css_path, css_url)
            except OSError as exc:
                failures.append(f"{css_url} :: {exc}")
                continue

            nested_to_download = [
                nested_url
                for nested_url in sorted(nested_assets)
                if nested_url not in asset_map and self._should_download_asset(nested_url, config)
            ]
            if nested_to_download:
                await asyncio.gather(*(download_one(nested_url) for nested_url in nested_to_download))
                for nested_url in nested_to_download:
                    if self._is_css_asset(nested_url, asset_content_types.get(nested_url, "")):
                        pending_css.append(nested_url)

            try:
                await asyncio.to_thread(self._rewrite_css_file, css_path, css_url, asset_map)
            except OSError as exc:
                failures.append(f"{css_url} :: {exc}")

        return asset_map, failures

    def _download_asset(self, asset_url: str, asset_dir: Path, config: CrawlConfig) -> tuple[str, str]:
        request = urllib.request.Request(asset_url, headers=build_request_headers(config))
        with urllib.request.urlopen(request, timeout=config.request_timeout_seconds) as response:
            payload = response.read()
            content_type = response.headers.get("content-type", "").split(";")[0].strip()

        parsed = urllib.parse.urlparse(asset_url)
        suffix = Path(parsed.path).suffix.lower()
        if not suffix and content_type:
            suffix = mimetypes.guess_extension(content_type) or ""

        stem = Path(parsed.path).name or "index"
        if not Path(stem).suffix and suffix:
            stem = f"{stem}{suffix}"

        hash_prefix = hashlib.sha1(asset_url.encode("utf-8")).hexdigest()[:12]
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", stem)
        target = asset_dir / f"{hash_prefix}_{safe_name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target.name, content_type

    def _extract_css_dependencies(self, css_path: Path, css_url: str) -> set[str]:
        css_text = css_path.read_text(encoding="utf-8", errors="replace")
        dependencies: set[str] = set()
        for pattern in (CSS_IMPORT_PATTERN, CSS_URL_PATTERN):
            for match in pattern.finditer(css_text):
                normalized_url = self._normalize_css_reference(match.group("url"), css_url)
                if normalized_url is not None:
                    dependencies.add(normalized_url)
        return dependencies

    def _rewrite_css_file(self, css_path: Path, css_url: str, asset_map: dict[str, str]) -> None:
        css_text = css_path.read_text(encoding="utf-8", errors="replace")

        def replace_import(match: re.Match[str]) -> str:
            normalized_url = self._normalize_css_reference(match.group("url"), css_url)
            if normalized_url is None or normalized_url not in asset_map:
                return match.group(0)

            local_reference = self._css_relative_reference(css_path, asset_map[normalized_url], normalized_url)
            tail = match.group("tail") or ""
            return f'@import url("{local_reference}"){tail};'

        def replace_url(match: re.Match[str]) -> str:
            normalized_url = self._normalize_css_reference(match.group("url"), css_url)
            if normalized_url is None or normalized_url not in asset_map:
                return match.group(0)

            quote = match.group("quote") or ""
            local_reference = self._css_relative_reference(css_path, asset_map[normalized_url], normalized_url)
            return f"url({quote}{local_reference}{quote})"

        rewritten = CSS_IMPORT_PATTERN.sub(replace_import, css_text)
        rewritten = CSS_URL_PATTERN.sub(replace_url, rewritten)
        css_path.write_text(rewritten, encoding="utf-8")

    def _normalize_css_reference(self, raw_reference: str, css_url: str) -> str | None:
        cleaned_reference = raw_reference.strip().strip("\"'")
        if not cleaned_reference:
            return None
        lowered = cleaned_reference.lower()
        if lowered.startswith(("data:", "javascript:", "#")):
            return None

        absolute_url = urllib.parse.urljoin(css_url, cleaned_reference)
        parsed = urllib.parse.urlparse(absolute_url)
        if parsed.scheme not in {"http", "https"}:
            return None
        return absolute_url

    def _css_relative_reference(self, css_path: Path, local_asset_name: str, source_url: str) -> str:
        rel = os.path.relpath(str(css_path.parent / local_asset_name), start=str(css_path.parent))
        parsed = urllib.parse.urlparse(source_url)
        suffix = f"#{parsed.fragment}" if parsed.fragment else ""
        return rel.replace("\\", "/") + suffix

    def _is_css_asset(self, asset_url: str, content_type: str) -> bool:
        return content_type.startswith("text/css") or Path(urllib.parse.urlparse(asset_url).path).suffix.lower() == ".css"

    def _should_download_asset(self, asset_url: str, config: CrawlConfig) -> bool:
        parsed = urllib.parse.urlparse(asset_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not config.same_origin_only:
            return True
        start = urllib.parse.urlparse(config.start_url)
        return parsed.netloc.lower() == start.netloc.lower()


class RewriterAgent:
    name = "rewriter"

    async def run(
        self,
        html: str,
        base_url: str,
        page_relative_path: Path,
        asset_map: dict[str, str],
        page_link_map: dict[str, Path],
    ) -> str:
        parser = HTMLRewriterParser(
            base_url=base_url,
            page_relative_path=page_relative_path,
            asset_map=asset_map,
            page_link_map=page_link_map,
        )
        parser.feed(html)
        parser.close()
        return parser.get_html()


class ValidatorAgent:
    name = "validator"

    async def run(self, html: str, asset_map: dict[str, str], base_url: str = "") -> list[str]:
        issues: list[str] = []
        if "<html" not in html.lower():
            issues.append("page content does not look like full HTML")

        parser = RemoteAssetReferenceParser()
        parser.feed(html)
        parser.close()
        unresolved_remote_assets = sorted(asset for asset in parser.remote_assets if asset not in asset_map)
        if unresolved_remote_assets:
            issues.append(f"{len(unresolved_remote_assets)} remote asset reference(s) were not localized")
        return issues

    async def enrich(
        self,
        config: CrawlConfig,
        page_url: str,
        depth: int,
        html: str,
        issues: list[str],
    ) -> tuple[str, str | None, list[str]]:
        if not config.use_llm_validation:
            return "heuristic", None, []

        should_audit = depth == 0 or bool(issues)
        if not should_audit:
            return "heuristic", None, []

        if config.llm_provider not in {"deepseek", "openai"}:
            return "heuristic", None, [f"unsupported llm provider {config.llm_provider!r}; skipped llm validation"]

        try:
            result = await asyncio.to_thread(self._call_llm_validation, config, page_url, depth, html, issues)
        except ModuleNotFoundError:
            return "heuristic", None, ["openai package not installed; skipped llm validation"]
        except EnvironmentError as exc:
            return "heuristic", None, [f"{exc}; skipped llm validation"]
        except Exception as exc:
            return "heuristic", None, [f"llm validation request failed: {exc}; skipped llm validation"]

        summary = str(result.get("summary", "")).strip() or None
        guidance = [
            str(item).strip()
            for item in result.get("suggested_actions", [])
            if str(item).strip()
        ]
        confidence = str(result.get("confidence", "unknown")).strip() or "unknown"
        guidance.append(
            f"llm_validation provider={config.llm_provider} model={config.llm_model} confidence={confidence}"
        )
        return "llm", summary, guidance

    def _call_llm_validation(
        self,
        config: CrawlConfig,
        page_url: str,
        depth: int,
        html: str,
        issues: list[str],
    ) -> dict[str, Any]:
        client = build_openai_compatible_client(config)
        title = self._extract_title(html)
        excerpt = html[: max(1000, config.llm_validation_html_chars)]
        payload = {
            "url": page_url,
            "depth": depth,
            "title": title,
            "heuristic_issues": issues,
            "html_excerpt": excerpt,
        }
        prompt = "\n".join(
            [
                "You are an offline mirror quality auditor.",
                "Review the provided page excerpt and heuristic issues.",
                "Decide whether the offline mirror likely preserved the main page content correctly.",
                "Return strict JSON with keys: summary (string), confidence (low|medium|high), suggested_actions (array of short strings).",
                "Do not include markdown fences or any extra text.",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are an offline mirror quality auditor. Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=config.llm_max_output_tokens,
        )
        response_text = response.choices[0].message.content if response.choices else ""
        if not response_text:
            raise ValueError("empty response from llm validation request")

        return load_llm_json(response_text, "llm validation")

    def _extract_title(self, html: str) -> str:
        match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip()


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

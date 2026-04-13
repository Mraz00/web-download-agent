from __future__ import annotations

import asyncio
import hashlib
import os
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

from .agents import (
    AssetFetcherAgent,
    CoordinatorDecision,
    CrawlerAgent,
    FailureDiagnosisAgent,
    RendererAgent,
    RewriterAgent,
    SeedDiscoveryAgent,
    SiteProfilerAgent,
    StrategyAgent,
    ValidatorAgent,
    save_json,
)
from .models import CrawlConfig, JobManifest, PageResult, PageTask


class MirrorPipeline:
    def __init__(self) -> None:
        self.site_profiler = SiteProfilerAgent()
        self.renderer = RendererAgent()
        self.crawler = CrawlerAgent()
        self.asset_fetcher = AssetFetcherAgent()
        self.rewriter = RewriterAgent()
        self.validator = ValidatorAgent()
        self.failure_diagnosis = FailureDiagnosisAgent()
        self.strategy_agent = StrategyAgent()
        self.seed_discovery_agent = SeedDiscoveryAgent()

    async def run(self, config: CrawlConfig) -> JobManifest:
        job_dir = self._build_job_dir(config)
        pages_dir = job_dir / "pages"
        assets_dir = job_dir / "assets"
        logs_dir = job_dir / "logs"
        pages_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        profile = await self.site_profiler.run(config)
        decision = await self.strategy_agent.run(config, profile)
        manifest = JobManifest(
            start_url=config.start_url,
            output_dir=str(job_dir),
            notes=decision.notes,
        )

        page_queue: asyncio.Queue[PageTask | None] = asyncio.Queue()
        state_lock = asyncio.Lock()
        scheduled: set[str] = {self._canonicalize_url(config.start_url)}
        processed: set[str] = set()

        await page_queue.put(PageTask(url=config.start_url, depth=0))
        workers = [
            asyncio.create_task(
                self._page_worker(
                    worker_id=index + 1,
                    page_queue=page_queue,
                    state_lock=state_lock,
                    scheduled=scheduled,
                    processed=processed,
                    manifest=manifest,
                    config=config,
                    pages_dir=pages_dir,
                    assets_dir=assets_dir,
                    logs_dir=logs_dir,
                    browser_required=decision.profile.browser_required,
                )
            )
            for index in range(max(1, config.page_worker_count))
        ]

        await page_queue.join()
        for _ in workers:
            await page_queue.put(None)
        await asyncio.gather(*workers)

        self._append_failure_summary(manifest)
        self._write_navigation_index(job_dir, manifest)
        save_json(logs_dir / "manifest.json", manifest.to_dict())
        return manifest

    async def _page_worker(
        self,
        worker_id: int,
        page_queue: asyncio.Queue[PageTask | None],
        state_lock: asyncio.Lock,
        scheduled: set[str],
        processed: set[str],
        manifest: JobManifest,
        config: CrawlConfig,
        pages_dir: Path,
        assets_dir: Path,
        logs_dir: Path,
        browser_required: bool,
    ) -> None:
        while True:
            task = await page_queue.get()
            if task is None:
                page_queue.task_done()
                break

            canonical_url = self._canonicalize_url(task.url)
            try:
                async with state_lock:
                    if canonical_url in processed:
                        continue

                snapshot = await self.renderer.run(
                    url=task.url,
                    config=config,
                    browser_required=browser_required,
                    depth=task.depth,
                )
                normalized_final_url = self._canonicalize_url(snapshot.final_url)
                page_relative_path = self._page_relative_path(normalized_final_url)
                links, assets = await self.crawler.run(
                    snapshot.final_url,
                    snapshot.html,
                    config,
                    discovered_assets=snapshot.discovered_assets,
                )
                extra_seed_links, seed_note = await self.seed_discovery_agent.run(
                    config=config,
                    page_url=snapshot.final_url,
                    depth=task.depth,
                    html=snapshot.html,
                    discovered_links=links,
                )
                page_link_map = {
                    self._canonicalize_url(link): self._page_relative_path(link)
                    for link in links
                }
                asset_map, download_failures = await self.asset_fetcher.run(assets, assets_dir, config)
                rewritten_html = await self.rewriter.run(
                    html=snapshot.html,
                    base_url=snapshot.final_url,
                    page_relative_path=page_relative_path,
                    asset_map=asset_map,
                    page_link_map=page_link_map,
                )
                issues = await self.validator.run(rewritten_html, asset_map, base_url=snapshot.final_url)
                issues.extend(download_failures)
                validation_source, validation_summary, validation_guidance = await self.validator.enrich(
                    config=config,
                    page_url=snapshot.final_url,
                    depth=task.depth,
                    html=rewritten_html,
                    issues=issues,
                )

                page_path = self._write_page(page_relative_path, rewritten_html, pages_dir)
                if config.save_network_log and snapshot.network_log:
                    save_json(
                        logs_dir / f"worker-{worker_id}-{self._safe_log_name(normalized_final_url)}.json",
                        {"url": snapshot.final_url, "events": snapshot.network_log},
                    )

                async with state_lock:
                    if seed_note:
                        manifest.notes.append(seed_note)
                    scheduled.add(normalized_final_url)
                    processed.add(canonical_url)
                    processed.add(normalized_final_url)
                    manifest.pages.append(
                        PageResult(
                            url=snapshot.final_url,
                            saved_path=str(page_path),
                            depth=task.depth,
                            asset_count=len(asset_map),
                            used_browser=snapshot.used_browser,
                            issues=issues,
                            validation_source=validation_source,
                            validation_summary=validation_summary,
                            validation_guidance=validation_guidance,
                        )
                    )
                    if task.depth < config.max_depth and len(scheduled) < config.max_pages:
                        queue_candidates = sorted(links | extra_seed_links)
                        for link in queue_candidates:
                            canonical_link = self._canonicalize_url(link)
                            if canonical_link in scheduled or len(scheduled) >= config.max_pages:
                                continue
                            scheduled.add(canonical_link)
                            await page_queue.put(PageTask(url=canonical_link, depth=task.depth + 1))
            except Exception as exc:
                failure = self.failure_diagnosis.classify(
                    url=task.url,
                    depth=task.depth,
                    stage="page_render",
                    attempt=task.attempt,
                    exc=exc,
                )
                should_retry = failure.retryable and task.attempt < config.page_retry_limit
                if should_retry:
                    await asyncio.sleep(config.retry_backoff_seconds * (task.attempt + 1))
                    await page_queue.put(
                        PageTask(
                            url=task.url,
                            depth=task.depth,
                            attempt=task.attempt + 1,
                        )
                    )
                else:
                    failure = await self.failure_diagnosis.enrich(config, failure, exc)
                    async with state_lock:
                        processed.add(canonical_url)
                        manifest.failures.append(failure)
            finally:
                page_queue.task_done()

    def _build_job_dir(self, config: CrawlConfig) -> Path:
        parsed = urllib.parse.urlparse(config.start_url)
        host = re.sub(r"[^a-zA-Z0-9.-]", "_", parsed.netloc or "site")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return config.output_dir / f"{host}-{stamp}"

    def _write_page(self, page_relative_path: Path, html: str, pages_dir: Path) -> Path:
        target = pages_dir / page_relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        return target

    def _page_relative_path(self, page_url: str) -> Path:
        parsed = urllib.parse.urlparse(page_url)
        clean_path = parsed.path.strip("/")
        query_hash = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:8] if parsed.query else ""

        if not clean_path:
            base = Path("index.html")
        elif Path(clean_path).suffix:
            base = Path(clean_path)
        else:
            base = Path(clean_path) / "index.html"

        safe_parts = [re.sub(r"[^a-zA-Z0-9._-]", "_", part) or "index" for part in base.parts]
        relative = Path(*safe_parts)
        if not query_hash:
            return relative

        if relative.suffix:
            stem = f"{relative.stem}__{query_hash}"
            return relative.with_name(f"{stem}{relative.suffix}")
        return relative / f"index__{query_hash}.html"

    def _canonicalize_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        clean = parsed._replace(fragment="")
        return urllib.parse.urlunparse(clean)

    def _safe_log_name(self, url: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]", "_", url)

    def _append_failure_summary(self, manifest: JobManifest) -> None:
        if not manifest.failures:
            return

        category_counts: dict[str, int] = {}
        for failure in manifest.failures:
            category_counts[failure.category] = category_counts.get(failure.category, 0) + 1

        summary = ", ".join(
            f"{category}={count}"
            for category, count in sorted(category_counts.items())
        )
        manifest.notes.append(f"failure_summary: {summary}")

    def _write_navigation_index(self, job_dir: Path, manifest: JobManifest) -> None:
        page_items: list[str] = []
        for page in sorted(manifest.pages, key=lambda item: (item.depth, item.url)):
            page_path = Path(page.saved_path)
            relative_href = os.path.relpath(page_path, start=job_dir).replace("\\", "/")
            page_items.append(
                (
                    "<li>"
                    f"<a href=\"{relative_href}\">{page.url}</a>"
                    f" <span>(depth={page.depth}, assets={page.asset_count}, browser={str(page.used_browser).lower()})</span>"
                    "</li>"
                )
            )

        failure_items = [
            (
                "<li>"
                f"{failure.url} [{failure.category}] at {failure.stage}: {failure.message}"
                "</li>"
            )
            for failure in manifest.failures
        ]

        html = "\n".join(
            [
                "<!DOCTYPE html>",
                "<html lang=\"zh-CN\">",
                "<head>",
                "  <meta charset=\"utf-8\">",
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
                "  <title>离线镜像导航</title>",
                "  <style>",
                "    :root { color-scheme: light; }",
                "    body { font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; margin: 0; background: #f5f1e8; color: #1f2937; }",
                "    main { max-width: 980px; margin: 0 auto; padding: 32px 20px 56px; }",
                "    h1 { margin-bottom: 8px; }",
                "    .meta { color: #4b5563; margin-bottom: 24px; }",
                "    .panel { background: #fffdf8; border: 1px solid #e5dccb; border-radius: 16px; padding: 20px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(96, 74, 28, 0.08); }",
                "    ul { margin: 0; padding-left: 20px; }",
                "    li { margin: 8px 0; line-height: 1.5; }",
                "    a { color: #8a3b12; text-decoration: none; }",
                "    a:hover { text-decoration: underline; }",
                "    code { background: #f3ead8; padding: 2px 6px; border-radius: 6px; }",
                "  </style>",
                "</head>",
                "<body>",
                "  <main>",
                "    <h1>离线镜像导航</h1>",
                f"    <p class=\"meta\">起始 URL: <code>{manifest.start_url}</code></p>",
                "    <section class=\"panel\">",
                f"      <p>共保存 <strong>{len(manifest.pages)}</strong> 个页面，任务失败 <strong>{len(manifest.failures)}</strong> 项。</p>",
                "      <p>直接从下面的页面列表进入离线镜像，所有链接都基于本地文件路径。</p>",
                "    </section>",
                "    <section class=\"panel\">",
                "      <h2>页面列表</h2>",
                "      <ul>",
                *[f"        {item}" for item in page_items],
                "      </ul>",
                "    </section>",
                "    <section class=\"panel\">",
                "      <h2>失败摘要</h2>",
                "      <ul>",
                *([f"        {item}" for item in failure_items] or ["        <li>无任务级失败。</li>"]),
                "      </ul>",
                "    </section>",
                "  </main>",
                "</body>",
                "</html>",
            ]
        )
        (job_dir / "index.html").write_text(html, encoding="utf-8")


def run_pipeline(config: CrawlConfig) -> JobManifest:
    return asyncio.run(MirrorPipeline().run(config))

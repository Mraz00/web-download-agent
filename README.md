# Website Download Agent MVP

这个项目是一版偏工程化的 MVP：它把 “multi-agent 思路” 落成了可运行的下载流水线，而不是让大模型直接接管所有下载动作。

## 目标

- 输入网站 URL
- 自动下载网页和静态资源
- 尽量保留页面结构和显示效果
- 生成一个可离线查看的镜像目录
- 给后续扩展成真正的多 agent 系统预留模块边界

## 当前模块

- `Coordinator`：负责全局任务编排
- `SiteProfilerAgent`：判断站点更适合浏览器渲染还是普通抓取
- `RendererAgent`：获取页面 HTML，优先用 Playwright 渲染，并采集运行时网络资源
- `CrawlerAgent`：发现站内链接和静态资源
- `AssetFetcherAgent`：并发下载图片、CSS、JS、字体等资源
- `RewriterAgent`：把页面里的资源路径和站内链接改成本地路径
- `ValidatorAgent`：做基础完整性检查
- `StrategyAgent`：可选调用 LLM，在任务启动前细化抓取策略

## 目录结构

```text
output/
  jobs/
    <host>-<timestamp>/
      pages/
      assets/
      logs/
      manifest.json
```

## 运行

先安装项目：

```bash
pip install -e .
```

如果你要支持 JS 渲染页面，再安装浏览器能力：

```bash
pip install -e .[browser]
playwright install chromium
```

如果你要启用 LLM 策略判断，再安装 LLM 依赖：

```bash
pip install -e .[llm]
```

启动一个下载任务：

```bash
site-mirror-agent --url https://example.com --output-dir ./output/jobs --max-depth 1
```

或者：

```bash
python -m web_download_agent.cli --url https://example.com --output-dir ./output/jobs --max-depth 1
```

如果目标站点需要登录，可以这样复用会话：

```bash
site-mirror-agent --url https://example.com/account --cookie "session=abc123; user=demo"
```

或者给浏览器渲染阶段传入 Playwright 登录态：

```bash
site-mirror-agent --url https://example.com/dashboard --storage-state ./auth/state.json
```

也可以补充自定义请求头：

```bash
site-mirror-agent --url https://example.com/api-docs --header "Authorization: Bearer token" --header "X-Tenant: demo"
```

如果你希望失败时自动重试，也可以这样运行：

```bash
site-mirror-agent --url https://example.com/docs --page-retries 3 --retry-backoff 1.5
```

如果你希望让 `StrategyAgent` 在任务启动前调用 DeepSeek 做抓取决策：

```bash
set DEEPSEEK_API_KEY=your_api_key
site-mirror-agent --url https://example.com --use-llm-strategy --llm-model deepseek-chat
```

这个 LLM 步骤当前只参与“策略判断”，不会直接接管下载、重写和落盘逻辑；如果缺少依赖、没配置 `DEEPSEEK_API_KEY`，或请求失败，会自动退回确定性启发式策略。你也可以通过 `--llm-provider openai` 和 `--llm-base-url` 切回别的 OpenAI-compatible 接口。

如果你希望让 `SiteProfilerAgent` 在策略判断前，先基于首页片段做一层 LLM 站点画像：

```bash
set DEEPSEEK_API_KEY=your_api_key
site-mirror-agent --url https://example.com --use-llm-site-profile
```

这一步会尝试识别站点类型、是否像 SPA/文档站/新闻站，以及是否可能需要额外交互；拿不到 key 或请求失败时，会自动退回启发式画像。

如果你希望让系统在首页之外，再由 LLM 额外推断一批“值得补抓的入口 URL”：

```bash
set DEEPSEEK_API_KEY=your_api_key
site-mirror-agent --url https://example.com --use-llm-seed-discovery
```

这一步只会在起始页触发一次，目标是补栏目页、分页根路径或隐藏入口，不会无上限扩张抓取范围。

如果你希望在浏览器渲染时，让 LLM 规划少量安全交互来触发懒加载或“更多内容”：

```bash
set DEEPSEEK_API_KEY=your_api_key
site-mirror-agent --url https://example.com --use-llm-interaction-planner
```

这一步当前只允许两类白名单动作：

- `scroll_bottom`
- 点击“更多 / 下一页 / 展开 / load more / next”这类低风险文案

它不会让模型执行登录、提交、删除、购买之类高风险交互。

如果你希望在最终失败时，让 `FailureDiagnosisAgent` 额外调用 DeepSeek 输出解释和恢复建议：

```bash
set DEEPSEEK_API_KEY=your_api_key
site-mirror-agent --url https://example.com/private --use-llm-failure-diagnosis
```

这一步只会在页面最终失败、且确定性重试已经结束后触发，不会对每次短暂失败都发起 LLM 请求。

如果你希望让 `ValidatorAgent` 对首页和存在问题的页面做一层 LLM 质量审查：

```bash
set DEEPSEEK_API_KEY=your_api_key
site-mirror-agent --url https://example.com --use-llm-validation
```

这一步不会替代原来的确定性校验，而是在基础问题列表之外，补一层“页面主体是否像真的被完整保留”的判断。





1. 加入真实的任务队列
2. 把每个模块升级成独立 worker
3. 用 LLM 只负责策略和异常处理

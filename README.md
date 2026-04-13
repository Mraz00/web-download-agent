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

## 这版 MVP 的边界

已经覆盖：

- 同域名页面发现
- 基础 HTML 下载
- Playwright 渲染入口和运行时资源采集
- 队列驱动的多 worker 页面调度
- 并发静态资源下载
- HTML 里的本地链接和资源重写
- CSS 里的 `url(...)` 和 `@import` 二级资源重写
- Cookie、自定义 Header、Playwright storage state 会话复用
- 页面级自动重试和结构化失败分类
- 可选的 DeepSeek LLM 站点画像
- 可选的 DeepSeek LLM 入口发现 / 补种子
- 可选的 DeepSeek LLM 交互规划
- 可选的 DeepSeek LLM 策略判断
- 可选的 DeepSeek LLM 失败解释和恢复建议
- 可选的 DeepSeek LLM 页面质量审查
- 任务清单输出

暂未覆盖：

- 自动登录流程录制与账号密码登录编排
- 验证码和强反爬
- 无限滚动/复杂交互录制
- 增量同步和断点续传
- 更复杂的 LLM 调度和多阶段协商

## 第二阶段架构

当前这版已经不再是单线程串行处理，而是更接近真实 multi-agent 执行：

1. `Coordinator` 维护页面任务队列
2. 多个页面 worker 并发处理渲染、解析、重写、落盘
3. 每个页面 worker 调用资源下载 agent 并发抓静态资源
4. Playwright 渲染时额外记录网络请求，补上 DOM 里未显式出现的资源

这样设计的好处是，后续要继续升级成：

- 独立 worker 进程
- Redis / RabbitMQ 队列
- LLM 策略 agent
- 失败重试 agent

时，主干几乎不用推翻。

## 为什么这样设计

这个项目最难的部分是“把页面原样保下来”，所以底层必须是确定性模块。multi-agent 更适合用于：

- 站点类型判断
- 抓取策略选择
- 失败重试与诊断
- 内容提取和质量评估

也就是说：

- 下载动作交给代码模块
- 决策动作交给 agent

## 下一步建议

第一阶段先做稳定：

1. 跑通单页和浅层站点镜像
2. 增强资源发现和重写质量
3. 增加失败重试与速率控制

## 上传 GitHub 前

建议在第一次提交前再检查这几件事：

1. 确认不要把 `output/`、`tmp*`、`validation_css/` 这类镜像产物和临时目录提交到仓库。
2. 如果你本地保存过登录态、Cookie 文件或 Playwright storage state，确认它们不会被提交。
3. 选择一个你自己的开源协议，再补 `LICENSE` 文件；这一步最好由你自己决定，不建议我替你默认选。
4. 首次公开时，优先提交 `src/`、`README.md`、`AGENTS.md`、`pyproject.toml` 这些核心文件。

第二阶段再变智能：

1. 加入真实的任务队列
2. 把每个模块升级成独立 worker
3. 用 LLM 只负责策略和异常处理

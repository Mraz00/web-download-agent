# AGENTS

## 项目定位

这个项目不是传统“只抽数据”的爬虫，而是一个面向“网站镜像下载”的 multi-agent 执行系统。

目标是：

- 输入一个网站 URL
- 自动抓取页面和静态资源
- 尽量保留网页结构、样式和资源引用关系
- 输出可以离线打开的页面镜像
- 为后续接入 LLM 策略调度、自动登录、验证码诊断预留清晰边界

核心原则：

- 下载、解析、重写、落盘优先用确定性代码完成
- agent 负责职责分离、调度、策略选择、失败诊断
- 不让 LLM 直接替代底层下载器

## 当前 Agent 列表

### 1. Coordinator

职责：

- 接收用户输入的 URL 和任务配置
- 创建页面任务队列
- 启动多个页面 worker
- 汇总页面结果、失败记录和任务摘要

输入：

- `CrawlConfig`

输出：

- `JobManifest`

当前代码位置：

- `src/web_download_agent/pipeline.py`

### 1.5 StrategyAgent

职责：

- 在启用 `--use-llm-strategy` 时调用 LLM
- 基于 URL、启发式画像和任务配置，细化抓取策略
- 当前只决定是否坚持浏览器优先，并输出原因说明
- 当 LLM 不可用时自动回退到确定性策略
- 当前默认使用 DeepSeek 的 OpenAI-compatible 接口

输入：

- `CrawlConfig`
- `SiteProfile`

输出：

- `CoordinatorDecision`

当前代码位置：

- `src/web_download_agent/agents.py`

### 2. SiteProfilerAgent

职责：

- 对起始 URL 做轻量站点画像
- 判断任务是否优先走浏览器渲染
- 给协调器返回策略说明
- 在启用 `--use-llm-site-profile` 时，基于首页片段补充站点类型和交互提示

输入：

- `CrawlConfig`

输出：

- `SiteProfile`

当前特点：

- 现在规则比较轻，只做基础 URL 判断
- 现在已经可以可选调用 LLM 增强首页画像

### 2.5 SeedDiscoveryAgent

职责：

- 在启用 `--use-llm-seed-discovery` 时，从起始页推断额外入口 URL
- 重点补栏目页、分页入口、归档入口等“值得补抓”的页面
- 只补充候选种子，不直接替代正常链接发现

输入：

- `CrawlConfig`
- 起始页 URL
- 起始页 HTML 片段
- 已发现链接列表

输出：

- 额外候选 URL 集合
- 可选说明摘要

### 2.6 InteractionPlannerAgent

职责：

- 在启用 `--use-llm-interaction-planner` 时，为浏览器渲染生成少量安全交互步骤
- 重点触发懒加载、更多内容展开、分页按钮点击
- 当前只允许白名单动作，不直接执行高风险交互

输入：

- `CrawlConfig`
- 当前页面 URL
- 当前页面 HTML 片段
- 页面深度

输出：

- 交互动作列表
- 可选说明摘要

### 3. RendererAgent

职责：

- 获取页面 HTML
- 优先使用 Playwright 渲染
- Playwright 模式下采集运行时网络资源
- 无浏览器能力时回退到普通 HTTP 请求

输入：

- 页面 URL
- `CrawlConfig`
- 是否要求浏览器渲染

输出：

- `PageSnapshot`

当前支持：

- `Cookie`
- 自定义 Header
- Playwright `storage state`

### 4. CrawlerAgent

职责：

- 从页面 HTML 中发现页面链接和静态资源
- 合并 DOM 中发现的资源和浏览器运行时资源
- 根据站点范围规则过滤掉不该继续抓取的链接

输入：

- 页面 URL
- HTML
- `CrawlConfig`
- 运行时资源列表

输出：

- 页面链接集合
- 静态资源集合

### 5. AssetFetcherAgent

职责：

- 并发下载 CSS、JS、图片、字体、附件等静态资源
- 递归分析 CSS 里的 `url(...)` 和 `@import`
- 把 CSS 二级依赖一起下载下来
- 把 CSS 文件本身改写成本地资源路径

输入：

- 资源 URL 集合
- 资源输出目录
- `CrawlConfig`

输出：

- `asset_map`，即远程资源 URL 到本地文件名的映射
- 下载失败列表

### 6. RewriterAgent

职责：

- 把 HTML 中的资源链接改写为本地相对路径
- 把站内页面链接改写为本地页面路径
- 保留 fragment，比如字体文件后的 `#id`

输入：

- HTML
- 页面基础 URL
- 页面本地相对路径
- `asset_map`
- 页面链接映射

输出：

- 改写后的 HTML

### 7. ValidatorAgent

职责：

- 对最终页面做基础完整性校验
- 报告仍然残留在 HTML 中的远程资源引用
- 帮助区分“镜像成功”与“看起来成功但仍依赖远程资源”
- 在启用 `--use-llm-validation` 时，对首页和存在问题的页面补充镜像质量审查

输入：

- 改写后的 HTML
- `asset_map`

输出：

- 问题列表

说明：

- 这个 agent 现在是基础校验器，不是浏览器级验收器
- 后续可以加“离线打开验证”和截图对比

### 8. FailureDiagnosisAgent

职责：

- 对页面失败做结构化分类
- 判断错误是否值得重试
- 统一输出失败记录
- 在启用 `--use-llm-failure-diagnosis` 时，为最终失败补充解释和恢复建议

当前分类包括：

- `auth_required`
- `not_found`
- `rate_limited`
- `server_error`
- `timeout`
- `network_error`
- `io_error`
- `unexpected_error`

输入：

- URL
- 深度
- 所在阶段
- 当前尝试次数
- 异常对象
- 可选的 `CrawlConfig`

输出：

- `FailureRecord`

## 当前执行流程

当前主流程如下：

1. 用户输入起始 URL 和任务参数
2. `Coordinator` 创建任务目录和页面队列
3. `SiteProfilerAgent` 生成启发式站点画像
4. `StrategyAgent` 可选调用 LLM，细化抓取策略
5. 多个页面 worker 从队列中取任务
6. `RendererAgent` 获取页面 HTML
7. `InteractionPlannerAgent` 可选在浏览器中触发少量安全交互
8. `CrawlerAgent` 发现页面链接和静态资源
9. `SeedDiscoveryAgent` 可选从起始页补充额外候选入口
10. `AssetFetcherAgent` 下载静态资源并递归处理 CSS 依赖
11. `RewriterAgent` 把 HTML 资源和站内链接改成本地路径
12. `ValidatorAgent` 检查页面中是否仍有远程资源引用
13. `Coordinator` 汇总结果并写入 `manifest.json`
14. 若页面失败，则交给 `FailureDiagnosisAgent` 决定是否重试；最终失败时可选调用 LLM 补充解释

## 数据模型

当前重要模型如下：

- `CrawlConfig`：任务配置
- `SiteProfile`：站点画像
- `PageSnapshot`：页面抓取结果
- `PageResult`：页面落盘结果
- `PageTask`：页面队列任务
- `FailureRecord`：结构化失败结果
- `JobManifest`：任务最终汇总

## 责任边界

### 必须由确定性代码完成的部分

- 页面请求
- 浏览器渲染
- 资源下载
- CSS 依赖解析
- HTML 和 CSS 链接重写
- 文件落盘
- manifest 生成

### 适合以后交给 LLM / 智能 agent 的部分

- 站点类型判断
- 复杂抓取策略选择
- 登录流程推荐
- 验证码和反爬诊断
- 失败原因解释
- 内容抽取和语义结构化

当前已经落地的第一步：

- `StrategyAgent` 可选调用 LLM，为站点镜像任务补一层策略判断

## 当前已支持能力

- 单页和浅层站点抓取
- 多 worker 页面队列
- 同站点资源发现
- HTML 本地化
- CSS 二级资源本地化
- 页面级重试
- 失败分类
- 登录态复用

## 当前未覆盖能力

- 自动执行登录流程
- 验证码处理
- 强反爬绕过
- 无限滚动和复杂交互录制
- 离线浏览器回放验证
- 增量同步和断点续传
- 真正的 LLM 调度器

## 后续建议新增 Agent

### LoginRecorderAgent

职责：

- 录制一次真实登录流程
- 导出 Playwright `storage state`
- 供后续镜像任务复用

### AntiBotDiagnosisAgent

职责：

- 识别验证码、限流、反爬页面
- 输出建议：降速、换浏览器、复用登录态、人工介入

### StrategyAgent

职责：

- 根据站点画像选择抓取策略
- 决定是否启用浏览器渲染、滚动脚本、点击展开逻辑

### OfflineReplayAgent

职责：

- 使用本地离线文件重新打开页面
- 检查样式、脚本和资源缺失
- 输出镜像质量评分

## 开发约定

- 新增 agent 时，优先先定义清楚输入、输出和失败策略
- 不要让多个 agent 写同一类状态，避免职责重叠
- 如果某个模块只是纯函数式处理，也可以保留为普通模块，不必强行包装成 agent
- 任何需要联网下载的动作，都应保留可诊断的失败信息
- 任何重写逻辑都应优先保证“离线可打开”，再考虑语义优雅

## 当前结论

这版项目已经具备“multi-agent 风格的工程骨架”，虽然还不是 LLM 驱动的自治系统，但边界已经足够清晰：

- 协调器负责调度
- 页面 worker 负责执行
- 各 agent 负责单一职责
- 失败有结构化诊断
- 后续可以继续演进成真正的智能系统

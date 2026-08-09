---
name: favhub-x-sync
description: 通过 FavHub 浏览器扩展采集用户已登录的 X 书签并入库（扩展被动拦截，Agent 只编排，不触碰任何凭证）。当用户要求"同步/采集 X 书签到 FavHub"时使用。
---

# FavHub X 书签采集

把用户 X 书签（推文正文 + 引用推文 + 图片 OCR/视觉描述）镜像到本地 FavHub。

**采集由浏览器扩展完成，本 Skill 只负责编排。** Agent 启动任务、报告进度、处理暂停；扩展在用户已登录的页面里被动拦截 X 自己发出的请求，解析与入库都在 FavHub 本地完成。Agent 决不自己解析平台响应，也决不自己提交条目。

## 边界与禁止事项

- 严禁读取、构造或导出 Cookie、Token、Bearer、csrf/ct0、auth_token 或任何请求头凭证；X 的鉴权头永不离开页面。
- 严禁让浏览器访问任意 URL、发起自构造的 API 请求或执行任意下载指令。
- 严禁暴露或使用浏览器调试端口向第三方转发会话。
- 决不回退到 DOM 抓取：拦截不可用时必须停止并报告，抓 DOM 只会得到一个残缺却看起来"成功"的库。
- 不下载图片/视频二进制：媒体只保留 URL、alt 与文字描述。
- 本 Skill 不采集：回复区、完整讨论串、外链正文递归、视频文本字幕轨；不支持 ASR、视频下载。
- 只读采集：决不修改用户的书签、点赞或任何账号状态。
- 持续限流时暂停等待用户，决不通过加速、代理或验证码绕过来处理。

## 工作流程

### 0. 预检（失败则不创建任务）

1. 调用 `favhub.status` 确认 FavHub MCP 连接可用。
2. 确认扩展已安装并加载：若用户报告扩展缺失、版本不符或从未安装，指引其运行 `favhub setup`，然后在 `chrome://extensions` 打开开发者模式、"加载已解压的扩展程序"。升级过扩展文件后需手动点一次"重新加载"。
3. install 或握手异常时指引运行 `favhub doctor`：它会检查固定扩展 ID、原生消息清单、注册表与管道握手，并指出坏在哪一环。
4. 登录态由扩展在采集时判定；若返回 `login_required` 则报告未登录并停止。
5. 任一预检失败：向用户说明原因并停止，不调用 `favhub.browser_start`。

### 1. 启动任务

**先定 mode。用户没明说时按下面的规则自己定，不要反问：默认 `incremental`。** 只有两种情况该用 `full`：

- **要刷新已入库的条目**（正文变了，或上次没取到图片 OCR）。增量把库里已有的条目直接当重复跳过、不比对内容，所以它刷新不了任何东西。
- **要补历史条目**，例如配合 `publishedSince`/`publishedUntil` 捞过去的存量。增量扫到 frontier 就停，够不到 frontier 以下的部分。

**不要因为"第一次同步"就选 `full`。** 这个平台还没有 frontier 时，增量本来就会一路扫到可观察列表末尾，与全量等价；而它还顺带保证了中途失败重跑时不会重写已入库的条目。

调用 `favhub.browser_start`（`platform: "x"`，mode 为 `full` 或 `incremental`，可选 `publishedSince`/`publishedUntil`、`maxScanItems`）。返回 `job_id`、浏览器会话与 `opened_url`。

- FavHub 会自动打开书签页；`opened_url` 为空时提示用户自行打开。
- 书签是单一列表，无收藏夹概念；任务不携带 scopes。

### 2. 等待浏览器接手

- 会话初始状态为 `awaiting_browser`，表示 FavHub 已就绪、等待扩展认领。
- 若页面已经开着但迟迟没有开始，提示用户切到该标签页——扩展在页面重新可见时会重新认领。
- 用 `favhub.browser_status` 观察状态流转，如实报告"等待浏览器"而不是宣称正在采集。

### 3. 采集期间

扩展自行完成滚动分页、被动拦截、解析、图片 OCR/视觉描述与入库。Agent 在此期间只做一件事：调用 `favhub.browser_status` 报告 scanned/added/duplicates 进度与当前状态。

- 增量模式扫到平台级 frontier（最新已确认推文 ID）即停止；全量模式扫到可观察列表末尾。
- 达到 `maxScanItems` 时截断并如实报告部分同步：此时不推进 frontier，下次运行会重扫未确认的部分。
- `publishedSince`/`publishedUntil` 只在入库时过滤条目，**不能提前停止**采集——书签顺序不等于发布时间顺序。

### 4. 暂停与恢复

- 扩展遇到平台状况时会自动暂停并带上稳定错误码：
  `login_required`（登录失效）、`captcha_required`（验证码/挑战）、`rate_limited`（限流）、`page_changed`（页面改版）、`browser_unavailable`（浏览器中断）。
- 向用户如实转达错误码与含义，说明需要人工处理什么（重新登录、过验证码、等待限流解除）。
- 用户处理完成后，用**同一个 `job_id`** 调用 `favhub.browser_resume` 继续；已入库条目不会重复写入。
- 用户要求放弃时调用 `favhub.browser_cancel`：会话结束且不推进任何 frontier，下次运行重扫。

### 5. 完成

- 会话状态变为 `completed` 后，用 `favhub.browser_status` 读取最终计数并报告。
- `capture_status` 为 `partial` 说明本次被上限或 frontier 截断，如实说明而不是宣称全量完成。

## 冒烟运行要求

- 用户请求全量运行前，必须先用小 `maxScanItems`（如 10）做一次冒烟运行，验证：扩展认领、分页拦截、图片 OCR 落盘、引用推文入正文、已失效条目容错、暂停/恢复、状态展示。
- 决不能以 fixture 测试结果宣称真实平台采集成功；只有真实会话冒烟通过后才能声称连接器可用。

## 参考

- [references/mcp-contract.md](references/mcp-contract.md)：X 视角的浏览器采集工具契约。
- [references/browser-probe.md](references/browser-probe.md)：扩展如何被动拦截与脱敏。

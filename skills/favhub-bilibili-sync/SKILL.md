---
name: favhub-bilibili-sync
description: 通过 FavHub 浏览器扩展采集用户已登录的 Bilibili 收藏夹并入库（同源只读请求，Agent 只编排，不触碰任何凭证）。当用户要求"同步/采集 B 站收藏夹到 FavHub"时使用。
---

# FavHub Bilibili 收藏夹采集

把用户 Bilibili 收藏夹（元数据 + 字幕）镜像到本地 FavHub。

**采集由浏览器扩展完成，本 Skill 只负责编排。** Agent 启动任务、报告进度、处理暂停；扩展在用户已登录的页面里发起同源只读请求，解析与入库都在 FavHub 本地完成。Agent 决不自己解析平台响应，也决不自己提交条目。

## 边界与禁止事项

- 严禁导出、记录或传输 Cookie、Token、SESSDATA、完整请求头或任何凭证；FavHub 的任何工具参数中都不得出现凭证。
- 严禁让浏览器访问任意 URL 或执行任意下载指令；扩展只允许请求收藏相关的固定端点白名单。
- 严禁暴露或使用浏览器调试端口向第三方转发会话。
- 决不回退到 DOM 抓取：接口不可用时必须停止并报告，抓 DOM 只会得到一个残缺却看起来"成功"的库。
- 本 Skill 不支持：X 连接器、OCR、ASR、视频下载、封面下载、评论/弹幕采集。封面只保留 URL。
- 只读采集：决不修改用户的收藏、关注或任何账号状态。扩展只发 GET，收藏夹的删除/移动端口不在白名单内。
- 持续限流时暂停等待用户，决不通过加速、代理或验证码绕过来处理。

## 工作流程

### 0. 预检（失败则不创建任务）

1. 调用 `favhub.status` 确认 FavHub MCP 连接可用。
2. 确认扩展已安装并加载：若用户报告扩展缺失、版本不符或从未安装，指引其运行 `favhub setup`，然后在 `chrome://extensions` 打开开发者模式、"加载已解压的扩展程序"。升级过扩展文件后需手动点一次"重新加载"。
3. install 或握手异常时指引运行 `favhub doctor`：它会检查固定扩展 ID、原生消息清单、注册表与管道握手，并指出坏在哪一环。
4. 登录态由扩展在采集时判定；若返回 `login_required` 则报告未登录并停止。
5. 任一预检失败：向用户说明原因并停止，不调用 `favhub.browser_start`。

### 1. 启动任务

调用 `favhub.browser_start`（`platform: "bilibili"`，mode 为 `full` 或 `incremental`，可选 `publishedSince`/`publishedUntil`、`maxScanItems`）。返回 `job_id` 与浏览器会话。

- **收藏夹由扩展自行枚举**，任务不需要预先传 scopes。
- FavHub 会自动打开 B 站首页；扩展在那里向平台询问当前登录账号，再据此采集收藏夹。收藏页地址在用户自己的账号 ID 下，FavHub 事先并不知道，因此落地页是首页而不是收藏页——这不影响采集，因为主动模式全程走接口、不读页面内容。

### 2. 等待浏览器接手

- 会话初始状态为 `awaiting_browser`，表示 FavHub 已就绪、等待扩展认领。
- 若页面已经开着但迟迟没有开始，提示用户切到该标签页——扩展在页面重新可见时会重新认领。
- 用 `favhub.browser_status` 观察状态流转，如实报告"等待浏览器"而不是宣称正在采集。

### 3. 采集期间

扩展自行完成收藏夹枚举、分页、字幕获取与入库，请求之间保持节流。Agent 在此期间只做一件事：调用 `favhub.browser_status` 报告进度与当前状态。

- 默认采集**全部收藏夹**；按用户要求应用**包含/排除**过滤器（按收藏夹 ID 匹配）。
- 每个收藏夹有自己的 frontier（`frontierScopes`）：增量模式扫到该夹自己的 frontier 即停止。被截断的夹不会出现在其中，因此不会被误认为"已扫完"。
- 字幕为尽力而为：没有字幕、字幕链接过期、字幕文档格式异常，都只损失这一条的字幕，决不中断整个收藏夹。
- 达到 `maxScanItems` 时截断并如实报告部分同步。被截断的收藏夹**不推进 frontier**，下次运行会重扫它未确认的部分。
- `publishedSince`/`publishedUntil` 只在入库时过滤条目，**不能提前停止**采集——收藏顺序不等于发布时间顺序。

### 4. 暂停与恢复

- 扩展遇到平台状况时会自动暂停并带上稳定错误码：
  `login_required`（登录失效）、`captcha_required`（验证码/挑战）、`rate_limited`（限流）、`page_changed`（页面改版）、`browser_unavailable`（浏览器中断）。
- 向用户如实转达错误码与含义，说明需要人工处理什么。
- 用户处理完成后，用**同一个 `job_id`** 调用 `favhub.browser_resume` 继续；已入库条目不会重复写入。
- 用户要求放弃时调用 `favhub.browser_cancel`：会话结束且不推进任何 frontier。

### 5. 完成

- 会话状态变为 `completed` 后，用 `favhub.browser_status` 读取最终计数并报告。
- 逐收藏夹的结果在 `scopes` 中：`status` 为 `partial` 的夹说明本次没扫完。
- `capture_status` 为 `partial` 说明整体被上限截断，如实说明而不是宣称全量完成。

## 冒烟运行要求

- 用户请求全量运行前，必须先用小 `maxScanItems`（如 5）做一次冒烟运行，验证：扩展认领、收藏夹枚举、分页、字幕落盘、暂停/恢复、状态展示。
- 决不能以 fixture 测试结果宣称真实平台采集成功；只有真实会话冒烟通过后才能声称连接器可用。

## 参考

- [references/mcp-contract.md](references/mcp-contract.md)：Bilibili 视角的浏览器采集工具契约。
- [references/browser-probe.md](references/browser-probe.md)：扩展如何发起同源只读请求与脱敏。

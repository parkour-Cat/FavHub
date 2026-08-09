# FavHub 浏览器采集 MCP 工具契约（X 视角）

四个工具走 `tools/call`，参数 camelCase，结果 snake_case。`platform` 取 `"x"`。
参数中不得包含本地路径；凭证类字段（Cookie/Token/Bearer/ct0 等）严禁出现在任何参数中。

条目提交由扩展经原生消息通道完成，**不在 Agent 的工具面上**：Agent 无法也不需要
自行提交条目或结束采集。

## favhub.browser_start

`platform: "x"`、`mode: "full" | "incremental"`、可选 `publishedSince`/`publishedUntil`（带时区）、`maxScanItems`。
X 是单一书签列表：**不携带 `scopes`**（传入会被拒绝）。

结果：`{job_id, browser_session, opened_url, frontiers: {x: [tweetId...]}, scoped_frontiers: {}}`

- `frontiers` 是**平台级**的最新已确认推文 ID；X 不使用 `scoped_frontiers`。
- `browser_session.status` 初始为 `awaiting_browser`，表示等待扩展认领。
- `opened_url` 是 FavHub 已代为打开的页面；为空时需提示用户自行打开。

## favhub.browser_status

`jobId` → 任务级 `capture_status`、平台 counts/error、`browser_sessions`（含会话状态与租约）、
`enrichment_pending` 与索引摘要。X 任务下 `scopes` 为空数组。

`capture_status` 为 `partial` 表示被 `maxScanItems` 或 frontier 截断，此时不推进 frontier。

## favhub.browser_resume

`jobId`、`platform: "x"`。用于暂停后在**同一个 job** 上继续。
会话不可恢复时返回错误而不是静默新建，避免出现两个写入者。

## favhub.browser_cancel

`jobId`、`platform: "x"`。结束会话且**不推进任何 frontier**，下次运行重扫本次未确认的部分。

## 暂停码

`login_required`、`captcha_required`、`rate_limited`、`page_changed`、`browser_unavailable`。
由扩展在遇到平台状况时上报，Agent 只负责如实转达。

## 错误码

- JSON-RPC `-32602`：顶层参数不合法（platform 不受支持、X 任务携带 scopes 类参数、日期无时区等）。
- 工具错误 `invalid_argument`：参数内容不合法。
- 工具错误 `not_found`：未知 job/platform。
- 工具错误 `storage_error` / `index_unavailable`：本地存储或索引问题。
- 错误消息为固定脱敏文案；日志只含稳定错误码与 job/platform 定位信息。

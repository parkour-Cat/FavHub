# FavHub 浏览器采集 MCP 工具契约（知乎视角）

四个工具走 `tools/call`，参数为 camelCase，结果 `structuredContent` 为 snake_case。
只允许 `platform: "zhihu"`。参数中不得包含本地路径；凭证类字段严禁出现在任何参数中。

条目提交与收尾由扩展经原生消息通道完成，**不在 Agent 的工具面上**：Agent 无法也不需要
自行提交条目或结束采集。

## favhub.browser_start

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `platform` | `"zhihu"` | 必填 |
| `mode` | `"full" \| "incremental"` | 必填 |
| `publishedSince` / `publishedUntil` | ISO-8601 带时区 | 可选，发布时间过滤 |
| `maxScanItems` | int ≥ 1 | 可选，扫描上限 |

**不需要传 `scopes`**：收藏夹由扩展在采集时自行枚举并向 FavHub 声明。

结果：`{job_id, browser_session, opened_url, frontiers, scoped_frontiers}`

- `browser_session.status` 初始为 `awaiting_browser`，表示等待扩展认领。
- `opened_url` 是 FavHub 已代为打开的收藏页；为空时需提示用户自行打开。
- 增量模式下 `scoped_frontiers` 是每个收藏夹上次确认的最新条目 ID；扫描遇到即停止该收藏夹。
  条目 ID 形如 `answer-<id>` / `article-<id>`。

## favhub.browser_status

参数：`jobId`。结果：任务级 `capture_status`、每平台 counts/error、每收藏夹 `scopes`
（scope_id、scope_name、status、counts、error）、`browser_sessions`（会话状态与租约）、
`enrichment_pending` 与索引摘要。

逐收藏夹独立报告，一个被截断的收藏夹不会拖累其他已扫完的：扩展提交的 `scopeResults`
逐夹记录 `maxScanReached`，而 `frontierScopes` 只包含真正扫完的夹——被截断的夹
**不出现**在其中，而不是给一个空列表。这条不变量很关键：同时声称"到达上限"又"给出
frontier"的夹会被 FavHub 拒绝，因为下次增量运行会跳过这次没扫到的部分。

## favhub.browser_resume

参数：`jobId`、`platform: "zhihu"`。用于暂停后在**同一个 job** 上继续。
会话不可恢复时返回错误而不是静默新建，避免出现两个写入者。

## favhub.browser_cancel

参数：`jobId`、`platform: "zhihu"`。结束会话且**不推进任何 frontier**，下次运行重扫。

## 暂停码

`login_required`、`rate_limited`、`page_changed`、`browser_unavailable`。
由扩展在遇到平台状况时上报，Agent 只负责如实转达。

知乎的错误信封由扩展与 FavHub 用同一套规则判定（错误码 100/101 或含"登录"→
`login_required`；4039 或含"频繁"/"异常"→ `rate_limited`；其余 → `page_changed`），
两侧结论必须一致，否则会出现一边暂停、一边照收的分歧。

## 错误码

- JSON-RPC `-32602`：顶层参数不合法（缺 `jobId`、平台不受支持、日期无时区等）。
- 工具错误 `invalid_argument`：参数内容不合法。
- 工具错误 `not_found`：未知 job/platform/scope。
- 工具错误 `storage_error` / `index_unavailable`：本地存储或索引问题。
- 错误消息均为固定的脱敏文案；日志只含稳定错误码与 job/platform/scope 定位信息。

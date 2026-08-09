# FavHub 丰富 MCP 工具契约

三个工具走 `tools/call`，参数 camelCase，结果 snake_case。流程凭 `taskId` 贯穿：任务由 `enrich_next` 认领，提交/跳过都只凭任务 ID，输入哈希由任务自带，Agent 无法错配条目。

## favhub.enrich_next

可选 `platform`（`bilibili` \| `github` \| `x` \| `zhihu`）。认领下一条待丰富任务；条目内容已变化或条目已消失的过时任务会被自动完成并跳过。

带 `platform` 时只认领该平台的任务，别的平台原样留在队列里（不是 skip，不计 attempts）。
此时 `{task: null}` 表示**该平台**已做完，不表示队列为空。

结果：`{task: null}`（队列为空）或

```json
{"task": {
  "task_id": "…", "platform": "x", "source_id": "77",
  "input_hash": "…", "attempts": 1,
  "title": "…", "author": "…", "canonical_url": "…",
  "content": [{"path": "content.md", "text": "…"}, …],
  "truncated": false
}}
```

`content` 为全部系统 Markdown（content.md/transcript/ocr），总量上限 100000 字符，超出部分截断并置 `truncated: true`。

## favhub.enrich_submit

| 参数 | 约束 |
| --- | --- |
| `taskId` | 必填，来自 enrich_next |
| `summary` | 非空，≤ 2000 字符；**正文超过 200 字符时，摘要长度必须小于正文长度**，否则拒绝 |
| `tags` | 1-8 个非空字符串，≤ 40 字符/个；提交时小写归一并去重 |
| `contentType` | `text` \| `video` \| `image` \| `mixed` |
| `provider` / `model` | 非空，记录生成来源 |

结果：`{task_id, outcome}`，`outcome` 为 `applied`（已落盘并重新入索引）或 `stale`（任务在认领后被内容更新替代，已自动完成，无需重试）。

## favhub.enrich_skip

`taskId`、`code`（`generation_failed` \| `content_unsupported`）、`message`（≤ 200 字符脱敏说明）。

两个 code 的结果不同，结果里的 `outcome` 会说明是哪一种：

| code | status | outcome | 会不会再被认领 |
| --- | --- | --- | --- |
| `generation_failed` | `pending`（attempts +1） | `retryable` | **会**——下一次 `enrich_next` 就可能是它 |
| `content_unsupported` | `declined` | `declined` | 不会，除非内容变化产生新的 `input_hash` |

`claim_next` 取的是最老的 pending 任务，所以用 `generation_failed` 表达"这条不做了"会让整轮循环卡在同一条上。

## 错误码

- JSON-RPC `-32602`：参数不合法（缺 taskId、tags 为空、contentType 越界等）。
- 工具错误 `invalid_argument`：任务状态不允许该操作（未认领/已完成）。
- 工具错误 `not_found`：未知任务或条目。
- 错误消息为固定脱敏文案；日志只含稳定错误码与任务/条目定位信息。

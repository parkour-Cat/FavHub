# FavHub 检索 MCP 工具契约（使用侧）

## favhub.search

| 参数 | 说明 |
| --- | --- |
| `query` | 必填，检索词 |
| `platforms` | 可选，`["bilibili","x"]` 子集 |
| `contentTypes` | 可选，按收藏条目的主要媒体类型筛选，如 `["video"]`；不要用于主题或讨论对象的检索 |
| `collections` | 可选，限定在用户自己的收藏夹名内，如 `["算法"]`；名称须来自 `favhub.collections` |
| `publishedSince` / `publishedUntil` | 可选，发布时间界（ISO-8601 带时区，含边界） |
| `limit` | 可选 1-50，默认 10 |

主题与媒体类型是两回事：问“AI 视频相关内容”时，`AI 视频`是检索主题，应省略
`contentTypes`；问“只看收藏的视频”时，视频是条目的媒体类型限制，应传入
`contentTypes: ["video"]`。

结果：`found`、`reason`（未命中原因）、`retrieval_mode`（`fts`/`hybrid`）、`vector_warning`、`hits[]`。
`limit` 计算的是返回的唯一收藏条目数，不会因同一条目有多个匹配片段而重复计数。
每条 hit：`platform`、`source_id`、`title`、`author`、`published_at`、`content_type`、`excerpt`、
`canonical_url`、`local_path`（数据根相对路径）、`line_start`/`line_end`、`citation_id`
（`favhub:<platform>/<source_id>#chunk-<n>`）、匹配来源与分数字段。

每条 hit 还包含：`evidence_level`（该条目已保存的最强证据类型）、
`evidence_warning`（证据不足时的提示）和 `supporting_chunks`（同一条目中另外的相关片段，
含各自引用位置）。`evidence_level: "title_only"` 只表示标题和元数据命中，是继续核实的弱线索，
不能作为内容事实的证明。

## favhub.get_item

`platform` + `sourceId`，可选 `includeContent`（默认 true）。
结果：`source`（source.json 快照，含丰富区块）、`files`、`system_content`
（content.md / transcript / ocr 全文，键为相对路径）。

## favhub.collections

无参数。返回这个库的地图，两部分：

- `collections[]`：用户自己的收藏夹，按存活条目数从多到少。每项 `platform`、`name`、`items`。
  条目数只算 `available` 的条目，平台已下架的不计入。
- `platforms[]`：每个平台的 `items`（存活条目数）与 `unfiled`（其中不属于任何收藏夹的条目数）。

`unfiled == items` 的平台没有收藏夹结构（GitHub star、X 书签都是平的），
`collections[]` 里不会有任何名字描述它们的内容——把收藏夹列表当成整个库会漏掉这部分。

## favhub.status

无参数。返回索引条目数、片段数、待处理/失败任务数与 embedding 状态摘要——
用于判断库是否可用、混合检索是否就绪。

## 错误

`invalid_argument` / `not_found` / `storage_error` / `index_unavailable`，均为脱敏固定文案。
`index_unavailable` 时可提示用户运行 `favhub --root <root> reindex` 或检查数据根。

# FavHub

[English](README.md) · 简体中文

[![CI](https://github.com/parkour-Cat/FavHub/actions/workflows/ci.yml/badge.svg)](https://github.com/parkour-Cat/FavHub/actions/workflows/ci.yml)

FavHub 把你收藏了却再没打开过的东西镜像到本地：B 站收藏夹、X 书签、知乎收藏、GitHub star，
汇成一个纯文件的本地库，建好检索索引，再通过 MCP 交给编程 Agent——于是你可以**向自己的收藏提问**，
而不是滑动着找。

三条性质是承重的：

- **FavHub 永远看不到任何凭证。** 采集发生在你自己已登录的浏览器里，由 Chrome 扩展完成。
  Cookie、token、请求头从不被读取、构造、导出或发送到任何地方。GitHub 连接器走的是公开接口。
- **FavHub 自己不发网络请求**，唯一的例外是 GitHub 的公开 API。摘要由调用方 Agent 的模型撰写，
  FavHub 只负责校验和落库。
- **文件就是库。** `items/` 是事实来源；SQLite 里存的是派生的索引数据，随时可以从文件重建。

## 安装

```powershell
uv tool install "favhub[embedding]"
favhub setup
favhub doctor
```

`favhub setup` 写入数据根、安装 Chrome 扩展文件、注册 Native Messaging 宿主、安装 Agent Skills。
`favhub doctor` 逐项验证固定的扩展 ID、原生宿主清单、注册表项和管道握手，**指出坏在哪一环**，
而不是笼统地报一个失败。

### 加载扩展（仅首次安装）

Chrome 不允许程序代替用户安装未打包的扩展，所以这步只能手动：

1. 打开 `chrome://extensions`，开启**开发者模式**。
2. 点击**加载已解压的扩展程序**，选择 `favhub setup` 打印出的目录
   （`%LOCALAPPDATA%\FavHub\extension`）。

扩展的密钥是固定的，所以重装后 ID 不变，Native Messaging 的白名单里永远只有一个 ID。

### 升级

```powershell
uv tool upgrade favhub
favhub setup
```

**然后去 `chrome://extensions` 在 FavHub Collector 卡片上点「重新加载」。**
不点的话 Chrome 会一直跑旧副本。FavHub 会拿 Chrome 报上来的版本和自己安装的版本比对，
**不一致就拒绝采集**——没重载的构建会明确报版本不匹配，而不是安静地用上个版本的代码继续采。

Windows 上，只要还有 FavHub 在运行，升级就无法替换 `favhub-mcp.exe`。先关掉 Agent 窗口；
否则 CLI 会告诉你是哪个进程号占着数据根。

### 卸载浏览器集成

```powershell
favhub uninstall-browser
```

删除已安装的扩展文件、Native Messaging 清单及其注册表项。**数据根不受影响。**
Chrome 里仍会列出这个扩展，需要你自己在 `chrome://extensions` 里移除。

## 采集

三个平台通过扩展在你自己的浏览器里采集，GitHub 完全不需要浏览器。每个连接器都有对应的
Agent Skill 负责编排——你让 Agent 去同步，它来驱动工具。

| 平台 | 读取方式 | 按收藏夹分区 |
| --- | --- | --- |
| B 站 | 扩展，主动发同源 GET | 是，每个收藏夹独立 |
| 知乎 | 扩展，主动发同源 GET | 是，每个收藏夹独立 |
| X | 扩展，你滚动时被动拦截 | 否，单一书签列表 |
| GitHub | FavHub 直接调用公开 starred API | 否，单一 star 列表 |

X 是例外，因为它的 GraphQL 接口靠请求头鉴权。扩展只在你滚动时记录**页面自己发出的**
书签请求的响应体，自己一个请求都不发。

**先冒烟。** 每个 Skill 在全量跑之前，都必须先用一个小的 `maxScanItems` 在真实登录的浏览器里
跑一遍。fixture 导入和假浏览器测试不是连接器，对真实平台什么都证明不了。

跨连接器通用的几条语义：

- **按分区增量。** 每个收藏夹在 `sync_frontier_scopes` 里有自己的 frontier，各自独立地停止、
  暂停和推进；重命名收藏夹不会重置它的 frontier。被 `maxScanItems` 截断的收藏夹**完全不推进**，
  所以下次会重扫它没确认完的部分。
- **发布时间过滤永远不会提前中止翻页。** 收藏顺序不等于发布顺序，所以超范围的条目只计数，
  扫描继续。
- **跨收藏夹去重。** 一个条目存在多个收藏夹里只采集一次，所有收藏夹名合并进 `collections`。
- **`notes.md` 是你的。** 每个条目只创建一次，任何同步、补摘要或修复路径都不会覆盖它。
- **增量模式绝不重写已有条目。** 库里已经有的条目会直接记为重复，连内容都不比对。
  要刷新已存条目必须用 `mode: full`。

```powershell
favhub github-sync --user <login> --mode incremental
```

可选的 `FAVHUB_GITHUB_TOKEN` 把速率上限从按 IP 计的每小时 60 次提升到按账号计的 5000 次。
它在请求时读取、不做任何存储、只附加到 api.github.com，且**从不出现在结果或错误里**——
结果只报 `authenticated: true|false`。无论设不设，采集范围都只是公开 star——
凭证只提高速率上限，不扩大采集范围。

### 一个值得知道的毛病

采集 B 站时，有时会拿到**属于另一个视频的字幕**。它是间歇性的：同一个视频可能这次给对，
下次给的就是别人的。

**出错的不是我们发出的请求。** detail 响应把标题、时长、cid 一并给出，而这里记录的每一次
拒绝，存下来的标题和时长都与真实视频**逐一吻合**——既然如此，跟它们一起来的 cid 也是
这个视频的。所以外来字幕是在 player 响应里回来的。尚未确定的只剩一点：那个响应是只给了
这一条外来轨，还是同时给了正确的轨、而本适配器「优先人工轨」的规则挑错了。

好在这个校验不依赖于知道答案。B 站按 `<aid><cid><hash>` 命名字幕对象，
也就是说对象自己写着它属于谁。FavHub 在下载之前先核对这个名字——
名字里不含当前视频的 cid 就拒绝。拒绝会记为 `subtitle_status: "wrong_video"`，
并把被拒的名字存进 `subtitle_offered`，因此它和"这个视频本来就没字幕"是可区分的；
而且**拒绝绝不会覆盖已经拿到的转写**。采用的对象名存进 `subtitle_source`——
这是事后回答"这段话到底是谁说的"的唯一依据。

## 补摘要：摘要、标签、内容类型

摘要和标签来自调用方 Agent 的模型。入库时会按采集的 `content_hash` 为每个条目入队一个持久的
`summarize` 任务，[`skills/favhub-enrich`](skills/favhub-enrich/SKILL.md) 驱动
「拉取 → 生成 → 提交」循环，用到 `favhub.enrich_next`、`favhub.enrich_submit`、
`favhub.enrich_skip` 三个工具。

**生成必须走便宜档模型。** 读一段文字写三五句话，用不着编排模型的单价；
Skill 按"档位"而不是型号名来表述这条规则，所以 Claude Code 和 Codex 都能照做。
`model` 字段必须如实记录真正生成文本的模型——正是这份诚实，
让 `favhub enrich-redo --model <name>` 能精确地把出问题的那一批重新入队。

有四条规则由**服务端强制执行**，而不只是在 Skill 里"请求遵守"：

- 摘要必须**比它所概括的内容更短**（正文超过 200 字时）。
- 中文内容**至少要有一个含中文字符的标签**。模型给中文内容打标签时有时会音译而不是翻译，
  而音译出来的词没有人会拿去搜索。
- 正文**去掉 URL 和媒体清单后为空**的条目根本不允许写摘要。正确做法是跳过它，
  而不是去描述那个链接。
- 拒绝时**明说是哪条规则被破坏了**，让调用方能照着改，而不是去猜是哪个字段不对。

两个跳过码的后果完全不同：`generation_failed` 让任务回到 pending 等待重试，
`content_unsupported` 则永久退出队列。内容变了会自动按新 hash 入队新任务，所以拒绝不是无期徒刑。

结果存在 `source.json` 的 `enrichment` 块里（不计入 `content_hash`），渲染进 `content.md`
成为 `## 摘要` 段落加一行标签——两者都可被 FTS 检索——并更新 `items.content_type`
让内容类型过滤器生效。**补摘要落库时会立即索引该条目**，所以摘要一写完就能被搜到。

```powershell
favhub --root data enrich-backfill              # 为补摘要功能出现之前采集的条目补入队
favhub --root data enrich-redo --model <name>   # 重做某个模型写的全部内容
favhub --root data enrich-redo --declined       # 重做全部被拒任务
```

## 提问

面向用户的流程是"提问"，不是"搜索框"：

```text
问题 -> 条目级检索 -> 读取最相关条目的全文 -> 证据矩阵 -> 带引用的综合回答
```

Agent 会读取最匹配的几条收藏的**完整内容**，然后写出带引用的直接回答，
遵循 [`skills/favhub-ask`](skills/favhub-ask/SKILL.md)。FavHub 自己不调用任何 LLM，
它提供的是检索、全文读取、证据字段和稳定引用。

回答必须区分三种来源：**你的收藏**、**当前的外部信息源**、**Agent 自己的推断**。
个人收藏可能残缺，也可能已经过时好几年，所以会变的事实值得去核实一次，
而不是仅仅因为它曾被收藏过就当作事实复述。

```powershell
favhub --root data search "<你的问题>"
favhub --root data search "<关键词>" --platform zhihu --collection "<收藏夹名>" --limit 20
favhub --root data get-item x <推文 ID> --include-content
```

检索的条数上限计的是**去重后的收藏条目**，不是匹配到的分块。每条结果带一个主引用和最多三条
辅助分块，以及一个 `evidence_level`：`title_only`、`body`、`transcript`、`ocr` 或 `mixed`。
`title_only` 的结果会保留为排名靠后的线索而非内容证据，并且会明确标注。

发布时间和收藏时间是两个独立的过滤器。收藏时间是这个条目进入你收藏的时刻——B 站、知乎、
GitHub 是真实值，X 用首次同步时间估算并会标注为估计值。该过滤器生效时，
收藏时间未知的条目会被排除。

连续查询多条时用 `search-batch`，它保持单个进程不退出，embedding provider 在多次查询之间
保持加载状态：

```powershell
favhub --root data search-batch --query "<第一条>" --query "<第二条>" --retrieval-mode hybrid
```

### 检索模式

`--retrieval-mode` 选择路径：

- **`auto`**（默认）走混合检索，向量不可用时带诊断信息优雅降级到词法检索。
- **`fts`** 只做词法检索，用于诊断，不加载向量。
- **`hybrid`** 是严格模式：向量不可用时直接报错，不静默降级。

**请用 `auto`。** 纯词法检索对中文口语提问经常**返回零结果**——FTS 的分词只在提问用的词
原样出现在文本里时才匹配得上；让一句问话形式的查询真正可用的是语义检索。现实后果是：
**如果向量索引坏了，检索不会报错，只会安静地变成一个对这类提问沉默的引擎**，
所以 `auto` 会一并报告它实际用了哪种模式、以及有没有发生降级。

### 你自己的收藏夹

```powershell
favhub --root data collections
```

按条目数从大到小列出收藏夹，只统计平台仍然提供的条目，并给出每个平台有多少条目
**不属于任何收藏夹**。有名字的收藏夹是一次刻意的动作，标记着一个值得查阅的主题；
平台的默认收藏夹装的是一键收藏，什么也不标记。`unfiled` 这个计数之所以存在，
是因为"收藏夹"只是部分平台才有的东西——GitHub star 和 X 书签是平铺列表，
把收藏夹列表当成整个库来读会把它们完全藏起来。MCP 侧的 `favhub.collections` 暴露同一张地图，
favhub-ask Skill 会先读它，来判断这次检索值不值得那点延迟。

## 可选的本地语义检索

```powershell
uv sync --extra embedding
favhub --root data embeddings init
favhub --root data embeddings build
```

初始化是**唯一**允许下载模型的路径，构建阶段只做本地加载。默认模型是
`intfloat/multilingual-e5-small`（MIT 协议），缓存在 `<data-root>/models/` 下。
`--max-items` 限制尝试的任务数，`--force` 重建当前 profile 的向量而**不触碰**源快照和笔记，
`--quiet` 关闭进度心跳。

向量是 SQLite 里归一化的 384 维 float32 BLOB。查询时精确扫描候选向量，
再用等权 RRF 融合向量排名和 FTS 排名。这套方案适配个人库的规模，也避免了跑一个向量服务。
没有远程 embedding API、没有遥测、没有任何内容导出路径；参与嵌入的内容不离开本机。

```powershell
uv run python scripts/benchmark_m2b.py
```

会报告 1000/10000/50000 分块规模下的扫描与融合耗时、内存和平台元数据。
它描述的是当前这台机器，不是跨硬件的性能保证。

## MCP（stdio）

```powershell
uv run favhub-mcp --root data
```

在标准输入输出上收发以换行分隔的 JSON-RPC。客户端必须先 `initialize`，
再发 `notifications/initialized`，然后才能调用工具。参数用 camelCase，结果用 snake_case；
不接受任何凭证、本地路径或任意 URL。

| 分组 | 工具 |
| --- | --- |
| 检索 | `favhub.search`、`favhub.get_item`、`favhub.collections`、`favhub.status` |
| 浏览器采集 | `favhub.browser_start`、`favhub.browser_status`、`favhub.browser_resume`、`favhub.browser_cancel` |
| 采集入库 | `favhub.sync_start`、`favhub.sync_submit_batch`、`favhub.sync_pause`、`favhub.sync_finish`、`favhub.sync_status` |
| GitHub | `favhub.github_sync` |
| 补摘要 | `favhub.enrich_next`、`favhub.enrich_submit`、`favhub.enrich_skip` |

这个适配器是一个 stdio 进程，不是 HTTP 服务，也不是常驻守护进程。它适合对话式使用，
因为本地 embedding provider 能在同一个进程里保持热态。向量的初始化仍然是惰性的：
启动时什么都不加载，第一个语义查询才按需加载。

### 一个数据根只能有一个 FavHub

运行中的 FavHub 会在其整个生命周期内独占数据根的锁。这让 MCP 工具和 CLI 成为**二选一**
而不是互补关系：Agent 窗口开着的时候用 MCP 工具，没开的时候用 CLI。
CLI 被拒绝时会报出占着数据根的进程号，通常就是那个 Agent 窗口。

## 存储与引用

```text
items/<平台>/<源 ID>/
  source.json          采集快照，以及 enrichment 块
  content.md           系统生成，进索引
  transcript/0001.md   B 站字幕，进索引
  ocr/NNNN.md          X 图片描述，进索引
  assets/              原始响应，不进索引
  notes.md             你的，只创建一次，永不覆盖
```

检索结果里的 `local_path` 是相对数据根的路径（`items/x/2081.../content.md`），
不是机器相关的绝对路径。引用是形如 `favhub:<平台>/<源 ID>#chunk-<序号>` 的稳定标识。

本次采集不再产出的文件会被删除，但**只限于该次采集声明自己掌握全部内容的资产目录**。
B 站每次采集都会尝试取字幕，所以"没产出"是关于这个视频的一个结论；
X 只有在被要求时才产出图片描述，所以"没产出"什么也不意味着。
空集本身分辨不了这两者，所以由 mapper 显式声明，而不是让存储层去猜。

## 索引与状态

```powershell
favhub --root data reindex          # 入队缺失的索引任务
favhub --root data reindex --force  # 把当前所有快照重新过一遍索引
favhub --root data status           # 索引和向量状态
favhub --root data status JOB_ID    # 某个同步任务的进度
```

没有后台守护进程。补摘要会就地索引它改写的内容，`embeddings build` 会同时排空索引队列和向量队列；
除此之外的任务会一直堆积，直到其中之一被运行。`pending_index_tasks` 统计待处理和运行中的任务；
`failed_index_tasks` 统计带有错误记录的任务，而失败的任务会保持 pending 等待重试，
所以两个数字会重叠。

从备份恢复的、或在某个功能出现之前采集的数据根，可以用回填命令补齐：

```powershell
favhub --root data favtime-backfill
favhub --root data collections-backfill
favhub --root data access-backfill
```

## 开发

```powershell
uv sync --all-extras
```

extras 对开发不是可选的：语义检索在用到时才 import numpy，而测试覆盖了这条路径，
所以不装 extras 的环境会因缺少模块而挂在门禁上，而不是因为你改的东西。dev 组默认
就会安装，不需要额外加参数。

合并前的完整质量门禁：

```powershell
uv run pytest --cov
npm run test:extension
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv lock --check
git diff --check
```

`--cov` 在总体覆盖率低于 90% 时判失败，这条配在 `pyproject.toml` 里，
而不是留给"谁记得去看那个数字"。它**故意不放进 `addopts`**：
像 `pytest tests/test_chunking.py` 这样的聚焦运行会拿一个文件去对整个包计算覆盖率，
从而在一条它根本没打算满足的门槛上失败。各模块之间差异很大——`browser_launcher` 最低，
因为它要真的启动一个 Chrome——某个模块明显低于总体门槛，
是一个应当上报的发版关注点，而不是应当掩盖的事。

`scripts/smoke_browser_extension.ps1` 用一个位于明确临时根目录下的一次性 profile 启动 Chrome，
所以冒烟运行永远不会碰到你真实的 profile。它不提供任何凭证、不绕过任何登录：
你需要在那个 profile 里手动登录，和普通用户完全一样。

fixture 导入是确定性的演示与测试路径，不是连接器：

```powershell
uv run favhub --root .tmp/favhub import-fixture tests/fixtures/m2a-captured-items.json --mode full
```

它和真实采集走同样的 `SyncModule` 和 `LibraryModule`，
但对任何平台的线上 API、鉴权流程或页面结构都不构成证明。

## 不做的事

FavHub 自己不做 OCR、不做 ASR，不下载视频/封面/图片的二进制，不采集评论和弹幕，不索引代码，
不读取私有 star，不追踪外部链接，没有 GUI，没有守护进程，不自动调度任何东西，
也不做跨条目聚类。标签是自由文本，没有受控词表。图片描述和字幕只有在上游采集器
已经提供的情况下才会被索引。

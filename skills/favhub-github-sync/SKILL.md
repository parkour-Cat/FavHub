---
name: favhub-github-sync
description: 采集用户的 GitHub 公开 star 列表入 FavHub。当用户要求"同步 GitHub star 到 FavHub"时使用。整个采集由 FavHub 自己完成，Agent 只发一次工具调用，不发网络请求、不接触任何凭证。
---

# FavHub GitHub Stars 采集

GitHub 是唯一不需要浏览器的平台——star 列表是公开 REST 端点。因此**请求发生在 FavHub 进程里**，不在你这里：你调用一次 `favhub.github_sync`，分页、README 抓取、去重、入库、frontier 推进都在对面完成。

## 你要做的

1. **预检**：`favhub.status` 确认 MCP 可用。
2. **确认登录名**：`user` 是 GitHub 登录名（URL 里 `github.com/<这一段>`），不是昵称、不是邮箱。用户改过用户名的话旧名会 404。
3. **首次冒烟**：第一次对某个账号跑，先带 `maxScanItems: 10` 验证通路，再不带上限跑一次。这里说的是去掉 `maxScanItems`，不是把 `mode` 改成 `full`。
4. **调用**：

   | 参数 | 说明 |
   | --- | --- |
   | `user` | 必填，GitHub 登录名 |
   | `mode` | `incremental`（默认，遇到上次的 frontier 即停）或 `full`。只有要刷新已入库的仓库时才用 `full`——增量把库里已有的条目直接当重复跳过、不比对内容。**不要因为"第一次同步"就选 `full`**：还没有 frontier 时增量本来就会扫到列表末尾。 |
   | `maxScanItems` | 可选正整数，扫到这么多条就停 |

5. **读结果并复述**：`status.platforms[0].counts` 里的 `added` / `duplicates` 是这次的实际战果；`readmes_missing` 是没有 README 因而只有 description 的仓库数（正常现象，不是失败）；`authenticated` 说明这次有没有用上凭证。

## 凭证：你永远不碰它

可选凭证由用户设在**他自己的环境变量** `FAVHUB_GITHUB_TOKEN` 里，FavHub 在发请求那一刻读取，不落盘、不写日志、不进结果。

- **决不**向用户索取该值，**决不**让他把值贴进对话，**决不**把值写进任何文件或命令行。需要他配置时，只说变量名和"请在你的环境里设置"。
- 结果里只有 `authenticated: true/false`，你看不到也不需要看到值。
- **决不**自己去请求 `api.github.com` 或 `raw.githubusercontent.com`——那是 FavHub 的事，你插手只会把凭证问题拉回到你的上下文里。

没配也能跑：未认证是每小时 60 次、**按 IP 计**，共享出口地址会被别人提前耗光。配了是 5000 次并且计在用户自己账上。

## 只采公开 star

即使配了凭证也只采公开 star。凭证会让 star 端点顺带返回"私有可见"的 star，但因为认证方式变了就多采一批，是没有人做过的决定。用户要求采私有 star 时，如实说不支持。

## 出错怎么讲

平台侧的失败**不抛异常**：返回体里带 `error: {code, message}`，该 job 已被 pause，frontier 没有推进。修好原因后**直接重跑**即可（会开一个新 job），增量模式会从上次确认过的位置继续，不会重复入库。四种 code，对应四种完全不同的话：

- `source_unavailable` —— 这个用户名在 GitHub 上不存在。请用户确认登录名，别猜。
- `login_required` —— GitHub 拒绝了环境里的凭证（过期、被吊销、权限不对或贴错）。请用户自行更换，**依然不要问他要值**。
- `rate_limited` —— 配额用尽。等窗口重置再跑；未认证的话，建议他配 `FAVHUB_GITHUB_TOKEN`。
- `page_changed` —— GitHub 的响应结构变了。这是 FavHub 需要修的 bug，如实报告并停止，不要尝试绕过。

## 边界

- 只读：**决不** star/unstar，决不修改任何 GitHub 状态。
- **决不**用 fixture 测试的结果宣称真实采集成功。
- 扫描被截断时（`maxScanItems` 或配额不足）frontier 不推进，下次会从同一处重来——这是刻意的，不要手工"补上"。

## CLI 备选与它的陷阱

同一套实现也有 CLI：

```powershell
favhub github-sync --user <login> --mode incremental
```

但 **MCP server 在运行时会独占数据根锁**，那时候 CLI 会直接报 "data root is already in use"。换句话说 CLI 只在用户没开 FavHub 时可用——所以在对话里优先用 MCP 工具。

## 参考

端点契约细节（分页头、限速语义、错误形状）见 `scripts/probe_github_contract.md`。这些细节是 FavHub 实现要管的，你不需要照着它发请求。

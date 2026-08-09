# 同源只读请求：扩展做什么，Agent 不做什么

Bilibili 用同源会话鉴权，因此页面可以自己请求下一页——不需要拦截，也不需要向
页面注入任何代码。这是与 X 相反的形态：X 只能被动看，Bilibili 主动问。

**这件事完全由 FavHub 浏览器扩展完成，Agent 不参与。** 本文档描述扩展的行为，
以便 Agent 如实向用户解释正在发生什么，而不是一份操作指南。
决不发起跨域业务请求、决不访问任意 URL、决不读取或导出 Cookie/Token/SESSDATA 等凭证。

## 扩展请求的端点

| 用途 | 端点（同源 GET） |
| --- | --- |
| 账号身份 | `/x/web-interface/nav`（取当前登录账号，与知乎 `/api/v4/me` 同构） |
| 收藏夹列表 | `/x/v3/fav/folder/created/list-all` |
| 收藏夹内容分页 | `/x/v3/fav/resource/list`（`pn` 递增，观察 `has_more`） |
| 视频详情 | `/x/web-interface/view` |
| 字幕轨道 | `/x/player/v2` |
| 字幕文档 | 轨道指向的字幕 JSON（`body[]` 时间戳 cue） |

这些构成**固定白名单**，由内容脚本在发起请求前再校验一次：Service Worker 说出
一个 URL 不等于有权请求它，否则一个被攻陷的 Worker 就能把页面当作转发代理。
收藏夹的删除/移动等相邻端点不在白名单内，且扩展只发 GET。

## 凭证的去向

- 只有平台自己的站点会收到用户的会话；字幕文档来自独立 CDN，请求**不带凭证**。
- 这不只是"少给一点"的问题：该 CDN 返回 `Access-Control-Allow-Origin: *`，
  而按 Fetch 标准带凭证的请求不接受通配来源，带上凭证会让每一条字幕都
  静默失败、看起来像"这些视频都没有字幕"。字幕链接本身已带签名参数。
- 该签名参数有效期很短，因此链接取到即用，决不缓存。

## 响应形状

冻结样例见仓库 `tests/fixtures/bilibili/`；实际解析一律交给 FavHub 纯解析器，
解析失败会得到 `login_required` / `page_changed` / `source_unavailable` /
`subtitle_unavailable` 等稳定错误码。

## 脱敏规则

- 只提交响应**正文**中的业务字段；不得附带请求头、响应头或任何会话凭证。
- 不得在 `platformMetadata`、`body` 或资产文本中嵌入用户自己的 `mid` 之外的隐私信息；
  作者公开信息（名称、公开 mid）可以保留。
- 日志与进度报告只写：计数、稳定错误码、收藏夹 ID/名称、视频 ID；决不粘贴原始响应全文。
- 若响应是登录 HTML 而非 JSON：这是 `login_required` 信号，决不当作空列表处理。

## 与真实会话探针的关系

`scripts/probe_bilibili_contract.md` 描述如何把真实会话中的脱敏响应固化为 fixture。
仓库中的 fixture 为**合成样例**；只有真实会话冒烟通过后才能宣称连接器可用。

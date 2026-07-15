# TikHub 短剧项目接口文档（企业微信前端接入）

> 文档日期：2026-07-15
>
> 线上后端：`https://paqu-tikhub-proxy.onrender.com`
>
> 接口实现：`tikhub_proxy.py`
>
> 当前公开前端：`index.html` / `tikhub-report-frontend.html`

## 1. 接入结论

企业微信前端不需要直接连接 TikHub，也不要直接连接 Supabase。统一请求本项目的 Render 后端：

```js
const API_BASE = "https://paqu-tikhub-proxy.onrender.com";
```

当前六类页面与接口的对应关系如下：

| 页面 | 主要接口 | 说明 |
| --- | --- | --- |
| 运营数据分析看板 | `GET /supabase/latest`、`GET /supabase/reports`、`GET /supabase/report` | 热度、1/3/7/30 天增长等由前端根据最新报表和历史报表计算 |
| 公司短剧榜单 | `GET /curated-catalog` | 只返回后台已认领且已上架的公司短剧 |
| 当前监控账号看板 | `GET /supabase/latest` | 页面卡片来自最新报表的 `summary`，不是直接读取后台账号池 |
| 报表－账号汇总 | `GET /supabase/latest` | 使用 `summary` 数组 |
| 报表－账号明细 | `GET /supabase/latest` | 使用 `dramas_detail` 数组，筛选、排序、分页均在前端完成 |
| 发现账号／发现作品 | `GET /discover-accounts` | 管理接口，需要公司后端代请求，不能把任务密码写进企业微信前端 |

数据链路：

```text
TikHub / TikTok
      ↓ 定时抓取
Render 后端 tikhub_proxy.py
      ↓ 写入
Supabase（report_runs、accounts、dramas 等表）
      ↓ 由 Render 封装成接口
企业微信 H5 / 当前网页
```

## 2. 鉴权与安全

### 2.1 公开只读接口

以下接口在当前线上配置中可由企业微信前端直接调用：

- `GET /health`
- `GET /supabase/latest`
- `GET /supabase/reports`
- `GET /supabase/report`
- `GET /curated-catalog`
- `GET /schedule-status`（未鉴权时只返回精简任务状态）
- `GET /drama-link?target=list...`（只返回分集元数据，不返回可下载文件）

后端当前允许跨域请求：

```http
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, OPTIONS
```

企业微信应用仍需在企业微信管理后台按实际部署方式配置可信域名。

### 2.2 受保护的管理接口

管理接口使用请求头：

```http
X-Schedule-Secret: <SCHEDULE_SECRET>
```

受保护接口包括：

- `GET/POST /schedule-accounts`
- `GET /discover-accounts`
- `POST /discover-accounts`
- `GET /run-scheduled`
- `GET/POST /admin/catalog`
- `GET /admin/access`
- 播放源、ZIP、下载脚本等私有媒体接口

**禁止在企业微信 H5、JavaScript 文件、localStorage 或接口地址中保存 `SCHEDULE_SECRET`。** 浏览器前端代码对用户可见，写入前端等于公开密码。

推荐结构：

```text
企业微信前端
    ↓ 调公司自己的业务后端（已验证企业微信用户身份）
公司业务后端 / BFF
    ↓ 服务端添加 X-Schedule-Secret
Render 接口
```

服务端代请求示意：

```js
// 只能运行在公司后端，不能放在浏览器中。
const response = await fetch(
  "https://paqu-tikhub-proxy.onrender.com/schedule-accounts",
  {
    headers: {
      "X-Schedule-Secret": process.env.PAQU_SCHEDULE_SECRET
    }
  }
);
```

当前 `/admin` 页面会由同域后端自动签发 HttpOnly 管理 Cookie，所以原后台不用手输密码；企业微信页面如果部署在其他域名，不能依赖这枚 `SameSite=Strict` Cookie，应使用公司后端代请求方案。

旧版的 `?secret=...` 查询参数不支持，也不要使用。

## 3. 通用约定

### 3.1 请求格式

- 接口默认返回 UTF-8 JSON。
- POST 请求使用 `Content-Type: application/json`。
- 账号可传 `account_name`、`@account_name` 或 TikTok 主页链接，后端会清洗为账号 ID。
- 时间主要使用北京时间 ISO 8601，例如：`2026-07-15T14:14:51+08:00`。
- 部分短剧发布时间为 `YYYY-MM-DD HH:mm:ss`，该字段按北京时间理解。

### 3.2 建议的请求封装

```js
async function apiGet(path, params = {}) {
  const url = new URL(path, API_BASE);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  });
  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" }
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
```

### 3.3 时间展示

```js
function parseProjectTime(value) {
  if (!value) return null;
  let text = String(value).trim();
  // 没有时区的短剧发布时间按北京时间解释。
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(text)) {
    text = text.replace(" ", "T") + "+08:00";
  }
  return new Date(text);
}

function formatBeijingTime(value) {
  const date = parseProjectTime(value);
  return date
    ? new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
      }).format(date)
    : "";
}
```

## 4. 报表接口

### 4.1 获取最新完整报表

```http
GET /supabase/latest
```

完整地址：

```text
https://paqu-tikhub-proxy.onrender.com/supabase/latest
```

用于：

- 运营看板的当前数据
- 当前监控账号看板
- 账号汇总
- 账号明细

顶层响应：

```json
{
  "generated_at": "2026-07-15T14:14:51+08:00",
  "accounts": 32,
  "dramas": 4144,
  "errors": [],
  "summary": [],
  "dramas_detail": [],
  "storage_source": "supabase",
  "supabase_run_id": 123
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `generated_at` | string | 报表生成时间，北京时间 |
| `accounts` | number | 本次成功写入报表的账号数 |
| `dramas` | number | 本次短剧来源记录总数 |
| `errors` | array | 抓取失败账号或错误信息 |
| `summary` | array | 账号汇总；账号看板和账号汇总页的数据源 |
| `dramas_detail` | array | 全部短剧来源明细；账号明细页的数据源 |
| `storage_source` | string | 当前一般为 `supabase` |
| `supabase_run_id` | number | Supabase 中本次报表记录 ID |

`summary` 单条记录：

```json
{
  "截图名称": "Daily Shorts",
  "账号": "freedailyshorts",
  "昵称": "Daily Shorts",
  "头像": "https://...",
  "粉丝": 2184300,
  "点赞": 55897200,
  "短剧数": 29,
  "总集数": 1759,
  "累计观看": 1485000000,
  "单剧均观看": 51210000,
  "最高观看短剧": "Say Yes to My Tomboy Roommate",
  "最高观看短剧中文名": "对我的假小子室友说“是”",
  "最高观看": 24300000,
  "主页链接": "https://www.tiktok.com/@freedailyshorts"
}
```

`dramas_detail` 单条记录的核心字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `Account / 账号` | string | TikTok 账号 ID |
| `Nickname / 昵称` | string | 账号昵称 |
| `Rank in Account / 账号内排序` | number | 该剧在账号内的排序 |
| `Drama ID / 短剧ID` | string | TikHub/TikTok 短剧 ID，可能带 `ID ` 前缀 |
| `English Title / 英文剧名` | string | 英文剧名 |
| `Chinese Title / 中文剧名` | string | 中文译名 |
| `Publish Time / 发布时间` | string | 发布时间，按北京时间理解 |
| `Episodes / 集数` | number | 集数 |
| `Views / 观看数` | number | 累计播放量 |
| `Duration Seconds / 总时长(秒)` | number | 总时长（秒） |
| `Duration Minutes / 总时长(分钟)` | number | 总时长（分钟） |
| `Limited Free / 是否限免` | string | 是否限免 |
| `English Themes / 英文题材` | string | 英文题材，通常用 `、` 分隔 |
| `Chinese Themes / 中文题材` | string | 中文题材 |
| `English Description Preview / 英文简介预览` | string | 英文简介 |
| `Chinese Description / 中文简介` | string | 中文简介 |
| `Drama Link / 短剧链接` | string | 短剧链接，可能为空 |
| `Source Profile URL / 来源主页` | string | 发布账号主页 |

响应中还保留 `账号`、`昵称`、`短剧名`、`集数`、`累计观看`、`单集均观看`、`主页链接`、`短剧链接` 等中文兼容字段。新前端建议优先使用上表中的双语主字段，并为旧数据兼容中文字段。

### 4.2 获取历史报表目录

```http
GET /supabase/reports?limit=30
```

参数：

| 参数 | 必填 | 范围 | 说明 |
| --- | --- | --- | --- |
| `limit` | 否 | 1～200 | 返回最近多少次报表，默认由后端配置决定 |

响应：

```json
{
  "ok": true,
  "source": "supabase",
  "reports": [
    {
      "name": "supabase_report_123.json",
      "title": "2026-07-15T14:14:51+08:00",
      "generated_at": "2026-07-15T14:14:51+08:00",
      "modified": "2026-07-15T14:15:10+08:00",
      "accounts": 32,
      "dramas": 4144,
      "path": "/supabase/report?id=123",
      "source": "supabase"
    }
  ]
}
```

### 4.3 获取某次历史报表

```http
GET /supabase/report?id=123
GET /supabase/report?id=123&compact=1
```

参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 历史目录中的报表 ID |
| `compact` | 否 | `1` 返回精简字段，适合历史趋势计算 |

`compact=1` 响应结构：

```json
{
  "generated_at": "2026-07-14T14:14:51+08:00",
  "accounts": 32,
  "dramas": 4100,
  "storage_source": "supabase",
  "supabase_run_id": 122,
  "summary": [
    { "a": "freedailyshorts", "n": "Daily Shorts", "d": 29, "e": 1759, "v": 1470000000 }
  ],
  "dramas_detail": [
    {
      "a": "freedailyshorts",
      "n": "Daily Shorts",
      "id": "765...",
      "en": "English title",
      "cn": "中文剧名",
      "p": "2026-06-01 12:00:00",
      "e": 60,
      "v": 12300000,
      "et": "Romance",
      "ct": "爱情"
    }
  ]
}
```

精简字段对照：

| 精简字段 | 含义 |
| --- | --- |
| `a` | account / 账号 |
| `n` | nickname / 昵称 |
| `d` | drama count / 短剧数 |
| `e` | episodes / 集数 |
| `v` | views / 累计观看 |
| `id` | 短剧 ID |
| `en` | 英文剧名 |
| `cn` | 中文剧名 |
| `p` | 发布时间 |
| `et` | 英文题材 |
| `ct` | 中文题材 |

### 4.4 运营看板的计算规则

运营看板不是从一个“看板接口”直接获取最终结果，而是：

1. 调 `/supabase/latest` 取得当前完整报表。
2. 调 `/supabase/reports?limit=30` 取得历史目录。
3. 取最近 10 次记录，调用 `/supabase/report?id=...&compact=1`。
4. 前端按同一短剧 ID，或“账号 + 剧名”匹配不同日期的同一部剧。

当前网页的主要计算口径：

| 展示项 | 计算方式 |
| --- | --- |
| 当前热度最高 | 当前 `dramas_detail` 中 `Views / 观看数` 最大的剧 |
| 近 1/3/7 天涨幅 | 当前累计播放 - 对应天数以前最近一条可用历史累计播放；最低按 0 展示 |
| 近 30 天热度新增榜 | 30 天窗口内最后一次累计播放 - 第一次累计播放，至少需要 2 个历史点 |
| 热门题材占比 | 当前每部剧按题材聚合播放量，取前 5 类 |
| 本月题材播放增长 | 当前题材总播放 - 历史基准题材总播放 |
| 本周新增短剧 | 当前短剧总数 - 7 天基准短剧总数 |
| 增长最多账号 | 当前账号短剧数 - 7 天前该账号短剧数，取最大值 |

因此历史点不足时，页面应显示“暂无历史基准”，不能用 0 伪造涨幅。

## 5. 公司短剧公开榜单

### 5.1 获取已上架公司短剧

```http
GET /curated-catalog
```

无需鉴权，只返回后台已认领且 `online=true` 的公司短剧；不返回内部备注。

响应：

```json
{
  "ok": true,
  "generated_at": "2026-07-15T14:14:51+08:00",
  "updated_at": "2026-07-15T15:23:00+08:00",
  "revision": 8,
  "storage": "supabase",
  "count": 1,
  "dramas": [],
  "ranking": []
}
```

`dramas` 按后台手动顺序排列，`ranking` 按累计播放从高到低排列。两者中的单条结构相同：

```json
{
  "id": "drama-xxxx",
  "chinese_title": "双倍保质期：马克的贪婪",
  "english_title": "Double Shelf Life: Mark's Greed",
  "writer": "编剧姓名",
  "producer": "制作/制片",
  "director": "导演",
  "cast": "主演",
  "aliases": ["历史剧名"],
  "online": true,
  "order": 1,
  "accounts": ["aidramalabs_anime2"],
  "source_count": 1,
  "active_source_count": 1,
  "total_views": 885000000,
  "episodes": 30,
  "latest_publish_time": "2026-05-11 09:06:38",
  "themes": ["复仇、奇幻、三角恋"],
  "rank": 1,
  "sources": []
}
```

`sources` 单条常用字段：

| 字段 | 说明 |
| --- | --- |
| `key` | 来源记录唯一键 |
| `account` | 发布账号 |
| `nickname` | 账号昵称 |
| `drama_id` | 原始短剧 ID |
| `english_title` / `chinese_title` | 该来源中的剧名 |
| `publish_time` | 发布时间 |
| `episodes` | 集数 |
| `views` | 该来源累计播放 |
| `themes` | 题材 |
| `link` | 短剧链接 |
| `profile_url` | 发布账号主页 |

同一部公司短剧可能有多个 `sources`，`total_views` 是所有有效来源播放量之和，`accounts` 是去重后的发布账号。

## 6. 当前监控账号看板

公开的“当前监控账号看板”不需要单独接口，直接复用：

```http
GET /supabase/latest
```

使用 `summary` 渲染账号卡片：

- 账号数：`summary.length` 或顶层 `accounts`
- 总粉丝：所有 `summary[].粉丝` 之和
- 累计观看：所有 `summary[].累计观看` 之和
- 昵称、头像、主页、最高观看短剧：对应账号记录中的同名字段

注意两种“账号数”的区别：

| 数据 | 含义 |
| --- | --- |
| `/supabase/latest` 的 `accounts` | 本次抓取成功、进入最新报表的账号数 |
| `/schedule-accounts` 的 `count` | 后端权威监控账号池数量，可能包含本次抓取失败的账号 |

所以最新报表显示 32 个、后台账号池显示 34 个并不矛盾；通常代表有 2 个账号本次抓取失败。

## 7. 发现账号与发现作品（受保护）

这些接口会调用 TikHub、消耗余额，并可能需要较长时间，只能由公司后端代请求。

### 7.1 读取上一次发现账号结果

```http
GET /discover-accounts?mode=accounts&run=0
X-Schedule-Secret: <server-side only>
```

响应：

```json
{
  "ok": true,
  "generated_at": "2026-07-15T14:00:00+08:00",
  "keywords": ["mini drama"],
  "count": 1,
  "accounts": [],
  "errors": [],
  "search_candidates": 20,
  "monitored_count": 34,
  "runtime_seconds": 12.5
}
```

账号候选核心字段：

| 字段 | 说明 |
| --- | --- |
| `account` / `nickname` / `avatar` | 账号基础资料 |
| `followers` / `hearts` / `video_count` | 粉丝、点赞、视频数 |
| `dramas` / `total_views` | 短剧数、短剧累计播放 |
| `top_drama` / `top_drama_views` | 最高播放短剧 |
| `sample_video_link` / `sample_desc` | 样本作品 |
| `source_keywords` | 发现该账号的关键词 |
| `profile_url` | TikTok 主页 |
| `already_monitored` | 是否已经在监控池中 |
| `enrich_error` | 资料补全错误，可能为空 |

### 7.2 执行发现账号

```http
GET /discover-accounts?mode=accounts&run=1&keywords=mini%20drama&limit=40&min_followers=0&min_dramas=0&max_videos=50
X-Schedule-Secret: <server-side only>
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `keywords` | 后端默认关键词 | 关键词、`@账号` 或 TikTok 主页链接，可用逗号或换行分隔 |
| `limit` | 后端配置 | 最大候选账号数，接口上限 500 |
| `min_followers` | 0 | 最低粉丝数 |
| `min_dramas` | 0 | 最低短剧数 |
| `max_videos` | 后端配置 | 每个关键词最多搜索视频数，接口上限 200 |

### 7.3 读取或执行发现作品

读取上次结果：

```http
GET /discover-accounts?mode=works&run=0
X-Schedule-Secret: <server-side only>
```

执行发现：

```http
GET /discover-accounts?mode=works&run=1&queries=https%3A%2F%2Fwww.tiktok.com%2F%40account%2Fvideo%2F123&limit=40&max_videos=50
X-Schedule-Secret: <server-side only>
```

`queries` 支持关键词、`@账号`、TikTok 主页链接、作品链接或分享短链接。

作品候选核心字段：

| 字段 | 说明 |
| --- | --- |
| `video_id` / `video_url` | 作品 ID 和作品页 |
| `description` | 作品文案 |
| `account` / `nickname` / `avatar` | 发布账号 |
| `publish_time` | 发布时间 |
| `views` / `likes` / `comments` / `shares` | 互动数据 |
| `profile_url` | 账号主页 |
| `source_queries` | 来源搜索词 |
| `already_monitored` | 发布账号是否已监控 |
| `drama_id` / `drama_title` / `episode_count` | 能识别到短剧时返回 |

### 7.4 将候选账号加入监控池

推荐直接调用 `/schedule-accounts` 的 append 模式，见下一节。`POST /discover-accounts` 也能追加，但保留它主要是为了兼容现有前端。

## 8. 后端监控账号池与抓取任务（受保护）

### 8.1 读取权威监控账号池

```http
GET /schedule-accounts
X-Schedule-Secret: <server-side only>
```

响应：

```json
{
  "ok": true,
  "accounts": ["account_a", "account_b"],
  "count": 2,
  "source": "SCHEDULE_ACCOUNTS+seed+backend_pool+supabase",
  "updated_at": "2026-07-15T14:00:00+08:00",
  "runtime_file": "reports/schedule_accounts.json"
}
```

后端会自动去重合并四个来源：

1. Render 环境变量 `SCHEDULE_ACCOUNTS`
2. GitHub 种子文件 `schedule_accounts.seed.json`
3. 后端动态池 `reports/schedule_accounts.json`
4. Supabase `accounts` 表

### 8.2 追加账号

```http
POST /schedule-accounts
Content-Type: application/json
X-Schedule-Secret: <server-side only>

{
  "mode": "append",
  "accounts": [
    "account_a",
    "@account_b",
    "https://www.tiktok.com/@account_c"
  ]
}
```

响应：

```json
{
  "ok": true,
  "mode": "append",
  "added": ["account_c"],
  "added_count": 1,
  "accounts": ["account_a", "account_b", "account_c"],
  "count": 3,
  "source": "backend_pool",
  "supabase": { "ok": true, "accounts": 3 },
  "updated_at": "2026-07-15T14:00:00+08:00"
}
```

前端新增账号统一使用 `mode: "append"`。`mode: "replace"` 会重写动态池，除非在做全量管理工具，否则不要使用。

### 8.3 查看抓取状态

```http
GET /schedule-status
```

未鉴权响应只包含安全字段：

```json
{
  "ok": true,
  "job": {
    "running": false,
    "started_at": "2026-07-15T14:00:00+08:00",
    "finished_at": "2026-07-15T14:14:51+08:00"
  },
  "schedule_account_count": 34,
  "schedule_account_source": "seed+backend_pool+supabase"
}
```

带服务端鉴权时会返回完整任务详情和错误信息。

### 8.4 手动启动抓取

异步启动（推荐）：

```http
GET /run-scheduled?wait=0
X-Schedule-Secret: <server-side only>
```

成功返回 HTTP `202`，随后轮询 `/schedule-status`。

同步等待：

```http
GET /run-scheduled?wait=1
X-Schedule-Secret: <server-side only>
```

同步等待可能耗时很久，不建议由企业微信页面直接保持请求。

如果任务已在运行，返回 HTTP `409`。

## 9. 公司短剧后台管理接口（受保护）

企业微信只做公开展示时不需要本节；如果要把认领、归属、上下架也迁入企业微信，则由公司后端代请求。

### 9.1 读取后台完整目录

```http
GET /admin/catalog
X-Schedule-Secret: <server-side only>
```

响应主要字段：

| 字段 | 说明 |
| --- | --- |
| `catalog` | 后台正式配置，包含 `revision`、`dramas`、`sources` |
| `storage` | `supabase`、`runtime_file` 或缓存来源 |
| `generated_at` | 当前抓取报表时间 |
| `sources` | 最新报表中的全部短剧来源，供待认领列表使用 |
| `source_count` | 来源记录数 |
| `accounts` | 最新报表账号列表 |
| `account_count` | 最新报表账号数 |
| `curated` | 包含未上架作品的后台榜单数据 |

`catalog` 结构：

```json
{
  "version": 2,
  "revision": 8,
  "updated_at": "2026-07-15T15:23:00+08:00",
  "dramas": {
    "drama-xxxx": {
      "id": "drama-xxxx",
      "chinese_title": "中文剧名",
      "english_title": "English title",
      "writer": "编剧",
      "producer": "制作/制片",
      "director": "导演",
      "cast": "主演",
      "aliases": [],
      "notes": "内部备注",
      "online": true,
      "order": 1,
      "created_at": "...",
      "updated_at": "..."
    }
  },
  "sources": {
    "opaque-source-key": {
      "status": "owned",
      "drama_id": "drama-xxxx",
      "updated_at": "..."
    }
  }
}
```

来源 `status` 只能是：

- `pending`：待认领
- `owned`：已认领，必须带有效 `drama_id`
- `ignored`：非公司作品

### 9.2 保存后台目录

```http
POST /admin/catalog
Content-Type: application/json
X-Schedule-Secret: <server-side only>

{
  "expected_revision": 8,
  "catalog": { "version": 2, "revision": 8, "dramas": {}, "sources": {} }
}
```

保存成功后 `revision` 自动加 1。若其他管理员已先保存，返回 HTTP `409`，并在响应中带最新 `catalog`；前端必须提示用户刷新后重试，不能覆盖最新修改。

请求体最大 8 MB。

### 9.3 查看后台权限

```http
GET /admin/access
X-Schedule-Secret: <server-side only>
```

返回当前超级管理员角色、权限列表，以及 TikHub、Supabase 服务是否已经配置。它只用于状态展示，不应该用于替代公司自身的用户身份鉴权。

## 10. 分集列表、播放与下载

### 10.1 获取一部剧的分集元数据

```http
GET /drama-link?uid=account_name&drama_id=765...&target=list&redirect=0
```

响应：

```json
{
  "ok": true,
  "target": "list",
  "uid": "account_name",
  "drama_id": "765...",
  "count": 60,
  "episodes": [
    {
      "index": 1,
      "episode_no": 1,
      "episode_label": "第1集",
      "video_id": "765...",
      "title": "EP01",
      "publish_time": "2026-07-01 10:00:00",
      "views": 12345,
      "views_text": "1.23万",
      "video_url": "https://www.tiktok.com/@account_name/video/765...",
      "play_url": "/drama-link?...target=play..."
    }
  ]
}
```

### 10.2 私有媒体能力

以下 `target` 需要服务端鉴权：

- `play` / `source` / `direct` / `media`
- `series`
- `zip` / `download`
- `local_script`

不要把带密码的下载地址发给浏览器。企业微信中如果要开放下载，应由公司后端先验证用户权限，再由服务端调用 Render；大文件还应考虑转存对象存储后下发短期签名地址。

## 11. 企业微信前端推荐的数据层

不要让六个页面各自重复下载最新报表。`/supabase/latest` 可能有数 MB，重复请求会明显拖慢页面。

推荐在应用入口创建一个共享数据仓库：

```js
const reportStore = {
  latest: null,
  latestPromise: null,
  history: [],

  getLatest() {
    if (this.latest) return Promise.resolve(this.latest);
    if (!this.latestPromise) {
      this.latestPromise = apiGet("/supabase/latest")
        .then(data => (this.latest = data))
        .finally(() => (this.latestPromise = null));
    }
    return this.latestPromise;
  }
};
```

建议加载顺序：

1. 应用启动先显示骨架屏或“正在加载最新报表”。
2. 只请求一次 `/supabase/latest`，账号看板、汇总、明细共用同一对象。
3. 先用最新数据渲染可见页面。
4. 后台加载 `/supabase/reports?limit=10`。
5. 历史详情统一使用 `compact=1`，并发数建议 2～3，完成后再显示涨幅。
6. 切换页面时不要重复请求；用户主动点击刷新时才清理内存缓存。
7. 公司榜单 `/curated-catalog` 单独缓存，可按 `revision` 判断是否变化。

建议将接口地址放在环境配置中：

```js
export const config = {
  apiBase: import.meta.env.VITE_PAQU_API_BASE ||
    "https://paqu-tikhub-proxy.onrender.com"
};
```

## 12. 常见状态码

| 状态码 | 含义 | 前端处理 |
| --- | --- | --- |
| `200` | 成功 | 正常处理 |
| `202` | 抓取任务已异步启动 | 显示运行中并轮询状态 |
| `400` | 参数错误 | 展示后端 `error` |
| `403` | 缺少或错误的管理凭证 | 不要重试；检查公司后端配置 |
| `404` | 报表、短剧或播放源不存在 | 展示空状态或稍后重试 |
| `409` | 任务正在运行，或后台目录版本冲突 | 读取最新状态/目录后再操作 |
| `413` | 后台目录请求体超过 8 MB | 减少请求内容 |
| `500` | 后端处理失败 | 记录 `error`，允许用户重试 |
| `502` | TikHub 或上游解析失败 | 稍后重试，并保留错误信息 |
| `503` | Render 环境变量或服务未配置 | 联系后端管理员 |

错误响应通常为：

```json
{
  "ok": false,
  "error": "错误说明"
}
```

## 13. 源码定位

| 功能 | 文件 |
| --- | --- |
| 所有 HTTP 路由和数据封装 | `tikhub_proxy.py` |
| 运营看板、监控账号、报表、发现页 | `index.html` 和完全同步的 `tikhub-report-frontend.html` |
| 公司短剧公开榜单 | `catalog.html`、`catalog.js` |
| 后台管理面板 | `admin.html`、`admin.js` |
| 后端默认账号种子 | `schedule_accounts.seed.json` |
| 静态报表兜底 | `public_reports/` |

接口路由入口位于 `tikhub_proxy.py` 的 `Handler.do_GET` 和 `Handler.do_POST`。公开前端的实际数据请求可搜索：

- `/supabase/latest`
- `/supabase/reports`
- `/supabase/report`
- `/curated-catalog`
- `/discover-accounts`
- `/drama-link`

## 14. 企业微信联调检查单

- [ ] 企业微信前端的 `API_BASE` 指向 Render 域名。
- [ ] 公开页面只调用公开只读接口。
- [ ] `SCHEDULE_SECRET`、TikHub API Key、Supabase Service Key 都没有出现在前端代码或网络地址中。
- [ ] 管理接口由公司后端代请求，并先验证企业微信用户身份和权限。
- [ ] `/supabase/latest` 在整个应用中只下载一次并共享。
- [ ] 历史报表使用 `compact=1`，并限制并发数。
- [ ] 所有等待数据的页面有加载状态和失败重试状态。
- [ ] 时间统一按 `Asia/Shanghai` 展示。
- [ ] 明确区分“最新报表账号数”和“后台监控池账号数”。
- [ ] 公司短剧榜单只使用 `/curated-catalog`，不直接读取后台 `catalog`。

# 后端定时自动抓取

线上 Render Starter 服务由后端进程直接负责定时抓取，时区固定为北京时间（UTC+8）：

- 每天 `00:00`（晚上 12 点）抓取一次。
- 每天 `12:00`（中午 12 点）抓取一次。
- 每 15 秒检查一次是否到达计划时段。
- 单次失败最多自动尝试 3 次，默认依次等待 5 分钟、10 分钟后重试。
- 服务重启或重新部署后会读取 Supabase 最新报表时间；若本时段还没有成功报表，会自动补抓。
- 同一进程使用任务锁避免定时、手动和页面触发的抓取重叠运行。
- Supabase 中本时段已有新报表时，不会因为服务重启而重复抓取。

`.github/workflows/scheduled-report.yml` 不再配置自动 cron，只保留 GitHub 后台的手动应急触发入口，避免 GitHub Actions 队列延迟造成时间不准或与后端重复执行。

## Render 环境变量

```text
TIKHUB_API_KEY=你的 TikHub API Key
SCHEDULE_SECRET=你自己设置的一串密码
SCHEDULE_ACCOUNTS=账号1,账号2,账号3

INTERNAL_SCHEDULER_ENABLED=1
INTERNAL_SCHEDULE_TIMES=00:00,12:00
INTERNAL_SCHEDULER_POLL_SECONDS=15
INTERNAL_SCHEDULER_MAX_ATTEMPTS=3
INTERNAL_SCHEDULER_RETRY_SECONDS=300

SCHEDULE_MAX_VIDEOS=100
SCHEDULE_USE_PLAYLISTS=1
SCHEDULE_MAX_PLAYLISTS=300
SCHEDULE_PAGE_SIZE=30
SCHEDULE_PLAYLIST_PAGE_SIZE=20
SCHEDULE_PLAYLIST_VIDEO_PAGE_SIZE=30
SCHEDULE_DELAY_MS=300
SCHEDULE_ACCOUNT_WORKERS=4
TIKHUB_RPS_LIMIT=18
TIKTOK_RPS_LIMIT=8
```

`INTERNAL_SCHEDULE_TIMES` 支持使用英文逗号分隔多个 `HH:MM` 时间。线上默认值由 `render.yaml` 固定为 `00:00,12:00`。

上述并发配置适用于已经单独购买 TikHub 20 RPS 套餐的账号。`18` 是安全上限，不等于把接口余额充值到 5 美元；RPS 套餐与按请求扣费余额是两项独立配置。如果 TikHub 后台显示的上限仍为 10 RPS，请把 `TIKHUB_RPS_LIMIT` 改为 `8`。

抓取账号池由 Render 环境变量、GitHub 种子文件、后端账号池和 Supabase 已保存账号合并去重。后台管理面板新增账号后，会进入后端监控池；`SCHEDULE_ACCOUNTS` 继续作为部署环境兜底。

默认优先走 TikHub 播放列表/合集接口统计短剧数量；如果某个账号拿不到可用合集，或者合集返回的集数/播放量明显为空，才会退回按公开视频标题自动归类。`SCHEDULE_MAX_VIDEOS` 只影响这个退回方案。

## 手动触发和查看

敏感接口只接受 `X-Schedule-Secret` 请求头，不接受网址中的 `?secret=`，避免密码进入浏览器历史、日志和分享链接。

手动触发：

```bash
curl "https://paqu-tikhub-proxy.onrender.com/run-scheduled?wait=1" \
  -H "X-Schedule-Secret: 你的SCHEDULE_SECRET"
```

查看任务和内部定时器状态：

```bash
curl "https://paqu-tikhub-proxy.onrender.com/schedule-status" \
  -H "X-Schedule-Secret: 你的SCHEDULE_SECRET"
```

返回结果里的 `internal_scheduler` 会显示是否启用、北京时间计划、下一次运行时间、当前时段、重试次数和最近错误。

查看账号池：

```bash
curl "https://paqu-tikhub-proxy.onrender.com/schedule-accounts" \
  -H "X-Schedule-Secret: 你的SCHEDULE_SECRET"
```

保存账号池：

```bash
curl -X POST "https://paqu-tikhub-proxy.onrender.com/schedule-accounts" \
  -H "X-Schedule-Secret: 你的SCHEDULE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"accounts":["account1","account2"]}'
```

查看报告列表：

```text
https://paqu-tikhub-proxy.onrender.com/reports
```

下载最近一次汇总：

```text
https://paqu-tikhub-proxy.onrender.com/reports/latest_report.csv
```

Render 本地 `reports/` 仍是临时文件；账号、报表和历史快照的持久数据以 Supabase 为准。不要把 Render 本地磁盘当作唯一数据源。

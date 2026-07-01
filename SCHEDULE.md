# 后端定时自动抓取

Render 服务支持把账号名单放在后端账号池或环境变量里，然后由 GitHub Actions 每天定时触发抓取。

Render 免费 Web Service 没有人访问时会休眠，所以“前端页面里的每天 08:00”只有页面开着才准。稳定方案是仓库里的 GitHub Actions 从外部访问 `/run-scheduled`，它会唤醒 Render 并触发抓取。当前工作流配置在 `.github/workflows/scheduled-report.yml`，默认北京时间每天 08:05 运行。

## Render 环境变量

```text
TIKHUB_API_KEY=你的 TikHub API Key
SCHEDULE_SECRET=你自己设置的一串密码
SCHEDULE_ACCOUNTS=账号1,账号2,账号3
SCHEDULE_MAX_VIDEOS=100
SCHEDULE_USE_PLAYLISTS=1
SCHEDULE_MAX_PLAYLISTS=300
SCHEDULE_PAGE_SIZE=30
SCHEDULE_PLAYLIST_PAGE_SIZE=20
SCHEDULE_PLAYLIST_VIDEO_PAGE_SIZE=30
SCHEDULE_DELAY_MS=300
```

`SCHEDULE_ACCOUNTS` 也可以一行一个账号。现在前端报表页还提供“账号池”，输入 `SCHEDULE_SECRET` 后可以把账号保存到后端运行时文件 `reports/schedule_accounts.json`。抓取时优先使用后端账号池；账号池为空时才回退到 `SCHEDULE_ACCOUNTS`。

默认会优先走 TikHub 播放列表/合集接口统计短剧数量；如果某个账号拿不到可用合集，或者合集返回的集数/播放量明显为空，才会退回按公开视频标题自动归类。`SCHEDULE_MAX_VIDEOS` 只影响这个退回方案。

## GitHub Secret

GitHub 仓库也要添加一个 Secret：

```text
SCHEDULE_SECRET=和 Render 里完全一样的密码
```

默认定时任务在北京时间每天 08:00 运行，配置文件是 `.github/workflows/scheduled-report.yml`。

注意 GitHub Actions 的 cron 不保证秒级准点，通常会有几分钟队列延迟；当前配置故意用 08:05，避开整点高峰。

## 手动触发和查看

前端页面现在有“后端账号报表”区域。只需要在前端填 `SCHEDULE_SECRET` 的值，然后点“启动后端抓取”或“加载最新结果”。账号名单不会显示在前端，仍然只从 Render 的 `SCHEDULE_ACCOUNTS` 读取。
前端报表页的“账号池”可以读取/保存后端账号池，并支持“保存并立即抓取”。

手动触发：

```text
https://paqu-tikhub-proxy.onrender.com/run-scheduled?wait=1&secret=你的SCHEDULE_SECRET
```

查看状态：

```text
https://paqu-tikhub-proxy.onrender.com/schedule-status?secret=你的SCHEDULE_SECRET
```

查看账号池：

```text
https://paqu-tikhub-proxy.onrender.com/schedule-accounts?secret=你的SCHEDULE_SECRET
```

保存账号池：

```bash
curl -X POST "https://paqu-tikhub-proxy.onrender.com/schedule-accounts?secret=你的SCHEDULE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"accounts":["account1","account2"]}'
```

查看报告列表：

```text
https://paqu-tikhub-proxy.onrender.com/reports?secret=你的SCHEDULE_SECRET
```

下载最近一次汇总：

```text
https://paqu-tikhub-proxy.onrender.com/reports/latest_report.csv?secret=你的SCHEDULE_SECRET
```

注意：Render 免费服务的本地文件不是永久存储，服务重启或重新部署后 `reports/` 里的历史报告和 `reports/schedule_accounts.json` 可能会丢。账号池适合日常在线调整；长期稳定建议继续把 `SCHEDULE_ACCOUNTS` 放在 Render Environment 里作为兜底，或后续接 Google Sheets、数据库、对象存储。

# 后端定时自动抓取

Render 服务支持把账号名单放在后端环境变量里，然后由 GitHub Actions 每天定时触发抓取。

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

`SCHEDULE_ACCOUNTS` 也可以一行一个账号。

默认会优先走 TikHub 播放列表/合集接口统计短剧数量；如果某个账号拿不到合集，才会退回按公开视频标题自动归类。`SCHEDULE_MAX_VIDEOS` 只影响这个退回方案，不会再把合集数量截成 100。

## GitHub Secret

GitHub 仓库也要添加一个 Secret：

```text
SCHEDULE_SECRET=和 Render 里完全一样的密码
```

默认定时任务在北京时间每天 08:00 运行，配置文件是 `.github/workflows/scheduled-report.yml`。

## 手动触发和查看

前端页面现在有“后端账号报表”区域。只需要在前端填 `SCHEDULE_SECRET` 的值，然后点“启动后端抓取”或“加载最新结果”。账号名单不会显示在前端，仍然只从 Render 的 `SCHEDULE_ACCOUNTS` 读取。

手动触发：

```text
https://paqu-tikhub-proxy.onrender.com/run-scheduled?wait=1&secret=你的SCHEDULE_SECRET
```

查看状态：

```text
https://paqu-tikhub-proxy.onrender.com/schedule-status?secret=你的SCHEDULE_SECRET
```

查看报告列表：

```text
https://paqu-tikhub-proxy.onrender.com/reports?secret=你的SCHEDULE_SECRET
```

下载最近一次汇总：

```text
https://paqu-tikhub-proxy.onrender.com/reports/latest_report.csv?secret=你的SCHEDULE_SECRET
```

注意：Render 免费服务的本地文件不是永久存储，服务重启或重新部署后 `reports/` 里的历史报告可能会丢。临时使用可以，长期稳定保存建议接 Google Sheets、数据库或对象存储。

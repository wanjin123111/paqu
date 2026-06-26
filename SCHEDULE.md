# 后端定时自动抓取

Render 服务支持把账号名单放在后端环境变量里，然后由 GitHub Actions 每天定时触发抓取。

## Render 环境变量

```text
TIKHUB_API_KEY=你的 TikHub API Key
SCHEDULE_SECRET=你自己设置的一串密码
SCHEDULE_ACCOUNTS=账号1,账号2,账号3
SCHEDULE_MAX_VIDEOS=100
SCHEDULE_PAGE_SIZE=30
SCHEDULE_DELAY_MS=300
```

`SCHEDULE_ACCOUNTS` 也可以一行一个账号。

## GitHub Secret

GitHub 仓库也要添加一个 Secret：

```text
SCHEDULE_SECRET=和 Render 里完全一样的密码
```

默认定时任务在北京时间每天 08:00 运行，配置文件是 `.github/workflows/scheduled-report.yml`。

## 手动触发和查看

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

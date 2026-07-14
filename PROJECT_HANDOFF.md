# TikHub 短剧报表项目迁移开发文档

> 用途：换电脑、换 Codex 会话或交给其他开发者时，先阅读本文件。  
> 快照日期：2026-07-13  
> 当前线上代码基线：打包前已同步至 `origin/main`，精确提交以 `git log -1` 为准。  
> 本文件不包含任何 API Key、数据库密钥或任务密码。

## 1. 项目目标

批量监控 TikTok 短剧账号，通过 TikHub 抓取账号、短剧、集数、播放量、题材、简介和作品链接，生成可视化首页、账号汇总、账号明细及 CSV/JSON 报表；同时由运营人员认领公司自有作品、合并多账号发布来源并维护主创资料，形成独立的公开公司短剧资产库。

系统需要同时满足：

- 首页优先展示已经保存的报表，不等待实时抓取。
- 每天自动抓取并保存历史快照，用于 1 天、3 天、7 天和 30 天增长分析。
- 前端可公开查看；抓取、保存、代理、视频源和下载等敏感能力必须鉴权。
- 历史数据最多保留 30 天，避免 GitHub、Render 和 Supabase 持续膨胀。

## 2. 项目与线上地址

- 当前开发目录：`C:\Users\24541\Documents\Codex\2026-06-30\c-users-24541-documents-codex-2026\paqu-security-retention`
- GitHub 仓库：`https://github.com/wanjin123111/paqu`
- Render 前后端一体地址：`https://paqu-tikhub-proxy.onrender.com/`
- GitHub Pages 静态镜像：`https://wanjin123111.github.io/paqu/`
- 本地开发地址：`http://localhost:8787/`
- Python：`3.11.9`

Render 的 `/` 会托管 `index.html`；GitHub Pages 也直接发布同一份静态前端。线上 API 和定时抓取由 Render 上的 `tikhub_proxy.py` 提供。

## 3. 当前目录结构

```text
paqu/
├─ index.html                         # 正式前端入口，Render 与 GitHub Pages 使用
├─ tikhub-report-frontend.html        # 前端同步副本，修改界面时必须同步
├─ admin.html / admin.js              # 公司短剧资产管理后台
├─ catalog.html / catalog.js          # 公开公司短剧库、榜单、搜索和详情
├─ tikhub_proxy.py                    # Python 后端、TikHub 代理、抓取、报表、Supabase
├─ 启动代理.bat                       # Windows 本地一键启动
├─ render.yaml                        # Render Blueprint 与默认环境变量
├─ runtime.txt                        # Python 版本
├─ schedule_accounts.seed.json        # 本地账号池兜底示例，不是线上完整账号池
├─ public_reports/                    # 可公开读取的最新报表与最近 30 天历史报表
│  ├─ latest_report.json
│  ├─ latest_report.csv
│  ├─ latest_dramas.csv
│  ├─ episode_history/                # 按账号分片的剧集播放历史，避免 Render 内存溢出
│  ├─ manifest.json
│  └─ scheduled_*                     # 历史快照，自动清理 30 天以前文件
├─ reports/                           # Render/本地运行时目录，已忽略，不是永久存储
├─ tests/
│  └─ test_security_retention.py      # 安全、鉴权、缓存、保留期测试
├─ .github/workflows/
│  └─ scheduled-report.yml            # 每天 08:05（北京时间）触发抓取并提交报表
├─ docs/                              # 页面结构、组件和设计参考
├─ README.md                          # 早期使用说明
├─ SCHEDULE.md                        # 早期定时任务说明
└─ PROJECT_HANDOFF.md                 # 本迁移开发文档，当前说明以此为准
```

## 4. 已完成功能

### 前端

- 独立全屏数据看板为第一页，点击“下一页”进入报表，不依靠下滑切页。
- 账号汇总、账号明细两个视图；明细每页 7 条。
- 明细简介限制高度并支持展开，避免单条记录撑满页面。
- 英文剧名、中文译名、发布时间、集数、播放量、时长、题材、简介和作品链接展示。
- 账号汇总保留主页链接；账号明细使用对应短剧/作品链接。
- 短剧视频列表、集数、中文播放量、作品页和受保护播放源。
- 历史增长筛选放在账号明细，可按用户输入的天数和播放增量筛选。
- 首页显示当前热度最高的同一部剧，以及该剧近 7 天、3 天、1 天涨幅；不会用三个不同剧冒充对比。
- 近 30 天热度新增榜、热门题材占比、本月/本周题材播放增长、监控账号数、本周新增短剧和增长最多账号。
- 初始加载先用轻量元数据校验浏览器缓存；旧缓存不再冒充最新数据，而是显示同步状态后重载完整报表。公开页每 1 分钟检查一次元数据，窗口重新获得焦点时也会立即检查。
- 近 10 次历史报表最多同时加载 2 份，避免浏览器并发请求令 Render 512MB 实例出现内存和响应压力。
- `/admin` 为公司短剧资产管理后台：支持作品认领、忽略、跨账号来源合并、主创资料、别名、内部备注、上下架、删除和手工排序。
- `/catalog` 为公开公司短剧库：仅展示后台已上架作品，支持播放热度榜、剧名/主创/账号搜索、排序和来源详情。
- 任务密码只保存在当前浏览器 `sessionStorage`，管理端写操作统一使用 `X-Schedule-Secret` 请求头。

### 后端与数据

- TikHub API 代理、账号抓取、短剧归类、题材和标题翻译、报表 JSON/CSV 生成。
- Render 定时抓取和 GitHub Actions 每日调度。
- Supabase 持久化账号、短剧、报表运行记录和历史快照。
- Supabase 最新报表缓存、按 ID 缓存、紧凑历史报表接口 `?compact=1`。
- 公司短剧配置复用 Supabase `report_runs` 持久化，使用 `source=admin_catalog` 独立保存；普通报表查询与 30 天清理均排除此记录。
- 公司短剧配置带递增 `revision`，多人或多页面同时编辑时使用乐观锁，旧版本保存返回 `409`，避免覆盖新数据。
- GitHub `public_reports` 作为公开静态回退数据源。
- Render、本地历史文件、剧集历史和 Supabase 快照统一执行最多 30 天保留策略。

### 安全

- 公开 `?url=` 代理、`/save`、视频源、ZIP/下载脚本和本地下载接口均已封闭。
- 敏感请求使用 `X-Schedule-Secret`；不要把密码放在 URL 查询参数或前端代码里。
- Render 设置 `ALLOW_LOOPBACK_PRIVATE_ACCESS=0`，防止反向代理环境错误绕过鉴权。
- 代理目标只允许 `api.tikhub.io` 与 `api.tikhub.dev`。
- 报表读取保持公开；写入、任务触发、代理和下载保持私有。

## 5. 正在开发或待继续功能

公司短剧资产后台与公开剧库已完成开发和本地端到端验证。精确线上状态以 Render Events 最新成功部署提交为准。

下一轮建议按优先级继续：

1. 用更多连续 30 天快照复核 1/3/7/30 天增长口径，特别是新剧在基准日不存在时的显示。
2. 给首页和账号明细补充小屏幕、不同缩放比例的回归截图。
3. 后续新增敏感接口时继续只使用 `X-Schedule-Secret` 请求头，不得恢复查询参数密钥。
4. 后续可按公司内部权限体系增加多用户登录和角色划分；当前管理端使用统一 `SCHEDULE_SECRET`。

## 6. 关键技术与运行方式

- 前端：原生 HTML、CSS、JavaScript，无构建步骤、无 npm 依赖。
- 后端：Python 3.11 标准库 HTTP 服务，无第三方 Python 包。
- 外部数据：TikHub API。
- 数据库：Supabase PostgREST。
- 部署：GitHub、GitHub Pages、GitHub Actions、Render Web Service。
- 报表格式：JSON、CSV；浏览器端支持导出 Excel/Word。

数据流：

```text
GitHub Actions（每天 08:05）
    → Render /run-scheduled（X-Schedule-Secret）
    → TikHub 抓取账号和短剧
    → Render reports/ 生成报表
    → Supabase 保存结构化快照
    → Actions 下载结果并提交 public_reports/
    → 首页优先读取 Supabase，失败时回退 public_reports
```

## 7. 数据存放位置

### GitHub 仓库

- `public_reports/latest_*`：最新公开报表。
- `public_reports/scheduled_*`：最近 30 天历史快照。
- `public_reports/manifest.json`：历史报表索引。
- `public_reports/episode_history/*.json`：按账号分片的剧集播放历史；运行时更新写入 `reports/episode_history/`。

### Supabase

- `accounts`：账号基础信息。
- `account_snapshots`：每次报表的账号指标快照。
- `dramas`：短剧基础信息。
- `drama_snapshots`：每次报表的短剧播放量和集数快照。
- `report_runs`：报表批次及完整原始 JSON。
- `report_runs` 中 `source=admin_catalog` 的一条记录：公司短剧认领、主创资料、来源绑定、上下架和排序配置；不会被普通历史报表清理。
- 30 天以前的 `account_snapshots`、`drama_snapshots`、`report_runs` 会由定时抓取后的清理逻辑删除。

### Render

- `reports/` 是运行时临时文件，免费实例重启或重新部署后可能丢失。
- 不能把 Render 本地磁盘当作唯一数据库；持久数据以 Supabase 和 GitHub `public_reports` 为准。

### 浏览器

- 页面设置可能保存在浏览器 `localStorage`。
- 资产管理后台的任务密码仅保存在当前标签会话的 `sessionStorage`；关闭浏览器会话后需重新输入。
- 不要依赖浏览器保存线上密钥；正式密钥只放 Render Environment / GitHub Actions Secrets。

## 8. 环境变量与密钥

新电脑的压缩包不包含以下值，需要从原 Render/Supabase/GitHub 后台重新配置或由项目所有者提供：

### 必需

```text
TIKHUB_API_KEY
SCHEDULE_SECRET
SCHEDULE_ACCOUNTS
SUPABASE_URL
SUPABASE_SERVICE_KEY
```

### 主要可选配置

```text
REPORT_RETENTION_DAYS=30
DRAMA_EPISODE_HISTORY_MAX_AGE_DAYS=30
SUPABASE_REPORT_HISTORY_LIMIT=30
SUPABASE_LATEST_CACHE_SECONDS=120
ADMIN_CATALOG_CACHE_SECONDS=20
SCHEDULE_MAX_RUNTIME_SECONDS=600
SCHEDULE_DELAY_MS=300
SCHEDULE_ACCOUNT_WORKERS=4
TIKHUB_RPS_LIMIT=18
TIKTOK_RPS_LIMIT=8
SCHEDULE_USE_DRAMA_LIBRARY=1
SCHEDULE_USE_PLAYLISTS=1
SCHEDULE_TRANSLATE_TITLES=1
ALLOW_LOOPBACK_PRIVATE_ACCESS=0   # Render 必须为 0
```

`SCHEDULE_ACCOUNT_WORKERS=4` 会并行处理 4 个账号；`TIKHUB_RPS_LIMIT=18` 针对 TikHub 20 RPS 套餐预留 2 RPS 余量，避免后台抓取、重试和手动查询叠加后触发 429。若 TikHub 账户仍是默认 10 RPS，应把该值改为 `8`。500 个账号可进入同一任务且不会截断，但实际完成时间仍取决于账号短剧数量、TikHub/TikTok 响应速度和 Render 配置。

GitHub 仓库的 `Settings → Secrets and variables → Actions` 还要配置：

```text
SCHEDULE_SECRET=与 Render 中完全相同的值
```

不要把真实值写入 `index.html`、`tikhub_proxy.py`、文档、Git 提交或聊天记录。

## 9. 换电脑后的启动与发布

### 推荐：从 GitHub 克隆

```powershell
git clone https://github.com/wanjin123111/paqu.git
cd paqu
python --version
python tikhub_proxy.py
```

浏览器打开 `http://localhost:8787/`。Windows 也可以直接双击 `启动代理.bat`。

只查看已保存报表时不需要 TikHub Key；本地重新抓取或访问私有接口时，需要在当前终端设置环境变量后再启动 Python。

### 使用迁移压缩包

1. 解压到不含特殊权限限制的目录，例如 `D:\CodexProjects\paqu`。
2. 打开 `PROJECT_HANDOFF.md`。
3. 双击 `启动代理.bat`，访问 `http://localhost:8787/`。
4. 如需继续 Git 开发，推荐另行 `git clone` 正式仓库，再将未提交的新文件放入克隆目录；迁移包本身不包含 `.git`。

### 发布更新

```powershell
git status
git add <本次修改的文件>
git commit -m "说明本次修改"
git push origin main
```

- 推送 `main` 后，Render 会自动部署后端和 `/` 前端。
- GitHub Pages 会发布静态 `index.html`。
- `.github/workflows/scheduled-report.yml` 每天自动抓取并提交 `public_reports`。
- 发布后至少检查：Render `/health`、Render `/`、GitHub Pages、最新报表时间和浏览器控制台。

### 本地验证

```powershell
python -m py_compile tikhub_proxy.py
python -m unittest discover -s tests -v
```

## 10. 重要文件说明

- `index.html`：线上正式页面；大多数前端修改发生在这里。
- `tikhub-report-frontend.html`：前端副本。现阶段必须与 `index.html` 同步修改，不能只改一个。
- `tikhub_proxy.py`：核心后端，修改前先搜索现有路由、缓存、保留期和鉴权函数，避免重复实现。
- `render.yaml`：Render 默认环境变量与启动命令；真实密钥使用 `sync: false`，只在后台填写。
- `.github/workflows/scheduled-report.yml`：每日抓取、报表下载、30 天清理和 Git 提交。
- `public_reports/manifest.json`：前端静态历史报表入口。
- `tests/test_security_retention.py`：任何安全、历史读取、缓存或清理修改后都必须运行。

## 11. 已知问题

- Render 免费实例可能休眠，直接打开 Render 域名时首次唤醒可能等待几十秒到约两分钟；GitHub Pages 静态入口通常更快。
- TikHub API 余额、限流、字段变化或单个端点异常会影响抓取完整度。
- 视频直链可能过期，且浏览器编解码器、来源防盗链或 CORS 会导致“有声音但画面停住”等播放差异。
- Supabase 免费额度和平台政策不是项目代码能保证的永久服务，需要定期查看配额和项目状态。
- `SCHEDULE.md` 的手动调用示例已经统一为 `X-Schedule-Secret` 请求头；敏感接口不再接受查询参数密钥。
- `public_reports/episode_history/*.json` 增长较快，当前按账号分片，并按 30 天和点数上限清理；不要再合并成单个 JSON，否则 Render 512MB 实例解析时容易内存溢出。

## 12. 不能随意改动/需要注意的约束

1. 不得把 `TIKHUB_API_KEY`、`SCHEDULE_SECRET`、`SUPABASE_SERVICE_KEY` 放入前端或 Git。
2. 不得重新开放公共 `?url=` 代理、`/save`、视频源或下载接口。
3. Render 必须保持 `ALLOW_LOOPBACK_PRIVATE_ACCESS=0`。
4. 历史保留上限保持 30 天；延长前先评估 GitHub 仓库和 Supabase 容量。
5. 顶部 7/3/1 天卡片必须比较同一部当前最高播放短剧；匹配优先使用短剧 ID，再回退账号和标题。
6. 账号明细每页保持 7 条，简介可展开且不能把右侧链接顶出页面。
7. `index.html` 与 `tikhub-report-frontend.html` 必须同步，直到后续明确移除副本。
8. Render 文件系统是临时层，不能作为唯一持久数据源。
9. 不要直接覆盖用户未提交的改动；修改前先执行 `git status` 和 `git fetch origin main`。

## 13. 下一步要做什么

换电脑后的第一轮建议：

1. 克隆仓库并运行测试。
2. 确认 Render 的 5 个必需环境变量仍存在，GitHub Actions 的 `SCHEDULE_SECRET` 与 Render 一致。
3. 打开两个线上地址，核对最新报表时间与账号数量。
4. 继续开发前先截图桌面和移动端首页，建立新的视觉基线。
5. 每次修改完成后：语法检查 → 自动测试 → 本地浏览器验证 → 提交 → 推送 → 线上复查。

## 14. 新 Codex 会话可直接粘贴的上下文

```text
请先阅读项目根目录 PROJECT_HANDOFF.md，再阅读 tikhub_proxy.py、index.html、render.yaml 和 .github/workflows/scheduled-report.yml。

项目是原生静态前端 + Python 3.11 后端，GitHub 仓库为 https://github.com/wanjin123111/paqu，Render 为 https://paqu-tikhub-proxy.onrender.com/。数据主要在 Supabase 和 public_reports，历史最多保留 30 天。

开始修改前先执行 git status、git fetch origin main，并尊重现有未提交改动。前端变更必须同步 index.html 与 tikhub-report-frontend.html。不得公开代理、/save、视频源或下载接口，不得把任何密钥写入代码或 URL。修改后运行 python -m py_compile tikhub_proxy.py 和 python -m unittest discover -s tests -v，完成后提交、推送并验证 Render 与 GitHub Pages。
```

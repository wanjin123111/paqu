# TikHub 短剧账号报表

这是一个本地运行的 TikHub 短剧账号批量报表工具。

## 文件说明

- `index.html` / `tikhub-report-frontend.html`: 前端页面。
- `tikhub_proxy.py`: 本地 Python 代理，用来解决浏览器 CORS，并托管页面。
- `启动代理.bat`: Windows 一键启动脚本。

## 本地使用

1. 双击 `启动代理.bat`。
2. 浏览器打开 `http://localhost:8787/`。
3. 在页面右上角设置 TikHub API Key。
4. 输入账号列表后生成报表。

## 让别人访问

### 方式一：GitHub Pages

上传到 GitHub 后，可以开启 GitHub Pages，让别人访问：

```text
https://你的GitHub用户名.github.io/仓库名/
```

注意：GitHub Pages 只能托管静态页面，不能运行 `tikhub_proxy.py`。如果 TikHub 接口被浏览器 CORS 拦截，访问者仍需要在自己的电脑上运行本地代理，或者你需要把代理部署到一个后端平台。

## 免费部署代理到 Render

仓库已包含 `render.yaml`，可以用 Render 免费 Web Service 部署 Python 代理。

1. 打开 <https://dashboard.render.com/> 并用 GitHub 登录。
2. 点击 **New +**，选择 **Blueprint**。
3. 选择这个仓库：`wanjin123111/paqu`。
4. Render 会读取 `render.yaml`，创建服务 `paqu-tikhub-proxy`。
5. 计划选择 **Free**，然后点击部署。
6. 部署完成后，Render 会给一个地址，通常类似：

```text
https://paqu-tikhub-proxy.onrender.com
```

然后在网页的“设置 → CORS 代理”里填：

```text
https://paqu-tikhub-proxy.onrender.com/?url={url}
```

如果 Render 生成的地址不是上面这个，把域名换成 Render 实际给你的域名即可。

### 让访问者不用填写 TikHub API Key

不要把 TikHub API Key 写进 GitHub 代码或前端页面。请放到 Render 后台环境变量里：

```text
TIKHUB_API_KEY=你的 TikHub API Key
```

设置路径：

1. 打开 Render 服务 `paqu-tikhub-proxy`。
2. 左侧进入 **Environment**。
3. 添加环境变量 `TIKHUB_API_KEY`。
4. 保存后手动 redeploy 一次服务。

这样前端不带 `Authorization` 时，代理会在服务端自动补上你的 Key。访问者不需要知道 Key，但他们的抓取会消耗你的 TikHub 余额。

免费服务 15 分钟没访问会休眠，第一次唤醒可能需要约 1 分钟。这个代理只允许转发到 `api.tikhub.io` 和 `api.tikhub.dev`，避免被别人当成通用公开代理滥用。

### 方式二：让别人本地运行

把仓库地址发给别人，对方下载后双击 `启动代理.bat`，再打开：

```text
http://localhost:8787/
```

这是当前代码最稳的使用方式。

### 方式三：没有域名也能临时公网访问

如果你想让别人直接访问你电脑上的页面，可以临时用隧道工具，例如 Cloudflare Tunnel、ngrok、localtunnel。它们会给你一个临时公网地址，不需要你先买域名。

示例思路：

1. 本机启动 `启动代理.bat`。
2. 用隧道工具把本机 `8787` 端口暴露出去。
3. 把隧道给出的 `https://...` 地址发给别人。

这种方式适合临时演示；长期使用建议部署到正式后端平台。

## 安全提醒

- 不要把 TikHub API Key 写进代码或提交到 GitHub。
- 页面里的“记住 Key”会把 Key 保存到当前浏览器的 localStorage。
- 公开仓库前请确认代码里没有私人密钥、Cookie、账号密码。

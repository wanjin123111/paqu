# -*- coding: utf-8 -*-
"""
==============================================================================
 TikHub 本地代理 + 网页托管  (tikhub_proxy.py)
==============================================================================
 作用:一条命令解决网页直连 TikHub 被浏览器 CORS 拦截的问题。
       它做两件事:
         1) 把网页本身托管在 http://localhost:8787/(和代理同源,无 CORS、无预检)
         2) 转发网页发来的 API 请求到 TikHub,并带上你的 Authorization 头
       你的 API Key 只经过本机这个进程转发给 TikHub,不经任何第三方。

 用法(3 步):
   1. 把这个 tikhub_proxy.py 和 tikhub-report-frontend.html 放在同一个文件夹
   2. 在该文件夹运行:   python tikhub_proxy.py
   3. 浏览器打开:       http://localhost:8787/
      然后在网页“设置 → CORS 代理”里填:   /?url={url}
      (就这么填,前面不用加域名;同源所以最稳)

 停止:Ctrl + C
 端口被占用?把下面的 PORT 改个数字,网页代理框也跟着改。
==============================================================================
"""
import os, posixpath, mimetypes, base64, json, datetime
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8787
ROOT = os.getcwd()                       # 托管当前文件夹
DEFAULT_PAGE = "tikhub-report-frontend.html"
REPORTS_DIR = os.path.join(ROOT, "reports")   # 定时监控存盘目录
FORWARD_HEADERS = ("Authorization", "Content-Type", "Accept", "User-Agent", "Accept-Language")
ALLOW_HEADERS = "Authorization, Content-Type, Accept"
# 伪装成正常浏览器,绕过 Cloudflare 的 "browser_signature_banned"(Error 1010)
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


class Handler(BaseHTTPRequestHandler):
    server_version = "tikhub-proxy/1.0"

    # ---- CORS ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", ALLOW_HEADERS)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "url" in qs:
            self._proxy("GET", qs["url"][0])
        else:
            self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/save":
            self._save_file()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("url", [None])[0]
        if not target:
            self._send_bytes(400, b'{"error":"missing url param"}', "application/json")
            return
        self._proxy("POST", target)

    # ---- 定时监控:把报表写到 reports/ 目录 ----
    def _save_file(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(ln) or b"{}")
            name = os.path.basename(str(payload.get("filename", "")).strip())
            if not name:
                self._send_bytes(400, b'{"ok":false,"error":"no filename"}', "application/json"); return
            os.makedirs(REPORTS_DIR, exist_ok=True)
            full = os.path.normpath(os.path.join(REPORTS_DIR, name))
            if not full.startswith(REPORTS_DIR):
                self._send_bytes(400, b'{"ok":false,"error":"bad path"}', "application/json"); return
            append = bool(payload.get("append"))
            if payload.get("base64"):
                data = base64.b64decode(payload.get("content", ""))
                with open(full, "ab" if append else "wb") as f:
                    f.write(data)
            elif append:
                # 文本追加:仅在文件新建时写 BOM + 表头,之后只追加数据行(避免 BOM 插到中间)
                new_file = not os.path.exists(full) or os.path.getsize(full) == 0
                with open(full, "a", encoding="utf-8", newline="") as f:
                    if new_file:
                        f.write("\ufeff")
                        hdr = payload.get("header")
                        if hdr:
                            f.write(hdr if hdr.endswith("\n") else hdr + "\n")
                    f.write(payload.get("content", ""))
            else:
                # 文本覆盖:带 BOM,Excel 直接识别中文
                with open(full, "w", encoding="utf-8-sig", newline="") as f:
                    f.write(payload.get("content", ""))
            self._send_bytes(200, json.dumps({"ok": True, "path": full}).encode("utf-8"), "application/json")
        except Exception as e:
            self._send_bytes(500, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"), "application/json")

    # ---- 静态托管 ----
    def _serve_static(self, path):
        path = path.split("?", 1)[0]
        if path in ("", "/"):
            path = "/" + DEFAULT_PAGE if os.path.isfile(os.path.join(ROOT, DEFAULT_PAGE)) else "/index.html"
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            self._send_bytes(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self._send_bytes(200, data, ctype, no_cache=True)

    # ---- 转发到 TikHub ----
    def _proxy(self, method, target):
        fwd = {h: self.headers[h] for h in FORWARD_HEADERS if h in self.headers}
        if "User-Agent" not in fwd or "python" in fwd.get("User-Agent", "").lower():
            fwd["User-Agent"] = DEFAULT_UA   # 关键:避免 Cloudflare 1010 封锁脚本特征
        body = None
        if method == "POST":
            ln = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(ln) if ln else None
        try:
            req = urllib.request.Request(target, data=body, headers=fwd, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                data, code = r.read(), r.status
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data, code = e.read(), e.code
            ctype = e.headers.get("Content-Type", "application/json")
        except Exception as e:
            self._send_bytes(502, ('{"error":"proxy failed: %s"}' % e).encode("utf-8"), "application/json")
            return
        self._send_bytes(code, data, ctype)

    def _send_bytes(self, code, data, ctype, no_cache=False):
        self.send_response(code)
        self._cors()
        if no_cache:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass  # 安静运行


def _pause():
    try:
        input("\n按回车键关闭本窗口...")
    except Exception:
        pass


if __name__ == "__main__":
    import traceback
    print("=" * 56)
    print(" TikHub 本地代理已启动")
    print("=" * 56)
    print(" 1) 浏览器打开:  http://localhost:%d/" % PORT)
    print(" 2) 网页 设置 → CORS 代理 填:  /?url={url}")
    print(" 托管目录:%s" % ROOT)
    print(" 保持本窗口开着;停止按 Ctrl+C")
    print("=" * 56)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    except OSError as e:
        msg = str(e).lower()
        print("\n[X] 启动失败:%s" % e)
        if getattr(e, "errno", None) in (48, 98, 10048) or "use" in msg or "占用" in msg:
            print("   端口 %d 已被占用 —— 你可能已经开了一个代理窗口(别重复开)," % PORT)
            print("   或者换个端口:把本文件顶部的 PORT 改成别的数字(如 8899),")
            print("   浏览器打开地址也对应改成 http://localhost:8899/ 。")
        _pause()
    except Exception as e:
        print("\n[X] 出错了:%s" % e)
        traceback.print_exc()
        _pause()

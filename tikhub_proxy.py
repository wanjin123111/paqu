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
import os, posixpath, mimetypes, base64, json, datetime, csv, hmac, io, re, threading, time
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
if os.environ.get("RENDER") or os.environ.get("PORT"):
    HOST = "0.0.0.0"
ROOT = os.path.dirname(os.path.abspath(__file__))  # 托管脚本所在文件夹
DEFAULT_PAGE = "tikhub-report-frontend.html"
REPORTS_DIR = os.path.join(ROOT, "reports")   # 定时监控存盘目录
FORWARD_HEADERS = ("Authorization", "Content-Type", "Accept", "User-Agent", "Accept-Language")
ALLOW_HEADERS = "Authorization, Content-Type, Accept"
ALLOWED_PROXY_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("ALLOWED_PROXY_HOSTS", "api.tikhub.io,api.tikhub.dev").split(",")
    if h.strip()
}
SERVER_API_KEY = os.environ.get("TIKHUB_API_KEY", "").strip()
# 伪装成正常浏览器,绕过 Cloudflare 的 "browser_signature_banned"(Error 1010)
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

def _env_int(name, default, low=None, high=None):
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except Exception:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


TIKHUB_HOST = os.environ.get("TIKHUB_HOST", "https://api.tikhub.io").rstrip("/")
TIKTOK_HOST = os.environ.get("TIKTOK_HOST", "https://www.tiktok.com").rstrip("/")
TIKTOK_AID = os.environ.get("TIKTOK_AID", "1233").strip() or "1233"
TIKTOK_REGION = os.environ.get("TIKTOK_REGION", "US").strip() or "US"
TIKTOK_LANGUAGE = os.environ.get("TIKTOK_LANGUAGE", "en").strip() or "en"
SCHEDULE_SECRET = os.environ.get("SCHEDULE_SECRET", "").strip()
SCHEDULE_ACCOUNTS = os.environ.get("SCHEDULE_ACCOUNTS", "")
SCHEDULE_MAX_VIDEOS = _env_int("SCHEDULE_MAX_VIDEOS", 100, 0, 20000)
SCHEDULE_MAX_PAGES = _env_int("SCHEDULE_MAX_PAGES", 80, 1, 20000)
SCHEDULE_PAGE_SIZE = _env_int("SCHEDULE_PAGE_SIZE", 30, 1, 50)
SCHEDULE_USE_DRAMA_LIBRARY = _env_bool("SCHEDULE_USE_DRAMA_LIBRARY", True)
SCHEDULE_DRAMA_PAGE_SIZE = _env_int("SCHEDULE_DRAMA_PAGE_SIZE", 50, 1, 50)
SCHEDULE_MAX_DRAMAS = _env_int("SCHEDULE_MAX_DRAMAS", 0, 0, 20000)
SCHEDULE_USE_PLAYLISTS = _env_bool("SCHEDULE_USE_PLAYLISTS", True)
SCHEDULE_MAX_PLAYLISTS = _env_int("SCHEDULE_MAX_PLAYLISTS", 300, 0, 20000)
SCHEDULE_PLAYLIST_PAGE_SIZE = _env_int("SCHEDULE_PLAYLIST_PAGE_SIZE", 20, 1, 50)
SCHEDULE_PLAYLIST_VIDEO_PAGE_SIZE = _env_int("SCHEDULE_PLAYLIST_VIDEO_PAGE_SIZE", 30, 1, 50)
SCHEDULE_MAX_PLAYLIST_VIDEO_PAGES = _env_int("SCHEDULE_MAX_PLAYLIST_VIDEO_PAGES", 200, 1, 1000)
SCHEDULE_DELAY_MS = _env_int("SCHEDULE_DELAY_MS", 300, 0, 60000)
SCHEDULE_RETRIES = _env_int("SCHEDULE_RETRIES", 4, 1, 10)
SCHEDULE_MAX_RUNTIME_SECONDS = _env_int("SCHEDULE_MAX_RUNTIME_SECONDS", 600, 30, 7200)
PUBLIC_REPORTS = os.environ.get("PUBLIC_REPORTS", "1").strip().lower() not in ("0", "false", "no", "off")

DEFAULT_ENDPOINTS = {
    "profile": "/api/v1/tiktok/app/v3/handler_user_profile",
    "secuid": "/api/v1/tiktok/app/v3/get_user_id_and_sec_user_id_by_username",
    "posts": "/api/v1/tiktok/app/v3/fetch_user_post_videos",
    "playlists": "/api/v1/tiktok/web/fetch_user_play_list",
    "playlist_videos": "/api/v1/tiktok/web/fetch_user_mix",
}
POST_EP_CANDIDATES = [
    "/api/v1/tiktok/app/v3/fetch_user_post_videos",
    "/api/v1/tiktok/app/v3/fetch_user_post_videos_v2",
    "/api/v1/tiktok/app/v3/fetch_user_post_videos_v3",
    "/api/v1/tiktok/web/fetch_user_post",
]
PLAYLIST_VIDEO_EP_CANDIDATES = [
    "/api/v1/tiktok/web/fetch_user_mix",
    "/api/v1/tiktok/web/fetch_play_list_videos",
]
PLAY_KEYS = ("play_count", "playCount", "play_cnt")
DESC_KEYS = ("desc", "title", "content", "aweme_title", "text")
ID_KEYS = ("aweme_id", "awemeId", "id", "item_id", "itemId")
PLAYLIST_ID_KEYS = ("mixId", "mix_id", "playlist_id", "playlistId")
PLAYLIST_NAME_KEYS = ("mixName", "mix_name", "name", "playlist_name", "title")
PLAYLIST_COUNT_KEYS = ("videoCount", "video_count", "aweme_count", "item_count", "itemCount", "episode_count", "episodeCount")
PLAYLIST_VIEW_KEYS = ("play_count", "playCount", "view_count", "viewCount", "total_play_count", "totalPlayCount")
DRAMA_ID_KEYS = ("dramaID", "dramaId", "drama_id", "id")
DRAMA_NAME_KEYS = ("dramaName", "drama_name", "name", "title")
DRAMA_COUNT_KEYS = ("numVideos", "num_videos", "videoCount", "video_count", "episodeCount", "episode_count")
DRAMA_VIEW_KEYS = ("numWatched", "num_watched", "play_count", "playCount", "view_count", "viewCount")
FOLLOWER_KEYS = ("followerCount", "follower_count", "fans_count", "total_follower", "followers")
HEART_KEYS = ("heartCount", "heart_count", "total_favorited", "favoriting_count", "likes")
NICK_KEYS = ("nickname", "nick_name", "nick")
VCOUNT_KEYS = ("videoCount", "aweme_count", "video_count")
SECUID_KEYS = ("secUid", "sec_uid", "sec_user_id", "secUserId")
SUMMARY_COLUMNS = ["截图名称", "账号", "昵称", "粉丝", "点赞", "短剧数", "总集数", "累计观看",
                   "单剧均观看", "最高观看短剧", "最高观看", "主页链接"]
DRAMA_COLUMNS = ["账号", "昵称", "短剧名", "集数", "累计观看", "单集均观看", "主页链接"]

JOB_LOCK = threading.Lock()
LAST_JOB = {"running": False, "started_at": None, "finished_at": None, "result": None, "error": None}


class TikHubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _send_tikhub_get(path, params, label):
    if not SERVER_API_KEY:
        raise TikHubError("TIKHUB_API_KEY is not configured on Render")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = TIKHUB_HOST + path + ("?" + query if query else "")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + SERVER_API_KEY,
        "User-Agent": DEFAULT_UA,
    }
    last_error = None
    for attempt in range(1, SCHEDULE_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=90) as resp:
                text = resp.read().decode("utf-8", "replace")
                return json.loads(text) if text else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            last_error = TikHubError("%s failed with HTTP %s: %s" % (label, exc.code, body), exc.code)
            if exc.code in (429, 500, 502, 503, 504) and attempt < SCHEDULE_RETRIES:
                time.sleep(max(0.5, SCHEDULE_DELAY_MS / 1000.0) * attempt)
                continue
            raise last_error
        except Exception as exc:
            last_error = TikHubError("%s request failed: %s" % (label, exc))
            if attempt < SCHEDULE_RETRIES:
                time.sleep(max(0.5, SCHEDULE_DELAY_MS / 1000.0) * attempt)
                continue
            raise last_error
    raise last_error or TikHubError("%s request failed" % label)


def _send_tiktok_get(path, params, label, referer_uid=None):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = TIKTOK_HOST + path + ("?" + query if query else "")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": TIKTOK_HOST + ("/@" + referer_uid if referer_uid else "/"),
        "User-Agent": DEFAULT_UA,
    }
    last_error = None
    for attempt in range(1, SCHEDULE_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=90) as resp:
                text = resp.read().decode("utf-8", "replace")
                return json.loads(text) if text else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            last_error = TikHubError("%s failed with HTTP %s: %s" % (label, exc.code, body), exc.code)
            if exc.code in (429, 500, 502, 503, 504) and attempt < SCHEDULE_RETRIES:
                time.sleep(max(0.5, SCHEDULE_DELAY_MS / 1000.0) * attempt)
                continue
            raise last_error
        except Exception as exc:
            last_error = TikHubError("%s request failed: %s" % (label, exc))
            if attempt < SCHEDULE_RETRIES:
                time.sleep(max(0.5, SCHEDULE_DELAY_MS / 1000.0) * attempt)
                continue
            raise last_error
    raise last_error or TikHubError("%s request failed" % label)


def _to_int(value):
    if value is None:
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return 0


def _deep_find(obj, keys, depth=0):
    if depth > 9 or obj is None:
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _deep_find(item, keys, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and not isinstance(obj[key], (dict, list)):
                return obj[key]
        for item in obj.values():
            found = _deep_find(item, keys, depth + 1)
            if found is not None:
                return found
    return None


def _looks_like_video(item):
    if not isinstance(item, dict):
        return False
    has_id = any(key in item for key in ID_KEYS)
    has_play = _deep_find(item, PLAY_KEYS) is not None or "stats" in item or "statistics" in item
    return has_id and has_play


def _looks_like_playlist(item):
    if not isinstance(item, dict):
        return False
    return any(key in item for key in PLAYLIST_ID_KEYS) or _deep_find(item, PLAYLIST_ID_KEYS) is not None


def _find_video_list(obj, depth=0):
    best = []
    if depth > 9 or obj is None:
        return best
    if isinstance(obj, list):
        if obj and sum(1 for item in obj if _looks_like_video(item)) >= max(1, len(obj) // 2):
            return obj
        for item in obj:
            found = _find_video_list(item, depth + 1)
            if len(found) > len(best):
                best = found
    elif isinstance(obj, dict):
        for item in obj.values():
            found = _find_video_list(item, depth + 1)
            if len(found) > len(best):
                best = found
    return best


def _find_playlist_list(obj, depth=0):
    best = []
    if depth > 9 or obj is None:
        return best
    if isinstance(obj, list):
        if obj and any(_looks_like_playlist(item) for item in obj):
            best = obj
        for item in obj:
            found = _find_playlist_list(item, depth + 1)
            if len(found) > len(best):
                best = found
    elif isinstance(obj, dict):
        for item in obj.values():
            found = _find_playlist_list(item, depth + 1)
            if len(found) > len(best):
                best = found
    return best


def _get_play_count(video):
    for container_key in ("statistics", "stats"):
        container = video.get(container_key) if isinstance(video, dict) else None
        if isinstance(container, dict):
            for key in PLAY_KEYS:
                if key in container:
                    return _to_int(container[key])
    return _to_int(_deep_find(video, PLAY_KEYS))


def _get_desc(video):
    if isinstance(video, dict):
        for key in DESC_KEYS:
            if isinstance(video.get(key), str):
                return video[key]
    found = _deep_find(video, DESC_KEYS)
    return found if isinstance(found, str) else ""


def _get_video_id(video):
    if isinstance(video, dict):
        for key in ID_KEYS:
            if key in video and not isinstance(video[key], (dict, list)):
                return str(video[key])
    found = _deep_find(video, ID_KEYS)
    return "" if found is None else str(found)


def _get_playlist_id(playlist):
    if isinstance(playlist, dict):
        for key in PLAYLIST_ID_KEYS:
            if key in playlist and not isinstance(playlist[key], (dict, list)):
                return str(playlist[key])
    found = _deep_find(playlist, PLAYLIST_ID_KEYS)
    return "" if found is None else str(found)


def _get_playlist_name(playlist, fallback):
    if isinstance(playlist, dict):
        for key in PLAYLIST_NAME_KEYS:
            if isinstance(playlist.get(key), str) and playlist[key].strip():
                return playlist[key]
    found = _deep_find(playlist, PLAYLIST_NAME_KEYS)
    if isinstance(found, str) and found.strip():
        return found
    return fallback


def _read_pagination(data):
    candidates = []
    if isinstance(data, dict):
        candidates.append(data)
        for key in ("data", "aweme_list", "itemList"):
            if isinstance(data.get(key), dict):
                candidates.append(data[key])
    has_more, cursor = None, None
    for item in candidates:
        for key in ("has_more", "hasMore", "hasMorePosts", "has_more_posts"):
            if key in item:
                has_more = item[key]
        for key in ("max_cursor", "cursor", "maxCursor", "next_cursor"):
            if item.get(key) not in (None, ""):
                cursor = item[key]
    return has_more, cursor


def _parse_accounts(text):
    text = (text or "").strip()
    if not text:
        return []
    try:
        if text.startswith("["):
            raw_items = json.loads(text)
        else:
            raw_items = re.split(r"[\s,;，；]+", text)
    except Exception:
        raw_items = re.split(r"[\s,;，；]+", text)
    accounts, seen = [], set()
    for item in raw_items:
        uid = str(item).strip().lstrip("@")
        if uid and uid.lower() not in seen:
            seen.add(uid.lower())
            accounts.append(uid)
    return accounts


def _resolve_secuid(uid):
    try:
        data = _send_tikhub_get(DEFAULT_ENDPOINTS["secuid"], {"username": uid, "unique_id": uid}, "secUid endpoint")
        found = _deep_find(data, SECUID_KEYS)
        return "" if found is None else str(found)
    except TikHubError as exc:
        if exc.status in (401, 402, 403):
            raise
    return ""


def _get_profile(uid, secuid):
    profile = {"nickname": uid, "followers": 0, "hearts": 0, "videoCount": 0}
    try:
        data = _send_tikhub_get(DEFAULT_ENDPOINTS["profile"], {
            "sec_user_id": secuid,
            "secUid": secuid,
            "unique_id": uid,
        }, "profile endpoint")
    except TikHubError as exc:
        if exc.status in (401, 402, 403):
            raise
        return profile
    profile["nickname"] = str(_deep_find(data, NICK_KEYS) or uid)
    profile["followers"] = _to_int(_deep_find(data, FOLLOWER_KEYS))
    profile["hearts"] = _to_int(_deep_find(data, HEART_KEYS))
    profile["videoCount"] = _to_int(_deep_find(data, VCOUNT_KEYS))
    return profile


def _fetch_posts_page(ep_list, params):
    last_error = None
    for endpoint in ep_list:
        try:
            return _send_tikhub_get(endpoint, params, "posts endpoint"), endpoint
        except TikHubError as exc:
            if exc.status == 404:
                last_error = exc
                continue
            raise
    raise last_error or TikHubError("No TikHub posts endpoint worked")


def _fetch_playlist_videos_page(ep_list, params):
    last_error = None
    for endpoint in ep_list:
        try:
            return _send_tikhub_get(endpoint, params, "playlist videos endpoint"), endpoint
        except TikHubError as exc:
            if exc.status == 404:
                last_error = exc
                continue
            raise
    raise last_error or TikHubError("No TikHub playlist videos endpoint worked")


def _runtime_exceeded(started):
    return time.time() - started > SCHEDULE_MAX_RUNTIME_SECONDS


def _get_all_videos(secuid, uid):
    videos, seen = [], set()
    cursor, locked_endpoint, stall = "0", None, 0
    started = time.time()
    ep_list = [DEFAULT_ENDPOINTS["posts"]] + [ep for ep in POST_EP_CANDIDATES if ep != DEFAULT_ENDPOINTS["posts"]]
    for _page in range(1, SCHEDULE_MAX_PAGES + 1):
        if _runtime_exceeded(started):
            break
        params = {
            "secUid": secuid,
            "sec_user_id": secuid,
            "unique_id": uid,
            "count": str(SCHEDULE_PAGE_SIZE),
            "cursor": str(cursor),
            "max_cursor": str(cursor),
        }
        if locked_endpoint:
            data = _send_tikhub_get(locked_endpoint, params, "posts endpoint")
        else:
            data, locked_endpoint = _fetch_posts_page(ep_list, params)
        batch = _find_video_list(data)
        added = 0
        for video in batch:
            video_id = _get_video_id(video)
            if video_id and video_id not in seen:
                seen.add(video_id)
                videos.append({"id": video_id, "desc": _get_desc(video), "views": _get_play_count(video)})
                added += 1
        if SCHEDULE_MAX_VIDEOS and len(videos) >= SCHEDULE_MAX_VIDEOS:
            return videos[:SCHEDULE_MAX_VIDEOS]
        has_more, next_cursor = _read_pagination(data)
        more_false = has_more in (False, 0, "0")
        advanced = next_cursor not in (None, "", "0") and str(next_cursor) != str(cursor)
        if more_false:
            break
        if advanced:
            cursor = str(next_cursor)
            stall = 0 if added else stall + 1
            if stall >= 6:
                break
            time.sleep(SCHEDULE_DELAY_MS / 1000.0)
            continue
        stall += 1
        if stall >= 6:
            break
        time.sleep((SCHEDULE_DELAY_MS / 1000.0) * (1 + stall))
    return videos[:SCHEDULE_MAX_VIDEOS] if SCHEDULE_MAX_VIDEOS else videos


def _get_user_playlists(secuid, uid, started):
    playlists, seen = [], set()
    cursor, prev, stall = "0", None, 0
    for _page in range(1, SCHEDULE_MAX_PAGES + 1):
        if _runtime_exceeded(started):
            break
        data = _send_tikhub_get(DEFAULT_ENDPOINTS["playlists"], {
            "secUid": secuid,
            "sec_user_id": secuid,
            "unique_id": uid,
            "count": str(SCHEDULE_PLAYLIST_PAGE_SIZE),
            "cursor": str(cursor),
            "max_cursor": str(cursor),
        }, "playlists endpoint")
        batch = _find_playlist_list(data)
        added = 0
        for item in batch:
            playlist_id = _get_playlist_id(item)
            if not playlist_id or playlist_id in seen:
                continue
            seen.add(playlist_id)
            playlists.append({
                "id": playlist_id,
                "name": _get_playlist_name(item, playlist_id),
                "episodes_hint": _to_int(_deep_find(item, PLAYLIST_COUNT_KEYS)),
                "views_hint": _to_int(_deep_find(item, PLAYLIST_VIEW_KEYS)),
            })
            added += 1
            if SCHEDULE_MAX_PLAYLISTS and len(playlists) >= SCHEDULE_MAX_PLAYLISTS:
                return playlists[:SCHEDULE_MAX_PLAYLISTS]
        has_more, next_cursor = _read_pagination(data)
        more_false = has_more in (False, 0, "0")
        advanced = next_cursor not in (None, "", "0") and str(next_cursor) != str(cursor) and str(next_cursor) != str(prev)
        if more_false:
            break
        if advanced:
            prev, cursor = cursor, str(next_cursor)
            stall = 0 if added else stall + 1
            if stall >= 6:
                break
            time.sleep(SCHEDULE_DELAY_MS / 1000.0)
            continue
        stall += 1
        if stall >= 6:
            break
        time.sleep((SCHEDULE_DELAY_MS / 1000.0) * (1 + stall))
    return playlists


def _get_playlist_video_stats(playlist_id, started):
    seen, total_views, episodes = set(), 0, 0
    cursor, prev, stall, locked_endpoint = "0", None, 0, None
    ep_list = [DEFAULT_ENDPOINTS["playlist_videos"]] + [ep for ep in PLAYLIST_VIDEO_EP_CANDIDATES if ep != DEFAULT_ENDPOINTS["playlist_videos"]]
    for _page in range(1, SCHEDULE_MAX_PLAYLIST_VIDEO_PAGES + 1):
        if _runtime_exceeded(started):
            break
        params = {
            "mixId": playlist_id,
            "mix_id": playlist_id,
            "playlistId": playlist_id,
            "count": str(SCHEDULE_PLAYLIST_VIDEO_PAGE_SIZE),
            "cursor": str(cursor),
            "max_cursor": str(cursor),
        }
        if locked_endpoint:
            data = _send_tikhub_get(locked_endpoint, params, "playlist videos endpoint")
        else:
            data, locked_endpoint = _fetch_playlist_videos_page(ep_list, params)
        batch = _find_video_list(data)
        added = 0
        for video in batch:
            video_id = _get_video_id(video)
            if video_id and video_id in seen:
                continue
            if video_id:
                seen.add(video_id)
            episodes += 1
            total_views += _get_play_count(video)
            added += 1
        has_more, next_cursor = _read_pagination(data)
        more_false = has_more in (False, 0, "0")
        advanced = next_cursor not in (None, "", "0") and str(next_cursor) != str(cursor) and str(next_cursor) != str(prev)
        if not batch or more_false:
            break
        if advanced:
            prev, cursor = cursor, str(next_cursor)
            stall = 0 if added else stall + 1
            if stall >= 6:
                break
            time.sleep(SCHEDULE_DELAY_MS / 1000.0)
            continue
        stall += 1
        if stall >= 6:
            break
        time.sleep((SCHEDULE_DELAY_MS / 1000.0) * (1 + stall))
    return {"episodes": episodes, "views": total_views}


def _playlist_dramas_are_usable(dramas):
    return bool(dramas) and any(_to_int(item.get("episodes")) or _to_int(item.get("views")) for item in dramas)


def _get_playlist_dramas(secuid, uid):
    started = time.time()
    playlists = _get_user_playlists(secuid, uid, started)
    dramas = []
    for playlist in playlists:
        episodes = playlist["episodes_hint"]
        views = playlist["views_hint"]
        if not _runtime_exceeded(started):
            try:
                stats = _get_playlist_video_stats(playlist["id"], started)
                if stats["episodes"]:
                    episodes = stats["episodes"]
                    views = stats["views"]
            except TikHubError as exc:
                if exc.status in (401, 402, 403):
                    raise
        name = (_clean_title(playlist["name"])[:60].strip() or playlist["name"] or playlist["id"])
        dramas.append({"name": name, "episodes": episodes, "views": views})
    return dramas


def _get_tiktok_drama_library(secuid, uid):
    if not secuid:
        return []
    started = time.time()
    dramas, seen = [], set()
    cursor, prev, stall = "0", None, 0
    for _page in range(1, SCHEDULE_MAX_PAGES + 1):
        if _runtime_exceeded(started):
            break
        data = _send_tiktok_get("/api/drama/user/drama_list/", {
            "secUid": secuid,
            "aid": TIKTOK_AID,
            "language": TIKTOK_LANGUAGE,
            "region": TIKTOK_REGION,
            "count": str(SCHEDULE_DRAMA_PAGE_SIZE),
            "cursor": str(cursor),
        }, "TikTok drama library endpoint", uid)
        if not isinstance(data, dict):
            break
        status = data.get("statusCode", data.get("status_code"))
        batch = data.get("dramaList") or data.get("drama_list") or []
        if status not in (None, 0, "0") and not batch:
            raise TikHubError("TikTok drama library returned status %s" % status)
        if not isinstance(batch, list):
            batch = []
        added = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            drama_id = _deep_find(item, DRAMA_ID_KEYS)
            drama_key = "" if drama_id is None else str(drama_id)
            name = str(_deep_find(item, DRAMA_NAME_KEYS) or "").strip()
            if not name:
                name = str(_deep_find(item, ("description",)) or drama_key).strip()
            if not name:
                continue
            key = drama_key or name.lower()
            if key in seen:
                continue
            seen.add(key)
            dramas.append({
                "name": (_clean_title(name)[:80].strip() or name[:80] or key),
                "episodes": _to_int(_deep_find(item, DRAMA_COUNT_KEYS)),
                "views": _to_int(_deep_find(item, DRAMA_VIEW_KEYS)),
            })
            added += 1
            if SCHEDULE_MAX_DRAMAS and len(dramas) >= SCHEDULE_MAX_DRAMAS:
                return dramas[:SCHEDULE_MAX_DRAMAS]
        has_more = data.get("hasMore", data.get("has_more"))
        next_cursor = data.get("cursor") or data.get("nextCursor") or data.get("next_cursor")
        more_false = has_more in (False, 0, "0")
        advanced = next_cursor not in (None, "", "0") and str(next_cursor) != str(cursor) and str(next_cursor) != str(prev)
        if more_false:
            break
        if advanced:
            prev, cursor = cursor, str(next_cursor)
            stall = 0 if added else stall + 1
            if stall >= 6:
                break
            time.sleep(SCHEDULE_DELAY_MS / 1000.0)
            continue
        stall += 1
        if stall >= 6:
            break
        time.sleep((SCHEDULE_DELAY_MS / 1000.0) * (1 + stall))
    return dramas


def _clean_title(text):
    text = text or ""
    patterns = [
        r"https?://\S+",
        r"[@#]\S+",
        r"\b(?:ep|episode|part|pt|chapter|ch|e|season)\s*\.?\s*\d+\b",
        r"第\s*\d+\s*[集話话部]",
        r"\d+\s*[集話话]",
        r"\bfull\s+(?:episode|movie|series|drama|story)\b",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.I)
    for _ in range(3):
        new_text = re.sub(r"[\(\[\{|\-\s]*\d+\s*[\)\]\}]*\s*$", "", text).strip()
        if new_text == text:
            break
        text = new_text
    return re.sub(r"\s+", " ", text).strip(" -|,.~!\t\r\n")


def _title_key(text):
    cleaned = _clean_title(text).lower()
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in cleaned)
    return " ".join(cleaned.split()[:8])


def _group_by_title(videos):
    groups = {}
    for video in videos:
        key = _title_key(video.get("desc", "")) or "__" + video.get("id", "")
        groups.setdefault(key, []).append(video)
    dramas = []
    for key, episodes in groups.items():
        total_views = sum(ep["views"] for ep in episodes)
        top = max(episodes, key=lambda item: item["views"])
        name = (_clean_title(top.get("desc", ""))[:60].strip() or top.get("desc", "")[:40] or key)
        dramas.append({"name": name, "episodes": len(episodes), "views": total_views})
    return dramas


def _build_summary_row(uid, profile, videos, dramas):
    total_episodes = sum(_to_int(drama.get("episodes")) for drama in dramas) if dramas else len(videos)
    total_views = sum(_to_int(drama.get("views")) for drama in dramas) if dramas else sum(video["views"] for video in videos)
    drama_count = len(dramas)
    avg_views = round(total_views / drama_count) if drama_count else 0
    top_name, top_views = "", 0
    if dramas:
        top = max(dramas, key=lambda item: item["views"])
        top_name, top_views = top["name"], top["views"]
    return {
        "截图名称": profile["nickname"],
        "账号": uid,
        "昵称": profile["nickname"],
        "粉丝": profile["followers"],
        "点赞": profile["hearts"],
        "短剧数": drama_count,
        "总集数": total_episodes,
        "累计观看": total_views,
        "单剧均观看": avg_views,
        "最高观看短剧": top_name,
        "最高观看": top_views,
        "主页链接": "https://www.tiktok.com/@" + uid,
    }


def _scrape_account(uid):
    secuid = _resolve_secuid(uid)
    profile = _get_profile(uid, secuid)
    videos, dramas = [], []
    if SCHEDULE_USE_DRAMA_LIBRARY:
        try:
            drama_library = _get_tiktok_drama_library(secuid, uid)
            if _playlist_dramas_are_usable(drama_library):
                dramas = drama_library
        except TikHubError:
            dramas = []
    if not dramas and SCHEDULE_USE_PLAYLISTS:
        try:
            playlist_dramas = _get_playlist_dramas(secuid, uid)
            if _playlist_dramas_are_usable(playlist_dramas):
                dramas = playlist_dramas
        except TikHubError as exc:
            if exc.status in (401, 402, 403):
                raise
    if not dramas:
        videos = _get_all_videos(secuid, uid)
        dramas = _group_by_title(videos)
    summary = _build_summary_row(uid, profile, videos, dramas)
    drama_rows = []
    for drama in dramas:
        drama_rows.append({
            "账号": uid,
            "昵称": profile["nickname"],
            "短剧名": drama["name"],
            "集数": drama["episodes"],
            "累计观看": drama["views"],
            "单集均观看": round(drama["views"] / drama["episodes"]) if drama["episodes"] else 0,
            "主页链接": "https://www.tiktok.com/@" + uid,
        })
    return summary, drama_rows


def _csv_blob(columns, rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return "\ufeff" + out.getvalue()


def _write_text_file(name, content):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.normpath(os.path.join(REPORTS_DIR, name))
    if not path.startswith(REPORTS_DIR):
        raise RuntimeError("bad report path")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


def _write_report_bundle(rows, drama_rows, errors):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    summary_name = "scheduled_report_%s.csv" % stamp
    drama_name = "scheduled_dramas_%s.csv" % stamp
    json_name = "scheduled_report_%s.json" % stamp
    summary_csv = _csv_blob(SUMMARY_COLUMNS, rows)
    drama_csv = _csv_blob(DRAMA_COLUMNS, drama_rows)
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "accounts": len(rows),
        "dramas": len(drama_rows),
        "errors": errors,
        "summary": rows,
        "dramas_detail": drama_rows,
    }
    _write_text_file(summary_name, summary_csv)
    _write_text_file(drama_name, drama_csv)
    _write_text_file(json_name, json.dumps(payload, ensure_ascii=False, indent=2))
    _write_text_file("latest_report.csv", summary_csv)
    _write_text_file("latest_dramas.csv", drama_csv)
    _write_text_file("latest_report.json", json.dumps(payload, ensure_ascii=False, indent=2))
    return {
        "summary": summary_name,
        "dramas": drama_name,
        "json": json_name,
        "latest_summary": "latest_report.csv",
        "latest_dramas": "latest_dramas.csv",
        "latest_json": "latest_report.json",
    }


def _run_scheduled_job(accounts):
    rows, drama_rows, errors = [], [], []
    for uid in accounts:
        try:
            summary, dramas = _scrape_account(uid)
            rows.append(summary)
            drama_rows.extend(dramas)
        except Exception as exc:
            errors.append({"account": uid, "error": str(exc)})
    files = _write_report_bundle(rows, drama_rows, errors)
    return {
        "ok": True,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "accounts_requested": len(accounts),
        "accounts_ok": len(rows),
        "accounts_failed": len(errors),
        "dramas": len(drama_rows),
        "files": files,
        "errors": errors,
    }


def _execute_scheduled_job(accounts):
    LAST_JOB.update({"running": True, "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                     "finished_at": None, "result": None, "error": None})
    try:
        result = _run_scheduled_job(accounts)
        LAST_JOB.update({"running": False, "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
                         "result": result, "error": None})
        return result
    except Exception as exc:
        LAST_JOB.update({"running": False, "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
                         "result": None, "error": str(exc)})
        raise


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
        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "service": "tikhub-proxy"})
        elif parsed.path == "/run-scheduled":
            self._run_scheduled_endpoint(qs)
        elif parsed.path == "/schedule-status":
            if self._require_schedule_secret(qs):
                self._send_json(200, {"ok": True, "job": LAST_JOB})
        elif parsed.path == "/reports":
            self._list_reports(qs)
        elif parsed.path.startswith("/reports/"):
            self._serve_report(parsed.path, qs)
        elif "url" in qs:
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

    def _require_schedule_secret(self, qs):
        if not SCHEDULE_SECRET:
            self._send_json(503, {"ok": False, "error": "SCHEDULE_SECRET is not configured"})
            return False
        supplied = qs.get("secret", [""])[0] or self.headers.get("X-Schedule-Secret", "")
        if not hmac.compare_digest(str(supplied), SCHEDULE_SECRET):
            self._send_json(403, {"ok": False, "error": "bad or missing schedule secret"})
            return False
        return True

    def _allow_report_read(self, qs):
        if PUBLIC_REPORTS:
            return True
        return self._require_schedule_secret(qs)

    def _run_scheduled_endpoint(self, qs):
        if not self._require_schedule_secret(qs):
            return
        if not SERVER_API_KEY:
            self._send_json(503, {"ok": False, "error": "TIKHUB_API_KEY is not configured"})
            return
        accounts = _parse_accounts(qs.get("accounts", [""])[0]) or _parse_accounts(SCHEDULE_ACCOUNTS)
        if not accounts:
            self._send_json(400, {"ok": False, "error": "SCHEDULE_ACCOUNTS is empty"})
            return
        if not JOB_LOCK.acquire(blocking=False):
            self._send_json(409, {"ok": False, "error": "scheduled job already running", "job": LAST_JOB})
            return

        wait = str(qs.get("wait", ["0"])[0]).lower() in ("1", "true", "yes")
        if wait:
            try:
                result = _execute_scheduled_job(accounts)
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc), "job": LAST_JOB})
            finally:
                JOB_LOCK.release()
            return

        def background():
            try:
                _execute_scheduled_job(accounts)
            finally:
                JOB_LOCK.release()

        threading.Thread(target=background, daemon=True).start()
        self._send_json(202, {"ok": True, "started": True, "accounts": len(accounts), "job": LAST_JOB})

    def _list_reports(self, qs):
        if not self._allow_report_read(qs):
            return
        if not os.path.isdir(REPORTS_DIR):
            self._send_json(200, {"ok": True, "reports": []})
            return
        reports = []
        for name in sorted(os.listdir(REPORTS_DIR), reverse=True):
            full = os.path.normpath(os.path.join(REPORTS_DIR, name))
            if not full.startswith(REPORTS_DIR) or not os.path.isfile(full):
                continue
            reports.append({
                "name": name,
                "size": os.path.getsize(full),
                "modified": datetime.datetime.fromtimestamp(os.path.getmtime(full)).isoformat(timespec="seconds"),
                "path": "/reports/" + urllib.parse.quote(name),
            })
        self._send_json(200, {"ok": True, "reports": reports})

    def _serve_report(self, path, qs):
        if not self._allow_report_read(qs):
            return
        name = os.path.basename(urllib.parse.unquote(path[len("/reports/"):]))
        full = os.path.normpath(os.path.join(REPORTS_DIR, name))
        if not full.startswith(REPORTS_DIR) or not os.path.isfile(full):
            self._send_json(404, {"ok": False, "error": "report not found"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as handle:
            data = handle.read()
        self.send_response(200)
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", "attachment; filename=%s" % name)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        parsed = urllib.parse.urlparse(target)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in ALLOWED_PROXY_HOSTS:
            self._send_bytes(403, b'{"error":"proxy target not allowed"}', "application/json")
            return
        fwd = {h: self.headers[h] for h in FORWARD_HEADERS if h in self.headers}
        auth = fwd.get("Authorization", "").strip()
        if SERVER_API_KEY and (not auth or auth.lower() == "bearer"):
            fwd["Authorization"] = "Bearer " + SERVER_API_KEY
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

    def _send_json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(code, data, "application/json; charset=utf-8", no_cache=True)

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
    print(" 监听地址:%s:%d" % (HOST, PORT))
    print(" 保持本窗口开着;停止按 Ctrl+C")
    print("=" * 56)
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
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

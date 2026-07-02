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
import os, posixpath, mimetypes, base64, json, datetime, csv, hmac, html, io, re, threading, time
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
if os.environ.get("RENDER") or os.environ.get("PORT"):
    HOST = "0.0.0.0"
ROOT = os.path.dirname(os.path.abspath(__file__))  # 托管脚本所在文件夹
DEFAULT_PAGE = "tikhub-report-frontend.html"
REPORTS_DIR = os.path.join(ROOT, "reports")   # 定时监控存盘目录
PUBLIC_REPORTS_DIR = os.path.join(ROOT, "public_reports")
DRAMA_DETAIL_CACHE_FILE = os.path.join(REPORTS_DIR, "drama_detail_cache.json")
DRAMA_EPISODE_HISTORY_FILE = os.path.join(REPORTS_DIR, "drama_episode_history.json")
SCHEDULE_ACCOUNTS_FILE = os.path.join(REPORTS_DIR, "schedule_accounts.json")
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
FORWARD_HEADERS = ("Authorization", "Content-Type", "Accept", "User-Agent", "Accept-Language")
ALLOW_HEADERS = "Authorization, Content-Type, Accept, X-Schedule-Secret"
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
SCHEDULE_FETCH_EPISODE_PUBLISH_TIME = _env_bool("SCHEDULE_FETCH_EPISODE_PUBLISH_TIME", True)
SCHEDULE_PUBLISH_TIME_EPISODE_SAMPLE = _env_int("SCHEDULE_PUBLISH_TIME_EPISODE_SAMPLE", 3, 1, 20)
SCHEDULE_USE_PLAYLISTS = _env_bool("SCHEDULE_USE_PLAYLISTS", True)
SCHEDULE_MAX_PLAYLISTS = _env_int("SCHEDULE_MAX_PLAYLISTS", 300, 0, 20000)
SCHEDULE_PLAYLIST_PAGE_SIZE = _env_int("SCHEDULE_PLAYLIST_PAGE_SIZE", 20, 1, 50)
SCHEDULE_PLAYLIST_VIDEO_PAGE_SIZE = _env_int("SCHEDULE_PLAYLIST_VIDEO_PAGE_SIZE", 30, 1, 50)
SCHEDULE_MAX_PLAYLIST_VIDEO_PAGES = _env_int("SCHEDULE_MAX_PLAYLIST_VIDEO_PAGES", 200, 1, 1000)
SCHEDULE_TRANSLATE_TITLES = _env_bool("SCHEDULE_TRANSLATE_TITLES", True)
SCHEDULE_DELAY_MS = _env_int("SCHEDULE_DELAY_MS", 300, 0, 60000)
SCHEDULE_RETRIES = _env_int("SCHEDULE_RETRIES", 4, 1, 10)
SCHEDULE_MAX_RUNTIME_SECONDS = _env_int("SCHEDULE_MAX_RUNTIME_SECONDS", 600, 30, 7200)
AUTO_REFRESH_COOLDOWN_SECONDS = _env_int("AUTO_REFRESH_COOLDOWN_SECONDS", 300, 30, 86400)
VIDEO_PLAY_URL_CACHE_TTL_SECONDS = _env_int("VIDEO_PLAY_URL_CACHE_TTL_SECONDS", 600, 0, 86400)
DRAMA_LINK_PAGE_SIZE = _env_int("DRAMA_LINK_PAGE_SIZE", 50, 1, 50)
DRAMA_LINK_MAX_EPISODES = _env_int("DRAMA_LINK_MAX_EPISODES", 500, 1, 5000)
SCHEDULE_SAVE_EPISODE_HISTORY = _env_bool("SCHEDULE_SAVE_EPISODE_HISTORY", True)
SCHEDULE_EPISODE_HISTORY_MAX_DRAMAS = _env_int("SCHEDULE_EPISODE_HISTORY_MAX_DRAMAS", 0, 0, 20000)
SCHEDULE_EPISODE_HISTORY_MAX_EPISODES = _env_int("SCHEDULE_EPISODE_HISTORY_MAX_EPISODES", DRAMA_LINK_MAX_EPISODES, 0, 5000)
SCHEDULE_EPISODE_HISTORY_DELAY_MS = _env_int("SCHEDULE_EPISODE_HISTORY_DELAY_MS", SCHEDULE_DELAY_MS, 0, 60000)
DRAMA_EPISODE_HISTORY_MAX_POINTS = _env_int("DRAMA_EPISODE_HISTORY_MAX_POINTS", 160, 20, 1000)
DRAMA_EPISODE_HISTORY_MAX_AGE_DAYS = _env_int("DRAMA_EPISODE_HISTORY_MAX_AGE_DAYS", 75, 35, 365)
DRAMA_EPISODE_HISTORY_DEDUP_SECONDS = _env_int("DRAMA_EPISODE_HISTORY_DEDUP_SECONDS", 1800, 60, 86400)
PUBLIC_REPORTS = os.environ.get("PUBLIC_REPORTS", "1").strip().lower() not in ("0", "false", "no", "off")
TRANSLATE_HOST = os.environ.get("TRANSLATE_HOST", "https://translate.googleapis.com").rstrip("/")

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
SINGLE_VIDEO_EP_CANDIDATES = [
    "/api/v1/tiktok/app/v3/fetch_one_video",
    "/api/v1/tiktok/app/v3/fetch_one_video_v2",
    "/api/v1/tiktok/app/v3/fetch_one_video_v3",
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
DRAMA_LINK_KEYS = ("shareUrl", "share_url", "shareLink", "share_link", "dramaUrl", "drama_url", "webUrl", "web_url")
VIDEO_LINK_KEYS = ("shareUrl", "share_url", "shareLink", "share_link", "videoUrl", "video_url", "webUrl", "web_url")
DRAMA_EPISODE_NUMBER_KEYS = (
    "EpisodeNumber", "episodeNumber", "episode_number", "EpisodeNo", "episodeNo", "episode_no",
    "EpisodeIndex", "episodeIndex", "episode_index", "Episode", "episode",
)
DRAMA_EN_TITLE_KEYS = ("englishTitle", "english_title", "enTitle", "titleEn", "title_en", "dramaName", "drama_name", "name", "title")
DRAMA_CN_TITLE_KEYS = ("chineseTitle", "chinese_title", "cnTitle", "titleCn", "title_cn", "zhTitle", "title_zh")
DRAMA_DURATION_SECONDS_KEYS = ("durationSeconds", "duration_seconds", "durationSec", "duration_sec", "duration", "totalDuration", "total_duration")
DRAMA_DURATION_MINUTES_KEYS = ("durationMinutes", "duration_minutes", "durationMin", "duration_min")
DRAMA_LIMITED_KEYS = ("limitedFree", "limited_free", "isLimitedFree", "is_limited_free", "isFree", "is_free", "free")
DRAMA_EN_THEMES_KEYS = ("englishThemes", "english_themes", "enThemes", "themesEn", "theme_en", "themes", "theme", "tags")
DRAMA_CN_THEMES_KEYS = ("chineseThemes", "chinese_themes", "cnThemes", "themesCn", "theme_cn", "zhThemes", "theme_zh")
DRAMA_EN_DESC_KEYS = ("englishDescription", "english_description", "enDescription", "descriptionEn", "descEn", "description", "desc")
DRAMA_CN_DESC_KEYS = ("chineseDescription", "chinese_description", "cnDescription", "descriptionCn", "descCn")
DRAMA_PUBLISH_TIME_KEYS = (
    "publishTime", "publish_time", "publishedAt", "published_at", "publishedTime", "published_time",
    "releaseTime", "release_time", "releaseDate", "release_date", "onlineTime", "online_time",
    "firstPublishTime", "first_publish_time", "firstReleaseTime", "first_release_time",
    "createTime", "create_time", "createTimeMs", "create_time_ms", "createdAt", "created_at",
)
FOLLOWER_KEYS = ("followerCount", "follower_count", "fans_count", "total_follower", "followers")
HEART_KEYS = ("heartCount", "heart_count", "total_favorited", "favoriting_count", "likes")
NICK_KEYS = ("nickname", "nick_name", "nick")
AVATAR_KEYS = (
    "avatarLarger", "avatar_larger", "avatarMedium", "avatar_medium", "avatarThumb", "avatar_thumb",
    "avatarUrl", "avatar_url", "avatar", "cover", "profile_pic_url", "profilePicUrl",
)
VCOUNT_KEYS = ("videoCount", "aweme_count", "video_count")
SECUID_KEYS = ("secUid", "sec_uid", "sec_user_id", "secUserId")
SUMMARY_COLUMNS = ["截图名称", "账号", "昵称", "粉丝", "点赞", "短剧数", "总集数", "累计观看",
                   "单剧均观看", "最高观看短剧", "最高观看短剧中文名", "最高观看", "主页链接"]
DRAMA_COLUMNS = ["Account / 账号", "Nickname / 昵称", "Screenshot Name / 截图名称", "Rank in Account / 账号内排序",
                 "Drama ID / 短剧ID", "English Title / 英文剧名", "Chinese Title / 中文剧名",
                 "Publish Time / 发布时间", "Episodes / 集数", "Views / 观看数", "Duration Seconds / 总时长(秒)",
                 "Duration Minutes / 总时长(分钟)", "Limited Free / 是否限免",
                 "English Themes / 英文题材", "Chinese Themes / 中文题材",
                 "English Description Preview / 英文简介预览", "Chinese Description / 中文简介",
                 "Description Truncated / 简介是否截断", "Drama Link / 短剧链接",
                 "Source Profile URL / 来源主页"]
DRAMA_DETAIL_CACHE_FIELDS = (
    "english_title", "chinese_title", "publish_time", "duration_seconds", "duration_minutes",
    "limited_free", "english_themes", "chinese_themes", "english_description",
    "chinese_description", "description_truncated",
)

JOB_LOCK = threading.Lock()
LAST_JOB = {"running": False, "started_at": None, "finished_at": None, "result": None, "error": None}
LAST_AUTO_REFRESH_AT = 0.0
DRAMA_DETAIL_CACHE = None
DRAMA_DETAIL_CACHE_LOCK = threading.Lock()
DRAMA_EPISODE_HISTORY_LOCK = threading.Lock()
TITLE_TRANSLATION_CACHE = {}
VIDEO_PLAY_URL_CACHE = {}
VIDEO_PLAY_URL_CACHE_LOCK = threading.Lock()
THEME_TRANSLATION_MAP = {
    "rural area": "乡村",
    "ensemble cast": "群像",
    "family disputes": "家庭纠纷",
    "city": "城市",
    "urbanlife general settings": "都市",
    "urban life": "都市",
    "farming/business": "种田/经商",
    "skill/talent competition": "技能/才艺竞赛",
    "superior and inferior": "强弱逆袭",
    "werewolf": "狼人",
    "billionaire": "豪门总裁",
    "ceo": "总裁",
    "marriage": "婚恋",
    "romance": "爱情",
    "revenge": "复仇",
    "fantasy": "奇幻",
    "drama": "剧情",
    "comedy": "喜剧",
    "suspense": "悬疑",
    "crime": "犯罪",
    "medical": "医疗",
    "campus": "校园",
    "royal": "皇室",
    "pregnancy": "孕育",
    "secret identity": "隐藏身份",
}


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


def _to_text(value, limit=None):
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "Yes" if value else "No"
    elif isinstance(value, (list, tuple, set)):
        parts = [_to_text(item) for item in value]
        text = "; ".join(part for part in parts if part)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _clean_drama_id(value):
    text = str(value or "").strip()
    if text.upper().startswith("ID "):
        text = text[3:].strip()
    return re.sub(r"[^0-9A-Za-z_-]", "", text)


def _first_http_url(obj, keys):
    for value in _collect_key_values(obj, keys):
        text = _to_text(value)
        match = re.search(r"https?://[^\s\"\'<>]+", text)
        if match:
            return match.group(0).rstrip(").,;")
    return ""


def _build_tiktok_video_url(uid, video_id):
    account = str(uid or "").strip().lstrip("@")
    clean_id = _clean_drama_id(video_id)
    if not account or not clean_id:
        return ""
    return "https://www.tiktok.com/@%s/video/%s" % (urllib.parse.quote(account, safe="._-"), urllib.parse.quote(clean_id, safe=""))


def _video_link_from_item(uid, item):
    return _first_http_url(item, VIDEO_LINK_KEYS) or _build_tiktok_video_url(uid, _get_video_id(item))


def _first_addr_url(value):
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "urls"):
            urls = value.get(key)
            if isinstance(urls, list):
                for url in urls:
                    text = _to_text(url)
                    if text.startswith("http"):
                        return text
            elif isinstance(urls, str) and urls.startswith("http"):
                return urls
        for key in ("url", "uri"):
            text = _to_text(value.get(key))
            if text.startswith("http"):
                return text
    elif isinstance(value, list):
        for item in value:
            text = _to_text(item)
            if text.startswith("http"):
                return text
    return ""


def _video_play_url_from_item(item):
    if not isinstance(item, dict):
        return ""
    video = item.get("video")
    if not isinstance(video, dict):
        return ""
    containers = []
    for key in (
        "play_addr_h264", "playAddrH264", "play_addr",
        "playAddr", "play_addr_bytevc1", "playAddrBytevc1",
    ):
        containers.append(video.get(key))
    bit_rates = video.get("bit_rate") or video.get("bitRate") or []
    if isinstance(bit_rates, list):
        for item_rate in bit_rates:
            if isinstance(item_rate, dict):
                containers.append(item_rate.get("play_addr") or item_rate.get("playAddr"))
    for key in ("download_addr", "downloadAddr"):
        containers.append(video.get(key))
    for container in containers:
        url = _first_addr_url(container)
        if url:
            return url
    return ""


def _video_play_url_from_tree(obj, depth=0):
    if depth > 8 or obj is None:
        return ""
    if isinstance(obj, dict):
        url = _video_play_url_from_item(obj)
        if url:
            return url
        for key in (
            "aweme_detail", "awemeDetail", "item_info", "itemInfo",
            "item", "aweme", "video_detail", "videoDetail", "data",
        ):
            if key in obj:
                url = _video_play_url_from_tree(obj.get(key), depth + 1)
                if url:
                    return url
        for key in ("aweme_list", "awemeList", "item_list", "itemList"):
            value = obj.get(key)
            if isinstance(value, list):
                for item in value:
                    url = _video_play_url_from_tree(item, depth + 1)
                    if url:
                        return url
        for value in obj.values():
            if isinstance(value, (dict, list)):
                url = _video_play_url_from_tree(value, depth + 1)
                if url:
                    return url
    elif isinstance(obj, list):
        for item in obj:
            url = _video_play_url_from_tree(item, depth + 1)
            if url:
                return url
    return ""


def _direct_find_any(obj, keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj and not isinstance(obj[key], (dict, list)):
            return obj[key]
    return None


def _collect_key_values(obj, keys, out=None, depth=0):
    if out is None:
        out = []
    if depth > 9 or obj is None:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and not isinstance(value, (dict, list)):
                out.append(value)
            elif isinstance(value, (dict, list)):
                _collect_key_values(value, keys, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_key_values(item, keys, out, depth + 1)
    return out


def _publish_epoch(value):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            stamp = float(text)
        except Exception:
            return None
        if stamp > 100000000000:
            stamp = stamp / 1000.0
        if stamp > 1000000000:
            return stamp
        return None
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BEIJING_TZ)
        return dt.timestamp()
    except Exception:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.datetime.strptime(text[:19], pattern)
            return dt.replace(tzinfo=BEIJING_TZ).timestamp()
        except Exception:
            continue
    return None


def _format_publish_time(value):
    epoch = _publish_epoch(value)
    if epoch is not None:
        return datetime.datetime.fromtimestamp(epoch, BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    text = _to_text(value, 80)
    if not text or not re.search(r"\d", text):
        return ""
    return text


def _publish_time_of(obj):
    direct = _format_publish_time(_direct_find_any(obj, DRAMA_PUBLISH_TIME_KEYS))
    if direct:
        return direct
    candidates = []
    for value in _collect_key_values(obj, DRAMA_PUBLISH_TIME_KEYS):
        formatted = _format_publish_time(value)
        if formatted:
            candidates.append(formatted)
    if not candidates:
        return ""
    return min(candidates, key=lambda item: _publish_epoch(item) or float("inf"))


def _has_cjk(text):
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _translate_english_title(title):
    title = _to_text(title, 160)
    if not title:
        return ""
    if _has_cjk(title):
        return title
    key = title.lower()
    if key in TITLE_TRANSLATION_CACHE:
        return TITLE_TRANSLATION_CACHE[key]
    if not SCHEDULE_TRANSLATE_TITLES:
        TITLE_TRANSLATION_CACHE[key] = ""
        return ""
    translated = ""
    params = urllib.parse.urlencode({
        "client": "gtx",
        "sl": "en",
        "tl": "zh-CN",
        "dt": "t",
        "q": title,
    })
    url = TRANSLATE_HOST + "/translate_a/single?" + params
    headers = {"Accept": "application/json", "User-Agent": DEFAULT_UA}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        translated = "".join(part[0] for part in (data[0] or []) if part and part[0])
        translated = _to_text(translated, 160)
        if translated.lower() == title.lower():
            translated = ""
    except Exception:
        translated = ""
    if translated:
        TITLE_TRANSLATION_CACHE[key] = translated
    return translated


def _chinese_title_or_translate(chinese_title, english_title):
    chinese_title = _to_text(chinese_title, 160)
    if chinese_title:
        return chinese_title
    return _translate_english_title(english_title)


def _clean_theme_label(value):
    text = _to_text(value, 80)
    if not text:
        return ""
    text = re.sub(r"^tag[_\s-]*", "", text, flags=re.I).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.isdigit() or re.search(r"[{}\[\]\":]", text):
        return ""
    if re.match(r"^id\s*\d+", text, flags=re.I):
        return ""
    return text[:60]


def _theme_values(value, out=None):
    if out is None:
        out = []
    if value in (None, ""):
        return out
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _theme_values(item, out)
        return out
    if isinstance(value, dict):
        for key in ("tagVal", "tagName", "themeVal", "themeName", "name", "title", "value", "label"):
            if key in value and not isinstance(value[key], (dict, list, tuple, set)):
                label = _clean_theme_label(value[key])
                if label:
                    out.append(label)
        for key, item in value.items():
            if key in ("tagID", "tagKey") or re.search(r"(?:id|key)$", str(key), flags=re.I):
                continue
            _theme_values(item, out)
        return out
    text = str(value).strip()
    if not text:
        return out
    if text[:1] in ("{", "["):
        try:
            return _theme_values(json.loads(text), out)
        except Exception:
            pass
    matched = False
    for match in re.finditer(r'"tagVal"\s*:\s*"([^"]+)"', text):
        label = _clean_theme_label(match.group(1))
        if label:
            out.append(label)
            matched = True
    if matched:
        return out
    if re.search(r"[{}\[\]\"]", text):
        return out
    for part in re.split(r"[;；,，、|]+", text):
        label = _clean_theme_label(part)
        if label:
            out.append(label)
    return out


def _theme_text(value, translate=False):
    labels, seen = [], set()
    for label in _theme_values(value, []):
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        if translate and not _has_cjk(label):
            label = THEME_TRANSLATION_MAP.get(key) or _translate_english_title(label) or label
        labels.append(label)
        if len(labels) >= 12:
            break
    return "、".join(labels)


def _yes_no(value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "是 / Yes" if value else "否 / No"
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "free", "limited"):
        return "是 / Yes"
    if text in ("0", "false", "no", "n", "none"):
        return "否 / No"
    return _to_text(value, 30)


def _duration_minutes(seconds, explicit_minutes=None):
    minutes = _to_int(explicit_minutes)
    if minutes:
        return round(minutes, 1)
    sec = _to_int(seconds)
    return round(sec / 60, 1) if sec else 0


def _has_cache_value(value):
    return value not in (None, "", [], {})


def _drama_cache_key(uid, drama_id, title):
    key = str(drama_id or "").strip()
    if key.upper().startswith("ID "):
        key = key[3:].strip()
    if not key:
        key = re.sub(r"\s+", " ", str(title or "").strip().lower())
    if not key:
        return ""
    return "%s|%s" % (str(uid or "").strip().lower(), key)


def _seed_cache_from_latest(cache):
    candidates = [
        os.path.join(REPORTS_DIR, "latest_report.json"),
        os.path.join(PUBLIC_REPORTS_DIR, "latest_report.json"),
    ]
    latest = next((path for path in candidates if os.path.isfile(path)), "")
    if not latest:
        return
    try:
        with open(latest, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return
    for row in payload.get("dramas_detail", []) or []:
        if not isinstance(row, dict):
            continue
        key = _drama_cache_key(row.get("Account / 账号") or row.get("账号"),
                               row.get("Drama ID / 短剧ID"), row.get("English Title / 英文剧名") or row.get("短剧名"))
        if not key:
            continue
        cached = cache.setdefault(key, {})
        mapping = {
            "english_title": row.get("English Title / 英文剧名"),
            "chinese_title": row.get("Chinese Title / 中文剧名"),
            "publish_time": row.get("Publish Time / 发布时间"),
            "duration_seconds": row.get("Duration Seconds / 总时长(秒)"),
            "duration_minutes": row.get("Duration Minutes / 总时长(分钟)"),
            "limited_free": row.get("Limited Free / 是否限免"),
            "english_themes": row.get("English Themes / 英文题材"),
            "chinese_themes": row.get("Chinese Themes / 中文题材"),
            "english_description": row.get("English Description Preview / 英文简介预览"),
            "chinese_description": row.get("Chinese Description / 中文简介"),
            "description_truncated": row.get("Description Truncated / 简介是否截断"),
        }
        for field, value in mapping.items():
            if _has_cache_value(value):
                cached[field] = value


def _load_drama_detail_cache():
    global DRAMA_DETAIL_CACHE
    if DRAMA_DETAIL_CACHE is not None:
        return DRAMA_DETAIL_CACHE
    with DRAMA_DETAIL_CACHE_LOCK:
        if DRAMA_DETAIL_CACHE is not None:
            return DRAMA_DETAIL_CACHE
        cache = {}
        try:
            with open(DRAMA_DETAIL_CACHE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                cache = data
        except Exception:
            cache = {}
        if not cache:
            _seed_cache_from_latest(cache)
        DRAMA_DETAIL_CACHE = cache
        return DRAMA_DETAIL_CACHE


def _remember_drama_detail(uid, drama_id, title, detail):
    key = _drama_cache_key(uid, drama_id, title)
    if not key:
        return
    cache = _load_drama_detail_cache()
    cached = cache.setdefault(key, {})
    for field in DRAMA_DETAIL_CACHE_FIELDS:
        value = detail.get(field)
        if _has_cache_value(value):
            cached[field] = value


def _apply_cached_drama_detail(uid, drama_id, title, detail):
    key = _drama_cache_key(uid, drama_id, title)
    if not key:
        return detail
    cached = _load_drama_detail_cache().get(key)
    if not isinstance(cached, dict):
        return detail
    for field in DRAMA_DETAIL_CACHE_FIELDS:
        if not _has_cache_value(detail.get(field)) and _has_cache_value(cached.get(field)):
            detail[field] = cached[field]
    return detail


def _save_drama_detail_cache():
    cache = _load_drama_detail_cache()
    if not cache:
        return
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp = DRAMA_DETAIL_CACHE_FILE + ".tmp"
    with DRAMA_DETAIL_CACHE_LOCK:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, DRAMA_DETAIL_CACHE_FILE)


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


def _deep_find_any(obj, keys, depth=0):
    if depth > 9 or obj is None:
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _deep_find_any(item, keys, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(obj, dict):
        for key in keys:
            if key in obj:
                return obj[key]
        for item in obj.values():
            found = _deep_find_any(item, keys, depth + 1)
            if found is not None:
                return found
    return None


def _first_profile_image(data):
    value = _deep_find_any(data, AVATAR_KEYS)
    url = _first_addr_url(value)
    if url:
        return url
    text = _to_text(value)
    return text if text.startswith("http") else ""


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


def _get_video_play_url(video_id, started=None):
    clean_id = _clean_drama_id(video_id)
    if not clean_id or (started is not None and _runtime_exceeded(started)):
        return ""
    now = time.time()
    if VIDEO_PLAY_URL_CACHE_TTL_SECONDS:
        with VIDEO_PLAY_URL_CACHE_LOCK:
            cached = VIDEO_PLAY_URL_CACHE.get(clean_id)
            if cached and now - cached.get("ts", 0) < VIDEO_PLAY_URL_CACHE_TTL_SECONDS:
                return cached.get("url", "")
    for endpoint in SINGLE_VIDEO_EP_CANDIDATES:
        if started is not None and _runtime_exceeded(started):
            return ""
        params = {"aweme_id": clean_id}
        if endpoint.endswith("_v3"):
            params["region"] = TIKTOK_REGION
        try:
            data = _send_tikhub_get(endpoint, params, "TikHub single video endpoint")
        except TikHubError:
            continue
        url = _video_play_url_from_tree(data)
        if url:
            if VIDEO_PLAY_URL_CACHE_TTL_SECONDS:
                with VIDEO_PLAY_URL_CACHE_LOCK:
                    VIDEO_PLAY_URL_CACHE[clean_id] = {"url": url, "ts": now}
            return url
    return ""


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


def _schedule_account_pool():
    try:
        with open(SCHEDULE_ACCOUNTS_FILE, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        accounts = payload.get("accounts", [])
        updated_at = payload.get("updated_at", "")
    else:
        accounts = payload
        updated_at = ""
    if isinstance(accounts, list):
        accounts = _parse_accounts("\n".join(str(item) for item in accounts))
    else:
        accounts = _parse_accounts(accounts)
    return {"accounts": accounts, "updated_at": updated_at, "source": "file" if accounts else ""}


def _configured_schedule_accounts():
    pool = _schedule_account_pool()
    if pool["accounts"]:
        return pool["accounts"], "backend_pool"
    env_accounts = _parse_accounts(SCHEDULE_ACCOUNTS)
    return env_accounts, "SCHEDULE_ACCOUNTS" if env_accounts else ""


def _write_schedule_account_pool(accounts):
    accounts = _parse_accounts("\n".join(str(item) for item in (accounts or [])))
    os.makedirs(REPORTS_DIR, exist_ok=True)
    payload = {
        "updated_at": datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "accounts": accounts,
    }
    tmp = SCHEDULE_ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, SCHEDULE_ACCOUNTS_FILE)
    return payload


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
    profile = {"nickname": uid, "followers": 0, "hearts": 0, "videoCount": 0, "avatar": ""}
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
    profile["avatar"] = _first_profile_image(data)
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
                video_play_url = _video_play_url_from_item(video)
                video_link = _first_http_url(video, VIDEO_LINK_KEYS) or _build_tiktok_video_url(uid, video_id)
                videos.append({"id": video_id, "desc": _get_desc(video), "views": _get_play_count(video),
                               "publish_time": _publish_time_of(video), "url": video_link,
                               "play_url": video_play_url})
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
                "publish_time": _publish_time_of(item),
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


def _get_playlist_video_stats(playlist_id, started, uid=""):
    seen, total_views, episodes, first_link = set(), 0, 0, ""
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
            if not first_link:
                first_link = _video_play_url_from_item(video) or _video_link_from_item(uid, video)
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
    return {"episodes": episodes, "views": total_views, "first_link": first_link}


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
                stats = _get_playlist_video_stats(playlist["id"], started, uid)
                if stats["episodes"]:
                    episodes = stats["episodes"]
                    views = stats["views"]
                    playlist["first_link"] = stats.get("first_link", "")
            except TikHubError as exc:
                if exc.status in (401, 402, 403):
                    raise
        name = (_clean_title(playlist["name"])[:60].strip() or playlist["name"] or playlist["id"])
        dramas.append({"name": name, "episodes": episodes, "views": views,
                       "publish_time": playlist.get("publish_time", ""),
                       "playlist_id": playlist.get("id", ""),
                       "drama_link": playlist.get("first_link", "")})
    return dramas


def _get_drama_first_episode_publish_time(drama_id, uid, started):
    if not SCHEDULE_FETCH_EPISODE_PUBLISH_TIME or not drama_id or _runtime_exceeded(started):
        return ""
    try:
        data = _send_tiktok_get("/api/drama/episode/item_list/", {
            "dramaID": drama_id,
            "aid": TIKTOK_AID,
            "language": TIKTOK_LANGUAGE,
            "region": TIKTOK_REGION,
            "storeRegion": TIKTOK_REGION,
            "count": str(SCHEDULE_PUBLISH_TIME_EPISODE_SAMPLE),
            "cursor": "0",
        }, "TikTok drama episode endpoint", uid)
    except TikHubError:
        return ""
    if not isinstance(data, dict):
        return ""
    batch = data.get("itemList") or data.get("item_list") or []
    if not isinstance(batch, list):
        return ""
    candidates = []
    for item in batch:
        formatted = _publish_time_of(item)
        if formatted:
            candidates.append(formatted)
    if not candidates:
        return ""
    return min(candidates, key=lambda item: _publish_epoch(item) or float("inf"))


def _get_drama_episode_items(drama_id, uid, started=None, limit=None):
    clean_id = _clean_drama_id(drama_id)
    if not clean_id or (started is not None and _runtime_exceeded(started)):
        return []
    max_items = max(1, int(limit or DRAMA_LINK_MAX_EPISODES))
    items, seen = [], set()
    cursor, prev, stall = "0", None, 0
    for _page in range(1, SCHEDULE_MAX_PAGES + 1):
        if started is not None and _runtime_exceeded(started):
            break
        count = min(DRAMA_LINK_PAGE_SIZE, max_items - len(items))
        if count <= 0:
            break
        try:
            data = _send_tiktok_get("/api/drama/episode/item_list/", {
                "dramaID": clean_id,
                "aid": TIKTOK_AID,
                "language": TIKTOK_LANGUAGE,
                "region": TIKTOK_REGION,
                "storeRegion": TIKTOK_REGION,
                "count": str(count),
                "cursor": str(cursor),
            }, "TikTok drama episode endpoint", uid)
        except TikHubError:
            break
        if not isinstance(data, dict):
            break
        batch = data.get("itemList") or data.get("item_list") or []
        if not isinstance(batch, list):
            batch = []
        added = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            video_id = _get_video_id(item)
            key = video_id or json.dumps(item, ensure_ascii=False, sort_keys=True)[:240]
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            added += 1
            if len(items) >= max_items:
                return items
        has_more, next_cursor = _read_pagination(data)
        if has_more in (False, 0, "0"):
            break
        advanced = next_cursor not in (None, "", "0") and str(next_cursor) != str(cursor) and str(next_cursor) != str(prev)
        if advanced:
            prev, cursor = cursor, str(next_cursor)
            stall = 0 if added else stall + 1
            if stall >= 3:
                break
            continue
        if not batch or not added:
            break
        stall += 1
        if stall >= 3:
            break
    return items


def _get_drama_episode_link(drama_id, uid, started=None, target="play"):
    clean_id = _clean_drama_id(drama_id)
    if not clean_id or (started is not None and _runtime_exceeded(started)):
        return ""
    batch = _get_drama_episode_items(clean_id, uid, started, limit=5)
    fallback = ""
    prefer_play = str(target or "").strip().lower() in ("", "play", "source", "direct", "media")
    for item in batch:
        if prefer_play:
            play_url = _video_play_url_from_item(item)
            if play_url:
                return play_url
            play_url = _get_video_play_url(_get_video_id(item), started)
            if play_url:
                return play_url
        if not fallback:
            fallback = _video_link_from_item(uid, item)
    return fallback


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
            episodes = _to_int(_deep_find(item, DRAMA_COUNT_KEYS))
            views = _to_int(_deep_find(item, DRAMA_VIEW_KEYS))
            duration_seconds = _to_int(_deep_find(item, DRAMA_DURATION_SECONDS_KEYS))
            english_title = _to_text(_deep_find(item, DRAMA_EN_TITLE_KEYS) or name, 160)
            chinese_title = _chinese_title_or_translate(_deep_find(item, DRAMA_CN_TITLE_KEYS), english_title)
            english_desc = _to_text(_deep_find_any(item, DRAMA_EN_DESC_KEYS), 600)
            chinese_desc = _to_text(_deep_find_any(item, DRAMA_CN_DESC_KEYS), 600)
            publish_time = _publish_time_of(item)
            english_themes_source = _deep_find_any(item, DRAMA_EN_THEMES_KEYS)
            chinese_themes = _theme_text(_deep_find_any(item, DRAMA_CN_THEMES_KEYS))
            if not chinese_themes:
                chinese_themes = _theme_text(english_themes_source, translate=True)
            drama_link = _first_http_url(item, DRAMA_LINK_KEYS)
            detail = _apply_cached_drama_detail(uid, drama_key, name, {
                "english_title": english_title,
                "chinese_title": chinese_title,
                "publish_time": publish_time,
                "duration_seconds": duration_seconds,
                "duration_minutes": _duration_minutes(duration_seconds, _deep_find(item, DRAMA_DURATION_MINUTES_KEYS)),
                "limited_free": _yes_no(_deep_find(item, DRAMA_LIMITED_KEYS)),
                "english_themes": _theme_text(english_themes_source),
                "chinese_themes": chinese_themes,
                "english_description": english_desc,
                "chinese_description": chinese_desc,
                "description_truncated": "是 / Yes" if len(english_desc) >= 600 or len(chinese_desc) >= 600 else "否 / No",
            })
            if not detail.get("publish_time") and drama_key:
                detail["publish_time"] = _get_drama_first_episode_publish_time(drama_key, uid, started)
            _remember_drama_detail(uid, drama_key, name, detail)
            dramas.append({
                "name": (_clean_title(name)[:80].strip() or name[:80] or key),
                "episodes": episodes,
                "views": views,
                "drama_id": ("ID " + drama_key) if drama_key and not drama_key.upper().startswith("ID ") else drama_key,
                "drama_link": drama_link,
                **detail,
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
        publish_times = [ep.get("publish_time", "") for ep in episodes if ep.get("publish_time")]
        publish_time = min(publish_times, key=lambda item: _publish_epoch(item) or float("inf")) if publish_times else ""
        dramas.append({"name": name, "episodes": len(episodes), "views": total_views,
                       "publish_time": publish_time, "top_video_id": top.get("id", ""),
                       "drama_link": top.get("play_url") or top.get("url", "")})
    return dramas


def _build_summary_row(uid, profile, videos, dramas):
    total_episodes = sum(_to_int(drama.get("episodes")) for drama in dramas) if dramas else len(videos)
    total_views = sum(_to_int(drama.get("views")) for drama in dramas) if dramas else sum(video["views"] for video in videos)
    drama_count = len(dramas)
    avg_views = round(total_views / drama_count) if drama_count else 0
    top_name, top_chinese_title, top_views = "", "", 0
    if dramas:
        top = max(dramas, key=lambda item: item["views"])
        top_name, top_views = top["name"], top["views"]
        top_chinese_title = _chinese_title_or_translate(top.get("chinese_title", ""), top.get("english_title") or top_name)
    return {
        "截图名称": profile["nickname"],
        "账号": uid,
        "昵称": profile["nickname"],
        "头像": profile.get("avatar", ""),
        "粉丝": profile["followers"],
        "点赞": profile["hearts"],
        "短剧数": drama_count,
        "总集数": total_episodes,
        "累计观看": total_views,
        "单剧均观看": avg_views,
        "最高观看短剧": top_name,
        "最高观看短剧中文名": top_chinese_title,
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
    for rank, drama in enumerate(dramas, 1):
        episodes = _to_int(drama.get("episodes"))
        views = _to_int(drama.get("views"))
        avg_views = round(views / episodes) if episodes else 0
        title = drama.get("english_title") or drama.get("name") or ""
        chinese_title = _chinese_title_or_translate(drama.get("chinese_title", ""), title)
        profile_url = "https://www.tiktok.com/@" + uid
        drama_link = (drama.get("drama_link", "") or
                      _build_tiktok_video_url(uid, drama.get("top_video_id", "")))
        drama_rows.append({
            "Account / 账号": uid,
            "Nickname / 昵称": profile["nickname"],
            "Screenshot Name / 截图名称": profile["nickname"],
            "Rank in Account / 账号内排序": rank,
            "Drama ID / 短剧ID": drama.get("drama_id", ""),
            "English Title / 英文剧名": title,
            "Chinese Title / 中文剧名": chinese_title,
            "Publish Time / 发布时间": drama.get("publish_time", ""),
            "Episodes / 集数": episodes,
            "Views / 观看数": views,
            "Duration Seconds / 总时长(秒)": _to_int(drama.get("duration_seconds")),
            "Duration Minutes / 总时长(分钟)": drama.get("duration_minutes", 0),
            "Limited Free / 是否限免": drama.get("limited_free", ""),
            "English Themes / 英文题材": drama.get("english_themes", ""),
            "Chinese Themes / 中文题材": drama.get("chinese_themes", ""),
            "English Description Preview / 英文简介预览": drama.get("english_description", ""),
            "Chinese Description / 中文简介": drama.get("chinese_description", ""),
            "Description Truncated / 简介是否截断": drama.get("description_truncated", ""),
            "Drama Link / 短剧链接": drama_link,
            "Source Profile URL / 来源主页": profile_url,
            "账号": uid,
            "昵称": profile["nickname"],
            "短剧名": title,
            "集数": episodes,
            "累计观看": views,
            "单集均观看": avg_views,
            "主页链接": profile_url,
            "短剧链接": drama_link,
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
    try:
        _save_drama_detail_cache()
    except Exception:
        pass
    files = _write_report_bundle(rows, drama_rows, errors)
    try:
        episode_history = _save_scheduled_episode_history(drama_rows)
    except Exception as exc:
        episode_history = {
            "enabled": bool(SCHEDULE_SAVE_EPISODE_HISTORY),
            "ok": False,
            "error": str(exc),
            "file": "reports/drama_episode_history.json",
        }
    return {
        "ok": True,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "accounts_requested": len(accounts),
        "accounts_ok": len(rows),
        "accounts_failed": len(errors),
        "dramas": len(drama_rows),
        "files": files,
        "episode_history": episode_history,
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


def _html_text(value, limit=None):
    return html.escape(_to_text(value, limit), quote=True)


def _trim_decimal(value):
    return ("%.2f" % value).rstrip("0").rstrip(".")


def _format_chinese_count(value):
    number = _to_int(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 100000000:
        return sign + _trim_decimal(number / 100000000.0) + "\u4ebf"
    if number >= 10000:
        return sign + _trim_decimal(number / 10000.0) + "\u4e07"
    return sign + str(number)


def _episode_history_key(uid, drama_id, video_id):
    account = str(uid or "").strip().lstrip("@").lower()
    clean_drama = _clean_drama_id(drama_id)
    clean_video = _clean_drama_id(video_id)
    if not account or not clean_drama or not clean_video:
        return ""
    return "%s|%s|%s" % (account, clean_drama, clean_video)


def _episode_point_ms(point):
    if not isinstance(point, dict):
        return 0
    value = _to_int(point.get("ms") or point.get("ts_ms") or point.get("timestamp_ms"))
    if value:
        return value
    raw = point.get("ts") or point.get("timestamp")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=BEIJING_TZ)
            return int(parsed.timestamp() * 1000)
        except Exception:
            return 0
    return 0


def _read_drama_episode_history():
    data = {}
    for path in (DRAMA_EPISODE_HISTORY_FILE, os.path.join(PUBLIC_REPORTS_DIR, "drama_episode_history.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            break
        except Exception:
            data = {}
    items = data.get("items") if isinstance(data, dict) else {}
    if not isinstance(items, dict):
        items = {}
    return {"version": 1, "items": items}


def _write_drama_episode_history(history):
    items = history.get("items") if isinstance(history, dict) else {}
    if not isinstance(items, dict) or not items:
        return
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp = DRAMA_EPISODE_HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "items": items}, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, DRAMA_EPISODE_HISTORY_FILE)


def _episode_growth_from_points(points, current_views, now_ms, days):
    if not isinstance(points, list):
        return None
    current = _to_int(current_views)
    target_ms = now_ms - days * 86400000
    recent_cutoff_ms = now_ms - 60000
    usable = []
    for point in points:
        ms = _episode_point_ms(point)
        if ms <= 0 or ms > recent_cutoff_ms:
            continue
        usable.append((ms, _to_int(point.get("views")), point))
    if not usable:
        return None
    older = [item for item in usable if item[0] <= target_ms]
    if not older:
        return None
    baseline = max(older, key=lambda item: item[0])
    growth = max(0, current - baseline[1])
    return {
        "value": growth,
        "days": days,
        "baseline_views": baseline[1],
        "baseline_ts": baseline[2].get("ts") or "",
        "current_views": current,
    }


def _trim_episode_history_points(points, now_ms):
    cutoff_ms = now_ms - DRAMA_EPISODE_HISTORY_MAX_AGE_DAYS * 86400000
    kept = []
    for point in points:
        ms = _episode_point_ms(point)
        if ms <= 0:
            continue
        if ms >= cutoff_ms:
            kept.append(point)
    kept.sort(key=_episode_point_ms)
    return kept[-DRAMA_EPISODE_HISTORY_MAX_POINTS:]


def _record_episode_history_entries(history, uid, drama_id, episodes, now_ms, now_text, collect_metrics=True):
    metrics, changed, recorded = {}, False, 0
    if not isinstance(history, dict):
        history = {}
    items = history.get("items") if isinstance(history, dict) else {}
    if not isinstance(items, dict):
        items = {}
    history["items"] = items
    for episode in episodes:
        video_id = _clean_drama_id(episode.get("video_id"))
        key = _episode_history_key(uid, drama_id, video_id)
        if not key:
            continue
        entry = items.get(key)
        if not isinstance(entry, dict):
            entry = {}
            items[key] = entry
        points = entry.get("points")
        if not isinstance(points, list):
            points = []
        if collect_metrics:
            metrics[video_id] = {
                "week": _episode_growth_from_points(points, episode.get("views"), now_ms, 7),
                "month": _episode_growth_from_points(points, episode.get("views"), now_ms, 30),
            }
        entry.update({
            "uid": str(uid or "").strip().lstrip("@"),
            "drama_id": _clean_drama_id(drama_id),
            "video_id": video_id,
            "episode_label": episode.get("episode_label") or "",
            "title": episode.get("title") or "",
        })
        points = _trim_episode_history_points(points, now_ms)
        snapshot = {"ms": now_ms, "ts": now_text, "views": _to_int(episode.get("views"))}
        if points and now_ms - _episode_point_ms(points[-1]) <= DRAMA_EPISODE_HISTORY_DEDUP_SECONDS * 1000:
            points[-1] = snapshot
        else:
            points.append(snapshot)
        entry["points"] = _trim_episode_history_points(points, now_ms)
        changed = True
        recorded += 1
    return metrics, changed, recorded


def _collect_episode_growth_and_record(uid, drama_id, episodes):
    metrics = {}
    if not episodes:
        return metrics
    now_ms = int(time.time() * 1000)
    now_text = datetime.datetime.fromtimestamp(now_ms / 1000.0, BEIJING_TZ).isoformat(timespec="seconds")
    changed = False
    with DRAMA_EPISODE_HISTORY_LOCK:
        history = _read_drama_episode_history()
        metrics, changed, _recorded = _record_episode_history_entries(history, uid, drama_id, episodes, now_ms, now_text, collect_metrics=True)
        if changed:
            try:
                _write_drama_episode_history(history)
            except Exception:
                pass
    return metrics


def _episode_growth_html(metric):
    if not metric:
        return '<span class="growth-empty">&#8212;</span>'
    value = _to_int(metric.get("value"))
    title = "\u5bf9\u6bd4\u5386\u53f2\u5feb\u7167 %s\uff1a\u57fa\u51c6 %s\uff0c\u5f53\u524d %s" % (
        metric.get("baseline_ts") or "",
        _format_chinese_count(metric.get("baseline_views")),
        _format_chinese_count(metric.get("current_views")),
    )
    if value > 0:
        return '<span class="growth-up" title="%s"><span class="trend-arrow">&#8593;</span>+%s</span>' % (
            _html_text(title),
            _html_text(_format_chinese_count(value)),
        )
    return '<span class="growth-flat" title="%s">+0</span>' % _html_text(title)


def _get_drama_episode_number(item, fallback):
    containers = []
    if isinstance(item, dict):
        drama_info = item.get("dramaInfo") or item.get("drama_info")
        if isinstance(drama_info, dict):
            video_data = drama_info.get("DramaVideoData") or drama_info.get("dramaVideoData") or drama_info.get("drama_video_data")
            if isinstance(video_data, dict):
                containers.append(video_data)
            containers.append(drama_info)
        containers.append(item)
    for container in containers:
        for key in DRAMA_EPISODE_NUMBER_KEYS:
            if key in container:
                value = _to_int(container.get(key))
                if value > 0:
                    return value
    value = _to_int(_deep_find(item, DRAMA_EPISODE_NUMBER_KEYS))
    return value if value > 0 else fallback


def _drama_episode_summary(item, uid, index):
    video_id = _get_video_id(item)
    episode_no = _get_drama_episode_number(item, index)
    page_url = _video_link_from_item(uid, item)
    play_url = ""
    if video_id:
        play_url = "/drama-link?" + urllib.parse.urlencode({
            "uid": uid,
            "video_id": video_id,
            "target": "play",
            "redirect": "1",
        })
    return {
        "index": index,
        "episode_no": episode_no,
        "episode_label": "\u7b2c%s\u96c6" % episode_no,
        "video_id": video_id,
        "title": _get_desc(item) or _to_text(_deep_find(item, DESC_KEYS), 160) or ("Episode %s" % index),
        "publish_time": _publish_time_of(item),
        "views": _get_play_count(item),
        "views_text": _format_chinese_count(_get_play_count(item)),
        "video_url": page_url,
        "play_url": play_url,
    }


def _drama_row_value(row, keys):
    if not isinstance(row, dict):
        return ""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _scheduled_episode_history_targets(drama_rows):
    targets, seen = [], set()
    for row in drama_rows:
        uid = str(_drama_row_value(row, ("Account / \u8d26\u53f7", "\u8d26\u53f7", "Account")) or "").strip().lstrip("@")
        drama_id = _clean_drama_id(_drama_row_value(row, ("Drama ID / \u77ed\u5267ID", "\u77ed\u5267ID", "Drama ID")))
        if not uid or not drama_id:
            continue
        key = (uid.lower(), drama_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append({
            "uid": uid,
            "drama_id": drama_id,
            "title": _to_text(_drama_row_value(row, ("English Title / \u82f1\u6587\u5267\u540d", "\u77ed\u5267\u540d", "English Title")), 120),
        })
    if SCHEDULE_EPISODE_HISTORY_MAX_DRAMAS:
        return targets[:SCHEDULE_EPISODE_HISTORY_MAX_DRAMAS]
    return targets


def _save_scheduled_episode_history(drama_rows):
    result = {
        "enabled": bool(SCHEDULE_SAVE_EPISODE_HISTORY),
        "ok": True,
        "targets_total": 0,
        "attempted": 0,
        "dramas_ok": 0,
        "dramas_empty": 0,
        "episodes_saved": 0,
        "errors": [],
        "runtime_limited": False,
        "file": "reports/drama_episode_history.json",
    }
    if not SCHEDULE_SAVE_EPISODE_HISTORY:
        return result
    targets = _scheduled_episode_history_targets(drama_rows)
    result["targets_total"] = len(targets)
    if not targets:
        return result
    started = time.time()
    max_episodes = SCHEDULE_EPISODE_HISTORY_MAX_EPISODES or DRAMA_LINK_MAX_EPISODES
    fetched = []
    for target in targets:
        if _runtime_exceeded(started):
            result["runtime_limited"] = True
            break
        uid, drama_id = target["uid"], target["drama_id"]
        result["attempted"] += 1
        try:
            items = _get_drama_episode_items(drama_id, uid, started=started, limit=max_episodes)
            episodes = [_drama_episode_summary(item, uid, idx + 1) for idx, item in enumerate(items)]
            if episodes:
                fetched.append((uid, drama_id, episodes))
                result["dramas_ok"] += 1
                result["episodes_saved"] += len(episodes)
            else:
                result["dramas_empty"] += 1
        except Exception as exc:
            if len(result["errors"]) < 20:
                result["errors"].append({"uid": uid, "drama_id": drama_id, "error": str(exc)})
        if SCHEDULE_EPISODE_HISTORY_DELAY_MS:
            time.sleep(SCHEDULE_EPISODE_HISTORY_DELAY_MS / 1000.0)
    if not fetched:
        return result
    now_ms = int(time.time() * 1000)
    now_text = datetime.datetime.fromtimestamp(now_ms / 1000.0, BEIJING_TZ).isoformat(timespec="seconds")
    with DRAMA_EPISODE_HISTORY_LOCK:
        history = _read_drama_episode_history()
        changed = False
        for uid, drama_id, episodes in fetched:
            _metrics, item_changed, _recorded = _record_episode_history_entries(
                history, uid, drama_id, episodes, now_ms, now_text, collect_metrics=False
            )
            changed = changed or item_changed
        if changed:
            _write_drama_episode_history(history)
    return result


def _render_drama_episode_list_page(uid, drama_id, episodes):
    account = (uid or "").strip().lstrip("@")
    try:
        growth_metrics = _collect_episode_growth_and_record(account, drama_id, episodes)
    except Exception:
        growth_metrics = {}
    rows = []
    for episode in episodes:
        metrics = growth_metrics.get(_clean_drama_id(episode.get("video_id"))) or {}
        page_link = '<a class="link ghost" href="%s" target="_blank" rel="noopener">&#20316;&#21697;&#39029;</a>' % _html_text(episode["video_url"]) if episode.get("video_url") else '<span class="muted">&#26080;</span>'
        play_link = '<a class="link primary" href="%s" target="_blank" rel="noopener">&#25773;&#25918;&#28304;</a>' % _html_text(episode["play_url"]) if episode.get("play_url") else '<span class="muted">&#26080;</span>'
        rows.append("""<tr>
  <td class="idx">%s</td>
  <td><div class="name">%s</div><div class="meta">%s</div></td>
  <td class="hide-sm">%s</td>
  <td class="hide-sm">%s</td>
  <td class="hide-sm growth-cell">%s</td>
  <td class="hide-sm growth-cell">%s</td>
  <td class="actions">%s%s</td>
</tr>""" % (
            _html_text(episode.get("episode_label") or episode.get("index")),
            _html_text(episode.get("title"), 180),
            "ID " + _html_text(episode.get("video_id")) if episode.get("video_id") else "",
            _html_text(episode.get("publish_time") or "N/A"),
            _html_text(episode.get("views_text") or _format_chinese_count(episode.get("views"))),
            _episode_growth_html(metrics.get("week")),
            _episode_growth_html(metrics.get("month")),
            play_link,
            page_link,
        ))
    if not rows:
        rows.append('<tr><td colspan="7" class="empty">No videos found.</td></tr>')
    source_url = "/drama-link?" + urllib.parse.urlencode({
        "uid": account,
        "drama_id": _clean_drama_id(drama_id),
        "target": "list",
        "redirect": "0",
    })
    body = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>&#30701;&#21095;&#35270;&#39057;&#21015;&#34920;</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#667085;--line:#e6eaf1;--head:#1d2633;--bg:#f5f7fb;--blue:#405cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 Arial,"Microsoft YaHei",sans-serif}.wrap{max-width:1180px;margin:24px auto;padding:0 18px}.panel{background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 10px 28px rgba(31,41,55,.08);overflow:hidden}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 20px;border-bottom:1px solid var(--line)}h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px}.tools{display:flex;gap:8px;flex-wrap:wrap}.btn,.link{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:7px 12px;border-radius:6px;text-decoration:none;border:1px solid var(--line);white-space:nowrap}.btn{color:var(--ink);background:#fff}.link.primary{background:var(--blue);border-color:var(--blue);color:#fff}.link.ghost{color:var(--blue);background:#fff;margin-left:8px}table{width:100%%;border-collapse:collapse;table-layout:fixed}thead th{background:var(--head);color:#fff;text-align:left;font-weight:700;padding:12px 14px}tbody td{border-top:1px solid var(--line);padding:12px 14px;vertical-align:top}tbody tr:nth-child(even){background:#fafbfe}.idx{width:64px;color:var(--muted)}.time-col{width:154px}.view-col{width:96px}.growth-col{width:118px}.action-col{width:178px}.name{font-weight:700;word-break:break-word}.meta{margin-top:3px;color:var(--muted);font-size:12px;word-break:break-all}.growth-cell{font-weight:800;white-space:nowrap}.growth-up{display:inline-flex;align-items:center;gap:4px;color:#e11d48}.growth-flat,.growth-empty{color:#98a2b3;font-weight:700}.trend-arrow{font-size:16px;line-height:1}.actions{white-space:nowrap}.empty{text-align:center;color:var(--muted);padding:34px}.note{color:var(--muted);font-size:12px;margin-top:12px}@media(max-width:760px){.top{display:block}.tools{margin-top:12px}table{table-layout:auto}.hide-sm{display:none}.actions{white-space:normal}.link.ghost{margin-left:0;margin-top:6px}}
</style>
</head>
<body>
<div class="wrap">
  <section class="panel">
    <div class="top">
      <div>
        <h1>&#30701;&#21095;&#35270;&#39057;&#21015;&#34920;</h1>
        <div class="sub">@%s &#183; &#20849; %s &#38598; &#183; &#30701;&#21095;ID %s</div>
      </div>
      <div class="tools">
        <a class="btn" href="/" target="_self">&#36820;&#22238;&#25253;&#34920;</a>
        <a class="btn" href="%s" target="_blank" rel="noopener">JSON</a>
      </div>
    </div>
    <table>
      <thead><tr><th class="idx">&#38598;&#25968;</th><th>&#35270;&#39057;</th><th class="hide-sm time-col">&#21457;&#24067;&#26102;&#38388;</th><th class="hide-sm view-col">&#35266;&#30475;</th><th class="hide-sm growth-col">&#21608;&#19978;&#28072;&#28909;&#24230;</th><th class="hide-sm growth-col">&#26376;&#19978;&#28072;&#28909;&#24230;</th><th class="action-col">&#38142;&#25509;</th></tr></thead>
      <tbody>%s</tbody>
    </table>
  </section>
  <div class="note">&#25773;&#25918;&#28304;&#38142;&#25509;&#20250;&#22312;&#28857;&#20987;&#26102;&#23454;&#26102;&#33719;&#21462;&#26368;&#26032;&#30452;&#38142;&#12290;</div>
</div>
</body>
</html>""" % (
        _html_text(account),
        len(episodes),
        _html_text(_clean_drama_id(drama_id)),
        _html_text(source_url),
        "\n".join(rows),
    )
    return body.encode("utf-8")

def _start_configured_scheduled_job_if_idle():
    global LAST_AUTO_REFRESH_AT
    if not SERVER_API_KEY:
        return False
    accounts, _source = _configured_schedule_accounts()
    if not accounts:
        return False
    if LAST_JOB.get("running"):
        return False
    now = time.time()
    if now - LAST_AUTO_REFRESH_AT < AUTO_REFRESH_COOLDOWN_SECONDS:
        return False
    if not JOB_LOCK.acquire(blocking=False):
        return False
    LAST_AUTO_REFRESH_AT = now
    LAST_JOB.update({"running": True, "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                     "finished_at": None, "result": None, "error": None})

    def background():
        try:
            _execute_scheduled_job(accounts)
        finally:
            JOB_LOCK.release()

    threading.Thread(target=background, daemon=True).start()
    return True


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
            job = dict(LAST_JOB)
            if not self._schedule_secret_matches(qs):
                job = {
                    "running": bool(LAST_JOB.get("running")),
                    "started_at": LAST_JOB.get("started_at"),
                    "finished_at": LAST_JOB.get("finished_at"),
                }
            self._send_json(200, {"ok": True, "job": job})
        elif parsed.path == "/schedule-accounts":
            self._schedule_accounts_endpoint(qs)
        elif parsed.path == "/drama-link":
            self._resolve_drama_link(qs)
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
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/schedule-accounts":
            self._schedule_accounts_endpoint(qs)
            return
        if parsed.path == "/save":
            self._save_file()
            return
        if parsed.path == "/translate-titles":
            self._translate_titles()
            return
        target = qs.get("url", [None])[0]
        if not target:
            self._send_bytes(400, b'{"error":"missing url param"}', "application/json")
            return
        self._proxy("POST", target)

    def _require_schedule_secret(self, qs):
        if not SCHEDULE_SECRET:
            self._send_json(503, {"ok": False, "error": "SCHEDULE_SECRET is not configured"})
            return False
        if not self._schedule_secret_matches(qs):
            self._send_json(403, {"ok": False, "error": "bad or missing schedule secret"})
            return False
        return True

    def _schedule_secret_matches(self, qs):
        if not SCHEDULE_SECRET:
            return False
        supplied = qs.get("secret", [""])[0] or self.headers.get("X-Schedule-Secret", "")
        return hmac.compare_digest(str(supplied), SCHEDULE_SECRET)

    def _allow_report_read(self, qs):
        if PUBLIC_REPORTS:
            return True
        return self._require_schedule_secret(qs)

    def _resolve_drama_link(self, qs):
        uid = (qs.get("uid", [""])[0] or qs.get("account", [""])[0]).strip().lstrip("@")
        drama_id = qs.get("drama_id", [""])[0] or qs.get("dramaID", [""])[0]
        video_id = qs.get("video_id", [""])[0] or qs.get("item_id", [""])[0]
        target = qs.get("target", ["play"])[0] or "play"
        target_norm = str(target or "").strip().lower()
        redirect = str(qs.get("redirect", ["1"])[0]).lower() not in ("0", "false", "no")
        if target_norm in ("list", "episodes", "episode_list", "all"):
            if not drama_id:
                self._send_json(400, {"ok": False, "error": "missing drama_id"})
                return
            episode_items = _get_drama_episode_items(drama_id, uid, limit=DRAMA_LINK_MAX_EPISODES)
            episodes = [_drama_episode_summary(item, uid, idx + 1) for idx, item in enumerate(episode_items)]
            if not redirect:
                self._send_json(200, {
                    "ok": True,
                    "target": "list",
                    "uid": uid,
                    "drama_id": _clean_drama_id(drama_id),
                    "count": len(episodes),
                    "episodes": episodes,
                })
                return
            self._send_bytes(200, _render_drama_episode_list_page(uid, drama_id, episodes), "text/html; charset=utf-8", no_cache=True)
            return
        prefer_play = target_norm in ("", "play", "source", "direct", "media")
        link = ""
        if video_id:
            if prefer_play:
                link = _get_video_play_url(video_id)
            if not link:
                link = _build_tiktok_video_url(uid, video_id)
        if not link and drama_id:
            link = _get_drama_episode_link(drama_id, uid, target=target)
        if not link:
            self._send_json(404, {"ok": False, "error": "drama link not found"})
            return
        if redirect:
            self.send_response(302)
            self.send_header("Location", link)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
            self._send_json(200, {"ok": True, "url": link, "target": target})

    def _schedule_accounts_endpoint(self, qs):
        if not self._require_schedule_secret(qs):
            return
        if self.command == "GET":
            pool = _schedule_account_pool()
            accounts, source = _configured_schedule_accounts()
            updated_at = pool.get("updated_at", "") if source == "backend_pool" else ""
            self._send_json(200, {
                "ok": True,
                "accounts": accounts,
                "count": len(accounts),
                "source": source or "empty",
                "updated_at": updated_at,
                "runtime_file": "reports/schedule_accounts.json",
            })
            return
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(ln) or b"{}")
            accounts = payload.get("accounts", payload.get("text", ""))
            if isinstance(accounts, str):
                accounts = _parse_accounts(accounts)
            elif isinstance(accounts, list):
                accounts = _parse_accounts("\n".join(str(item) for item in accounts))
            else:
                accounts = []
            saved = _write_schedule_account_pool(accounts)
            self._send_json(200, {
                "ok": True,
                "accounts": saved["accounts"],
                "count": len(saved["accounts"]),
                "source": "backend_pool",
                "updated_at": saved["updated_at"],
                "runtime_file": "reports/schedule_accounts.json",
            })
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _run_scheduled_endpoint(self, qs):
        if not self._require_schedule_secret(qs):
            return
        if not SERVER_API_KEY:
            self._send_json(503, {"ok": False, "error": "TIKHUB_API_KEY is not configured"})
            return
        configured_accounts, source = _configured_schedule_accounts()
        accounts = _parse_accounts(qs.get("accounts", [""])[0]) or configured_accounts
        if not accounts:
            self._send_json(400, {"ok": False, "error": "schedule account pool is empty"})
            return
        if not JOB_LOCK.acquire(blocking=False):
            self._send_json(409, {"ok": False, "error": "scheduled job already running", "job": LAST_JOB})
            return

        wait = str(qs.get("wait", ["0"])[0]).lower() in ("1", "true", "yes")
        if wait:
            try:
                result = _execute_scheduled_job(accounts)
                if isinstance(result, dict):
                    result["source"] = source
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
        self._send_json(202, {"ok": True, "started": True, "accounts": len(accounts), "source": source, "job": LAST_JOB})

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
            if name == "latest_report.json":
                _start_configured_scheduled_job_if_idle()
            public_full = os.path.normpath(os.path.join(PUBLIC_REPORTS_DIR, name))
            if public_full.startswith(PUBLIC_REPORTS_DIR) and os.path.isfile(public_full):
                full = public_full
            else:
                self._send_json(404, {"ok": False, "error": "report not found"})
                return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as handle:
            data = handle.read()
        self.send_response(200)
        self._cors()
        cache_control = "no-store" if name == "latest_report.json" or full.startswith(REPORTS_DIR) else "public, max-age=120, stale-while-revalidate=600"
        self.send_header("Cache-Control", cache_control)
        if cache_control == "no-store":
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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

    def _translate_titles(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(ln) or b"{}")
            titles = payload.get("titles", [])
            if not isinstance(titles, list):
                titles = []
            translations = {}
            for title in titles[:1000]:
                title = _to_text(title, 160)
                if not title or title in translations:
                    continue
                translations[title] = _translate_english_title(title)
            self._send_json(200, {"ok": True, "translations": translations})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

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
        cache_control = None
        if rel.startswith("public_reports/"):
            cache_control = "public, max-age=120, stale-while-revalidate=600"
        elif not path.endswith(".html"):
            cache_control = "public, max-age=3600"
        self._send_bytes(200, data, ctype, no_cache=path.endswith(".html"), cache_control=cache_control)

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

    def _send_bytes(self, code, data, ctype, no_cache=False, cache_control=None):
        self.send_response(code)
        self._cors()
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        elif no_cache:
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

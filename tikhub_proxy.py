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
import os, posixpath, mimetypes, base64, json, datetime, csv, gzip, hmac, html, io, re, threading, time, tempfile, zipfile, concurrent.futures, uuid, gc
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
if os.environ.get("RENDER") or os.environ.get("PORT"):
    HOST = "0.0.0.0"
ROOT = os.path.dirname(os.path.abspath(__file__))  # 托管脚本所在文件夹
DEFAULT_PAGE = "tikhub-report-frontend.html"
REPORTS_DIR = os.path.join(ROOT, "reports")   # 定时监控存盘目录
PUBLIC_REPORTS_DIR = os.path.join(ROOT, "public_reports")
DRAMA_DETAIL_CACHE_FILE = os.path.join(REPORTS_DIR, "drama_detail_cache.json")
DRAMA_EPISODE_HISTORY_DIR = os.path.join(REPORTS_DIR, "episode_history")
PUBLIC_DRAMA_EPISODE_HISTORY_DIR = os.path.join(PUBLIC_REPORTS_DIR, "episode_history")
SCHEDULE_ACCOUNTS_FILE = os.path.join(REPORTS_DIR, "schedule_accounts.json")
SCHEDULE_ACCOUNTS_SEED_FILE = os.path.join(ROOT, "schedule_accounts.seed.json")
DISCOVERED_ACCOUNTS_FILE = os.path.join(REPORTS_DIR, "discovered_accounts.json")
DISCOVERED_WORKS_FILE = os.path.join(REPORTS_DIR, "discovered_works.json")
ADMIN_CATALOG_FILE = os.path.join(REPORTS_DIR, "admin_catalog.json")
ADMIN_CATALOG_SOURCE = "admin_catalog"
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
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://paqu-tikhub-proxy.onrender.com").strip().rstrip("/")
TIKTOK_AID = os.environ.get("TIKTOK_AID", "1233").strip() or "1233"
TIKTOK_REGION = os.environ.get("TIKTOK_REGION", "US").strip() or "US"
TIKTOK_LANGUAGE = os.environ.get("TIKTOK_LANGUAGE", "en").strip() or "en"
# Optional server-side TikTok login session.  Some short-drama episodes are no
# longer exposed as anonymous public videos even though their metadata remains
# visible.  Operators can supply a legitimate TikTok Cookie header here; it is
# used only by the backend resolver/media relay and is never returned to the
# browser, report JSON, generated downloader, or logs.
TIKTOK_SESSION_COOKIE = os.environ.get("TIKTOK_SESSION_COOKIE", "").strip()
SCHEDULE_SECRET = os.environ.get("SCHEDULE_SECRET", "").strip()
ADMIN_SESSION_COOKIE_NAME = "paqu_admin_session"
ADMIN_SESSION_MAX_AGE = _env_int("ADMIN_SESSION_MAX_AGE", 86400, 300, 2592000)
ADMIN_PERMISSION_DEFINITIONS = (
    ("catalog.read", "查看后台正式数据"),
    ("catalog.review", "认领、忽略与恢复作品"),
    ("catalog.merge", "合并多账号发布的同一短剧"),
    ("dramas.manage", "新建、编辑与删除公司短剧"),
    ("dramas.publish", "上架、下架与调整前台顺序"),
    ("accounts.manage", "读取、添加与更新监控账号池"),
    ("schedule.run", "手动执行监控抓取任务"),
    ("media.private", "访问受保护的播放源与下载"),
    ("reports.export", "导出后台配置与报表"),
)


def _admin_session_token():
    if not SCHEDULE_SECRET:
        return ""
    return hmac.new(
        SCHEDULE_SECRET.encode("utf-8"),
        b"paqu-admin-auto-access-v1",
        "sha256",
    ).hexdigest()
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
SCHEDULE_ACCOUNT_WORKER_CAP = _env_int("SCHEDULE_ACCOUNT_WORKER_CAP", 2, 1, 16)
SCHEDULE_ACCOUNT_WORKERS = min(
    _env_int("SCHEDULE_ACCOUNT_WORKERS", 2, 1, 16),
    SCHEDULE_ACCOUNT_WORKER_CAP,
)
INTERNAL_SCHEDULER_ENABLED = _env_bool("INTERNAL_SCHEDULER_ENABLED", False)
INTERNAL_SCHEDULE_TIMES = os.environ.get("INTERNAL_SCHEDULE_TIMES", "00:00,12:00").strip()
INTERNAL_SCHEDULER_POLL_SECONDS = _env_int("INTERNAL_SCHEDULER_POLL_SECONDS", 15, 5, 300)
INTERNAL_SCHEDULER_TRIGGER_WINDOW_SECONDS = _env_int("INTERNAL_SCHEDULER_TRIGGER_WINDOW_SECONDS", 120, 30, 600)
INTERNAL_SCHEDULER_MAX_ATTEMPTS = _env_int("INTERNAL_SCHEDULER_MAX_ATTEMPTS", 3, 1, 10)
INTERNAL_SCHEDULER_RETRY_SECONDS = _env_int("INTERNAL_SCHEDULER_RETRY_SECONDS", 300, 30, 3600)
# TikHub's 20-RPS plan is kept below its hard ceiling so retries and manual
# lookups from the same process still have a little headroom.
TIKHUB_RPS_LIMIT = _env_int("TIKHUB_RPS_LIMIT", 18, 1, 1000)
# Drama-library calls go directly to TikTok and do not consume TikHub RPS.
# Keep those requests paced as well when multiple accounts run concurrently.
TIKTOK_RPS_LIMIT = _env_int("TIKTOK_RPS_LIMIT", 8, 1, 1000)
VIDEO_PLAY_URL_CACHE_TTL_SECONDS = _env_int("VIDEO_PLAY_URL_CACHE_TTL_SECONDS", 600, 0, 86400)
VIDEO_PLAY_NEGATIVE_CACHE_TTL_SECONDS = _env_int("VIDEO_PLAY_NEGATIVE_CACHE_TTL_SECONDS", 20, 0, 300)
VIDEO_PLAY_RESOLVE_TIMEOUT_SECONDS = _env_int("VIDEO_PLAY_RESOLVE_TIMEOUT_SECONDS", 25, 5, 120)
VIDEO_PLAY_RESOLVE_RETRIES = _env_int("VIDEO_PLAY_RESOLVE_RETRIES", 2, 1, 4)
VIDEO_MEDIA_TICKET_TTL_SECONDS = _env_int("VIDEO_MEDIA_TICKET_TTL_SECONDS", 172800, 300, 604800)
DRAMA_LINK_PAGE_SIZE = _env_int("DRAMA_LINK_PAGE_SIZE", 50, 1, 50)
DRAMA_LINK_MAX_EPISODES = _env_int("DRAMA_LINK_MAX_EPISODES", 500, 1, 5000)
DRAMA_ZIP_MAX_EPISODES = _env_int("DRAMA_ZIP_MAX_EPISODES", DRAMA_LINK_MAX_EPISODES, 1, 5000)
DRAMA_ZIP_DOWNLOAD_TIMEOUT_SECONDS = _env_int("DRAMA_ZIP_DOWNLOAD_TIMEOUT_SECONDS", 120, 10, 600)
DRAMA_ZIP_CHUNK_BYTES = _env_int("DRAMA_ZIP_CHUNK_BYTES", 262144, 32768, 1048576)
DRAMA_ZIP_MAX_VIDEO_BYTES = _env_int("DRAMA_ZIP_MAX_VIDEO_BYTES", 0, 0, 1024 * 1024 * 1024 * 20)
DRAMA_ZIP_WORKERS = _env_int("DRAMA_ZIP_WORKERS", 3, 1, 8)
LOCAL_DOWNLOAD_DIR = os.path.abspath(os.environ.get("LOCAL_DOWNLOAD_DIR", os.path.join(ROOT, "downloads")))
LOCAL_DOWNLOAD_WORKERS = _env_int("LOCAL_DOWNLOAD_WORKERS", DRAMA_ZIP_WORKERS, 1, 12)
LOCAL_DOWNLOAD_MAX_JOBS = _env_int("LOCAL_DOWNLOAD_MAX_JOBS", 20, 1, 200)
SCHEDULE_SAVE_EPISODE_HISTORY = _env_bool("SCHEDULE_SAVE_EPISODE_HISTORY", True)
SCHEDULE_EPISODE_HISTORY_MAX_DRAMAS = _env_int("SCHEDULE_EPISODE_HISTORY_MAX_DRAMAS", 0, 0, 20000)
SCHEDULE_EPISODE_HISTORY_MAX_EPISODES = _env_int("SCHEDULE_EPISODE_HISTORY_MAX_EPISODES", DRAMA_LINK_MAX_EPISODES, 0, 5000)
SCHEDULE_EPISODE_HISTORY_DELAY_MS = _env_int("SCHEDULE_EPISODE_HISTORY_DELAY_MS", SCHEDULE_DELAY_MS, 0, 60000)
SCHEDULE_EPISODE_HISTORY_FLUSH_DRAMAS = _env_int("SCHEDULE_EPISODE_HISTORY_FLUSH_DRAMAS", 10, 1, 100)
DRAMA_EPISODE_HISTORY_MAX_POINTS = _env_int("DRAMA_EPISODE_HISTORY_MAX_POINTS", 160, 20, 1000)
REPORT_RETENTION_DAYS = _env_int("REPORT_RETENTION_DAYS", 30, 1, 30)
DRAMA_EPISODE_HISTORY_MAX_AGE_DAYS = _env_int(
    "DRAMA_EPISODE_HISTORY_MAX_AGE_DAYS",
    REPORT_RETENTION_DAYS,
    1,
    REPORT_RETENTION_DAYS,
)
DRAMA_EPISODE_HISTORY_DEDUP_SECONDS = _env_int("DRAMA_EPISODE_HISTORY_DEDUP_SECONDS", 1800, 60, 86400)
DISCOVERY_DEFAULT_KEYWORDS = ",".join([
    "short drama", "shortdrama", "mini drama", "minidrama", "micro drama", "microdrama",
    "vertical drama", "verticaldrama", "vertical series", "drama series", "drama clips",
    "mobile drama", "vertical minidrama", "short drama episode", "mini drama episode",
    "billionaire drama", "ceo drama", "revenge drama", "romance drama", "werewolf drama",
    "reelshort", "dramabox", "shortmax", "goodshort", "netshort", "yuzu drama", "pinedrama", "duanju",
    "短剧", "短剧推荐", "小短剧", "微短剧", "霸总短剧", "甜宠短剧", "复仇短剧",
])
DISCOVERY_KEYWORDS = os.environ.get("DISCOVERY_KEYWORDS", DISCOVERY_DEFAULT_KEYWORDS)
DISCOVERY_MAX_KEYWORDS = _env_int("DISCOVERY_MAX_KEYWORDS", 35, 1, 50)
DISCOVERY_MAX_VIDEOS_PER_KEYWORD = _env_int("DISCOVERY_MAX_VIDEOS_PER_KEYWORD", 25, 1, 200)
DISCOVERY_MAX_CANDIDATES = _env_int("DISCOVERY_MAX_CANDIDATES", 60, 1, 500)
DISCOVERY_MIN_FOLLOWERS = _env_int("DISCOVERY_MIN_FOLLOWERS", 0, 0, 1000000000)
DISCOVERY_MIN_DRAMAS = _env_int("DISCOVERY_MIN_DRAMAS", 0, 0, 100000)
DISCOVERY_SEARCH_PAGES = _env_int("DISCOVERY_SEARCH_PAGES", 2, 1, 10)
DISCOVERY_SEARCH_TIMEOUT_SECONDS = _env_int("DISCOVERY_SEARCH_TIMEOUT_SECONDS", 6, 2, 30)
DISCOVERY_MAX_RUNTIME_SECONDS = _env_int("DISCOVERY_MAX_RUNTIME_SECONDS", 75, 15, 600)
DISCOVERY_ENRICH_RESERVE_SECONDS = _env_int("DISCOVERY_ENRICH_RESERVE_SECONDS", 35, 5, 300)
DISCOVERY_SEARCH_MODE = os.environ.get("DISCOVERY_SEARCH_MODE", "app_v3").strip().lower() or "app_v3"
DISCOVERY_ENABLE_PUBLIC_TIKTOK = _env_bool("DISCOVERY_ENABLE_PUBLIC_TIKTOK", False)
PUBLIC_DRAMA_SEARCH_CACHE_SECONDS = _env_int("PUBLIC_DRAMA_SEARCH_CACHE_SECONDS", 300, 0, 3600)
PUBLIC_DRAMA_SEARCH_CACHE_MAX_ITEMS = _env_int("PUBLIC_DRAMA_SEARCH_CACHE_MAX_ITEMS", 64, 4, 500)
PUBLIC_DRAMA_SEARCH_RATE_LIMIT = _env_int("PUBLIC_DRAMA_SEARCH_RATE_LIMIT", 20, 1, 200)
PUBLIC_DRAMA_SEARCH_RATE_WINDOW_SECONDS = _env_int("PUBLIC_DRAMA_SEARCH_RATE_WINDOW_SECONDS", 60, 10, 3600)
PUBLIC_REPORTS = os.environ.get("PUBLIC_REPORTS", "1").strip().lower() not in ("0", "false", "no", "off")
ALLOW_LOOPBACK_PRIVATE_ACCESS = _env_bool("ALLOW_LOOPBACK_PRIVATE_ACCESS", not bool(os.environ.get("RENDER")))
TRANSLATE_HOST = os.environ.get("TRANSLATE_HOST", "https://translate.googleapis.com").rstrip("/")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_ENABLED = _env_bool("SUPABASE_ENABLED", True)
SUPABASE_BATCH_SIZE = _env_int("SUPABASE_BATCH_SIZE", 50, 1, 500)
SUPABASE_SCHEDULE_ACCOUNTS = _env_bool("SUPABASE_SCHEDULE_ACCOUNTS", True)
SUPABASE_SCHEDULE_ACCOUNT_CACHE_SECONDS = _env_int("SUPABASE_SCHEDULE_ACCOUNT_CACHE_SECONDS", 60, 0, 3600)
SUPABASE_REPORT_READ = _env_bool("SUPABASE_REPORT_READ", True)
SUPABASE_REPORT_HISTORY_LIMIT = _env_int("SUPABASE_REPORT_HISTORY_LIMIT", 30, 1, 200)
SUPABASE_USER_AGENT = "paqu-tikhub-proxy/1.0"
SUPABASE_SCHEDULE_ACCOUNT_CACHE = {"expires_at": 0, "accounts": [], "error": ""}
SUPABASE_REPORT_CACHE_ITEM_CAP = _env_int("SUPABASE_REPORT_CACHE_ITEM_CAP", 1, 1, 30)
SUPABASE_REPORT_CACHE_MAX_ITEMS = min(
    _env_int("SUPABASE_REPORT_CACHE_MAX_ITEMS", 1, 1, 30),
    SUPABASE_REPORT_CACHE_ITEM_CAP,
)
SUPABASE_LATEST_CACHE_SECONDS = _env_int("SUPABASE_LATEST_CACHE_SECONDS", 120, 0, 3600)
SUPABASE_REPORT_CACHE = {"latest": None, "latest_expires_at": 0, "by_id": {}}
SUPABASE_REPORT_CACHE_LOCK = threading.Lock()
ADMIN_CATALOG_CACHE_SECONDS = _env_int("ADMIN_CATALOG_CACHE_SECONDS", 20, 0, 300)
ADMIN_CATALOG_CACHE = {"expires_at": 0, "catalog": None, "storage": ""}
ADMIN_CATALOG_LOCK = threading.RLock()


class AdminCatalogConflict(RuntimeError):
    pass


class _EvenRateLimiter:
    """Thread-safe, evenly spaced request limiter without burst traffic."""

    def __init__(self, requests_per_second, clock=None, sleeper=None):
        self.requests_per_second = max(1, int(requests_per_second))
        self._interval = 1.0 / self.requests_per_second
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self):
        with self._lock:
            now = self._clock()
            scheduled = max(now, self._next_at)
            self._next_at = scheduled + self._interval
        delay = scheduled - now
        if delay > 0:
            self._sleeper(delay)


TIKHUB_RATE_LIMITER = _EvenRateLimiter(TIKHUB_RPS_LIMIT)
TIKTOK_RATE_LIMITER = _EvenRateLimiter(TIKTOK_RPS_LIMIT)

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
    "/api/v1/tiktok/app/v3/fetch_one_video_v3",
    "/api/v1/tiktok/app/v3/fetch_one_video",
    "/api/v1/tiktok/app/v3/fetch_one_video_v2",
]
WEB_SINGLE_VIDEO_EP_CANDIDATES = [
    "/api/v1/tiktok/web/fetch_post_detail_v2",
    "/api/v1/tiktok/web/fetch_post_detail",
]
SHARE_VIDEO_EP_CANDIDATES = [
    ("/api/v1/tiktok/app/v3/fetch_one_video_by_share_url_v2", "share_url"),
    ("/api/v1/tiktok/app/v3/fetch_one_video_by_share_url", "share_url"),
    ("/api/v1/hybrid/video_data", "url"),
]
DISCOVERY_SEARCH_ENDPOINTS = {
    "app_v3": "/api/v1/tiktok/app/v3/fetch_video_search_result",
    "web": "/api/v1/tiktok/web/fetch_search_video",
}
DISCOVERY_USER_SEARCH_ENDPOINT = "/api/v1/tiktok/app/v3/fetch_user_search_result"
DISCOVERY_TOPIC_SEARCH_PHRASES = {
    "short drama", "shortdrama", "mini drama", "minidrama", "micro drama", "microdrama",
    "vertical drama", "verticaldrama", "vertical series", "drama series", "drama clips",
    "mobile drama", "vertical minidrama", "short drama episode", "mini drama episode",
    "billionaire drama", "ceo drama", "revenge drama", "romance drama", "werewolf drama",
    "reelshort", "dramabox", "shortmax", "goodshort", "netshort", "yuzu drama", "pinedrama", "duanju",
}
PLAY_KEYS = ("play_count", "playCount", "play_cnt")
LIKE_KEYS = ("digg_count", "diggCount", "like_count", "likeCount")
COMMENT_KEYS = ("comment_count", "commentCount", "comments_count", "commentsCount")
SHARE_KEYS = ("share_count", "shareCount", "shares_count", "sharesCount")
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
AUTHOR_CONTAINER_KEYS = ("author", "authorInfo", "author_info", "user", "userInfo", "user_info", "creator", "owner")
AUTHOR_UNIQUE_KEYS = ("uniqueId", "unique_id", "uniqueID", "authorUniqueId", "author_unique_id", "nicknameId", "nickname_id")
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
LAST_JOB = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
    "phase": "idle",
    "accounts_total": 0,
    "accounts_completed": 0,
    "accounts_succeeded": 0,
    "accounts_failed": 0,
    "current_account": "",
    "current_accounts": [],
    "account_workers": SCHEDULE_ACCOUNT_WORKERS,
    "tikhub_rps_limit": TIKHUB_RPS_LIMIT,
}
INTERNAL_SCHEDULER_STATE_LOCK = threading.Lock()
INTERNAL_SCHEDULER_STATE = {
    "thread_started": False,
    "active_slot": "",
    "skipped_slot": "",
    "completed_slot": "",
    "running_slot": "",
    "attempts": 0,
    "last_triggered_at": "",
    "last_completed_at": "",
    "last_error": "",
    "next_retry_at": "",
    "next_retry_timestamp": 0.0,
    "needs_report_check": True,
}
DRAMA_DETAIL_CACHE = None
DRAMA_DETAIL_CACHE_LOCK = threading.RLock()
DRAMA_EPISODE_HISTORY_LOCK = threading.Lock()
TITLE_TRANSLATION_CACHE = {}
TITLE_TRANSLATION_CACHE_LOCK = threading.Lock()
VIDEO_PLAY_URL_CACHE = {}
VIDEO_PLAY_URL_CACHE_LOCK = threading.Lock()
PUBLIC_DRAMA_SEARCH_CACHE = {}
PUBLIC_DRAMA_SEARCH_CACHE_LOCK = threading.Lock()
PUBLIC_DRAMA_SEARCH_RATE = {}
PUBLIC_DRAMA_SEARCH_RATE_LOCK = threading.Lock()
LOCAL_DOWNLOAD_JOBS = {}
LOCAL_DOWNLOAD_JOBS_LOCK = threading.Lock()
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


def _send_tikhub_get(path, params, label, timeout=90, retries=None):
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
    attempts = SCHEDULE_RETRIES if retries is None else max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            TIKHUB_RATE_LIMITER.wait()
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                return json.loads(text) if text else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            last_error = TikHubError("%s failed with HTTP %s: %s" % (label, exc.code, body), exc.code)
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(max(0.5, SCHEDULE_DELAY_MS / 1000.0) * attempt)
                continue
            raise last_error
        except Exception as exc:
            last_error = TikHubError("%s request failed: %s" % (label, exc))
            if attempt < attempts:
                time.sleep(max(0.5, SCHEDULE_DELAY_MS / 1000.0) * attempt)
                continue
            raise last_error
    raise last_error or TikHubError("%s request failed" % label)


def _send_tiktok_get(path, params, label, referer_uid=None, timeout=90, retries=None):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = TIKTOK_HOST + path + ("?" + query if query else "")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": TIKTOK_HOST + ("/@" + referer_uid if referer_uid else "/"),
        "User-Agent": DEFAULT_UA,
    }
    last_error = None
    attempts = SCHEDULE_RETRIES if retries is None else max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            TIKTOK_RATE_LIMITER.wait()
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                return json.loads(text) if text else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            last_error = TikHubError("%s failed with HTTP %s: %s" % (label, exc.code, body), exc.code)
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(max(0.5, SCHEDULE_DELAY_MS / 1000.0) * attempt)
                continue
            raise last_error
        except Exception as exc:
            last_error = TikHubError("%s request failed: %s" % (label, exc))
            if attempt < attempts:
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
    if isinstance(value, str):
        text = html.unescape(value).strip()
        if text.startswith("//"):
            text = "https:" + text
        return text if text.startswith(("http://", "https://")) else ""
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "UrlList", "URLList", "urls"):
            urls = value.get(key)
            if isinstance(urls, list):
                candidates = []
                for url in urls:
                    text = _first_addr_url(url)
                    if text:
                        candidates.append(text)
                if candidates:
                    # TikHub's Web V2 response can list Akamai `webapp-prime`
                    # hosts first even though those hosts reject requests from
                    # cloud servers.  Its `www.tiktok.com` play endpoint is the
                    # durable public entry point and redirects to a working CDN.
                    candidates.sort(key=lambda item: (
                        0 if urllib.parse.urlparse(item).hostname in ("tiktok.com", "www.tiktok.com") else 1,
                    ))
                    return candidates[0]
            else:
                text = _first_addr_url(urls)
                if text:
                    return text
        for key in (
            "url", "Url", "URL", "uri", "play_url", "playUrl", "download_url", "downloadUrl",
            "original_video_url", "originalVideoUrl", "watermark_free_url", "watermarkFreeUrl",
        ):
            text = _first_addr_url(value.get(key))
            if text:
                return text
    elif isinstance(value, list):
        for item in value:
            text = _first_addr_url(item)
            if text:
                return text
    return ""


def _video_play_url_from_item(item):
    if not isinstance(item, dict):
        return ""
    video = item.get("video")
    containers = []
    if isinstance(video, dict):
        for key in (
            "PlayAddrStruct", "playAddrStruct",
            "play_addr_h264", "playAddrH264", "play_addr",
            "playAddr", "play_addr_bytevc1", "playAddrBytevc1",
            "play_addr_265", "playAddr265", "play_addr_lowbr", "playAddrLowbr",
            "play_url", "playUrl",
        ):
            containers.append(video.get(key))
        bit_rates = (
            video.get("bit_rate") or video.get("bitRate") or video.get("bit_rates")
            or video.get("bitrateInfo") or video.get("BitrateInfo") or []
        )
        if isinstance(bit_rates, list):
            for item_rate in bit_rates:
                if isinstance(item_rate, dict):
                    for key in (
                        "play_addr", "playAddr", "play_addr_h264", "playAddrH264",
                        "play_url", "playUrl", "PlayAddr", "PlayAddrStruct", "playAddrStruct",
                    ):
                        containers.append(item_rate.get(key))
        for key in (
            "download_addr", "downloadAddr", "download_url", "downloadUrl",
            "DownloadAddr", "DownloadAddrStruct", "downloadAddrStruct",
        ):
            containers.append(video.get(key))
    # Some TikHub response versions expose the media address on the item/data
    # object instead of nesting it under `video`.
    for key in (
        "play_addr_h264", "playAddrH264", "play_addr", "playAddr",
        "play_url", "playUrl", "PlayAddrStruct", "playAddrStruct",
        "download_addr", "downloadAddr", "DownloadAddr", "DownloadAddrStruct",
        "download_url", "downloadUrl", "url_list", "urlList",
        "original_video_url", "originalVideoUrl", "watermark_free_url", "watermarkFreeUrl",
    ):
        containers.append(item.get(key))
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
            "item", "itemStruct", "aweme", "video_detail", "videoDetail", "data",
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
        for key, value in obj.items():
            # A TikTok work's background music also has a `playUrl`.  It must
            # never be accepted as the work's video source.
            if str(key).lower() in ("music", "music_info", "musicinfo"):
                continue
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
    with TITLE_TRANSLATION_CACHE_LOCK:
        if key in TITLE_TRANSLATION_CACHE:
            return TITLE_TRANSLATION_CACHE[key]
    if not SCHEDULE_TRANSLATE_TITLES:
        with TITLE_TRANSLATION_CACHE_LOCK:
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
        with TITLE_TRANSLATION_CACHE_LOCK:
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
    with DRAMA_DETAIL_CACHE_LOCK:
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
    with DRAMA_DETAIL_CACHE_LOCK:
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


def _release_drama_detail_cache():
    """Release the large in-memory detail cache after a scheduled scrape."""
    global DRAMA_DETAIL_CACHE
    with DRAMA_DETAIL_CACHE_LOCK:
        DRAMA_DETAIL_CACHE = None


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


def _video_play_cache_store(video_id, source, now=None):
    if isinstance(source, str):
        source = {"url": source}
    source = dict(source or {})
    url = source.get("url") or ""
    if not VIDEO_PLAY_URL_CACHE_TTL_SECONDS and (url or not VIDEO_PLAY_NEGATIVE_CACHE_TTL_SECONDS):
        return
    with VIDEO_PLAY_URL_CACHE_LOCK:
        VIDEO_PLAY_URL_CACHE[video_id] = {
            "url": url,
            "tt_chain_token": source.get("tt_chain_token") or "",
            "cookie": source.get("cookie") or "",
            "endpoint": source.get("endpoint") or "",
            "error_code": source.get("error_code") or "",
            "error": source.get("error") or "",
            "ts": now or time.time(),
        }


def _video_play_cache_remove(video_id):
    with VIDEO_PLAY_URL_CACHE_LOCK:
        VIDEO_PLAY_URL_CACHE.pop(_clean_drama_id(video_id), None)


def _get_video_play_source(video_id, started=None, uid=""):
    clean_id = _clean_drama_id(video_id)
    if not clean_id or (started is not None and _runtime_exceeded(started)):
        return {}
    now = time.time()
    with VIDEO_PLAY_URL_CACHE_LOCK:
        cached = VIDEO_PLAY_URL_CACHE.get(clean_id)
    if cached:
        cached_url = cached.get("url", "")
        ttl = VIDEO_PLAY_URL_CACHE_TTL_SECONDS if cached_url else VIDEO_PLAY_NEGATIVE_CACHE_TTL_SECONDS
        if ttl and now - cached.get("ts", 0) < ttl:
            return dict(cached)
    failures = []
    login_required = False
    removed_or_unavailable = False

    # TikHub's Web V2 resolver is the most stable current fallback for older or
    # region-limited TikTok posts.  Its URLs require the returned
    # tt_chain_token cookie, which is retained in the source object and used by
    # our protected media relay/downloaders below.
    for endpoint in WEB_SINGLE_VIDEO_EP_CANDIDATES:
        if started is not None and _runtime_exceeded(started):
            return {}
        try:
            data = _send_tikhub_get(
                endpoint,
                {"itemId": clean_id, "region": TIKTOK_REGION},
                "TikHub web single video endpoint",
                timeout=VIDEO_PLAY_RESOLVE_TIMEOUT_SECONDS,
                retries=VIDEO_PLAY_RESOLVE_RETRIES,
            )
        except TikHubError as exc:
            failures.append("%s: %s" % (endpoint.rsplit("/", 1)[-1], str(exc)[:180]))
            continue
        source = _video_play_source_from_payload(data, endpoint)
        if source.get("url"):
            _video_play_cache_store(clean_id, source, now)
            return source
        login_required = login_required or _payload_is_short_drama_login_gated(data)
        removed_or_unavailable = removed_or_unavailable or _payload_reports_video_removed(data)
        failures.append("%s: no media address" % endpoint.rsplit("/", 1)[-1])

    for endpoint in SINGLE_VIDEO_EP_CANDIDATES:
        if started is not None and _runtime_exceeded(started):
            return {}
        params = {"aweme_id": clean_id}
        if endpoint.endswith("_v3"):
            params["region"] = TIKTOK_REGION
        try:
            data = _send_tikhub_get(
                endpoint,
                params,
                "TikHub single video endpoint",
                timeout=VIDEO_PLAY_RESOLVE_TIMEOUT_SECONDS,
                retries=VIDEO_PLAY_RESOLVE_RETRIES,
            )
        except TikHubError as exc:
            failures.append("%s: %s" % (endpoint.rsplit("/", 1)[-1], str(exc)[:180]))
            continue
        source = _video_play_source_from_payload(data, endpoint)
        if source.get("url"):
            _video_play_cache_store(clean_id, source, now)
            return source
        login_required = login_required or _payload_is_short_drama_login_gated(data)
        removed_or_unavailable = removed_or_unavailable or _payload_reports_video_removed(data)
        failures.append("%s: no media address" % endpoint.rsplit("/", 1)[-1])

    # The share-link resolver follows a separate TikHub parsing path and is an
    # important fallback for short-drama episodes that the ID endpoint cannot
    # currently resolve (region/response-version differences are common).
    share_url = _build_tiktok_video_url(uid, clean_id)
    if share_url:
        for endpoint, param_name in SHARE_VIDEO_EP_CANDIDATES:
            if started is not None and _runtime_exceeded(started):
                return {}
            try:
                data = _send_tikhub_get(
                    endpoint,
                    {param_name: share_url},
                    "TikHub share link video endpoint",
                    timeout=VIDEO_PLAY_RESOLVE_TIMEOUT_SECONDS,
                    retries=VIDEO_PLAY_RESOLVE_RETRIES,
                )
            except TikHubError as exc:
                failures.append("%s: %s" % (endpoint.rsplit("/", 1)[-1], str(exc)[:180]))
                continue
            source = _video_play_source_from_payload(data, endpoint)
            if source.get("url"):
                _video_play_cache_store(clean_id, source, now)
                return source
            login_required = login_required or _payload_is_short_drama_login_gated(data)
            removed_or_unavailable = removed_or_unavailable or _payload_reports_video_removed(data)
            failures.append("%s: no media address" % endpoint.rsplit("/", 1)[-1])

    # Final fallback: inspect the real TikTok work page.  This supports public
    # response-shape changes and, when TIKTOK_SESSION_COOKIE is configured,
    # content that TikTok intentionally exposes only to an authenticated user.
    work_source = _fetch_tiktok_work_page_source(uid, clean_id)
    if work_source.get("url"):
        _video_play_cache_store(clean_id, work_source, now)
        return work_source
    login_required = login_required or work_source.get("error_code") == "tiktok_login_required"
    removed_or_unavailable = (
        removed_or_unavailable
        or work_source.get("error_code") == "video_removed_or_unavailable"
    )
    if login_required:
        failure = {
            "error_code": "tiktok_login_required",
            "error": (
                "该短剧已被 TikTok 设为登录后观看；请在后端配置有效的 "
                "TIKTOK_SESSION_COOKIE 后重试。"
            ),
            "endpoint": work_source.get("endpoint") or "tiktok-work-page",
        }
    elif removed_or_unavailable:
        failure = {
            "error_code": "video_removed_or_unavailable",
            "error": "TikTok/TikHub 当前将该作品标记为已删除或不可播放。",
            "endpoint": work_source.get("endpoint") or "tikhub",
        }
    else:
        failure = {
            "error_code": "play_source_unavailable",
            "error": "当前上游没有返回可用播放地址，请稍后重试。",
            "endpoint": work_source.get("endpoint") or "resolver-chain",
        }
    _video_play_cache_store(clean_id, failure, now)
    print("[video-play] unresolved video_id=%s account=%s attempts=%s" % (
        clean_id,
        (uid or "-").strip().lstrip("@"),
        " | ".join(failures[-5:]) or "no resolver attempted",
    ), flush=True)
    return failure


def _get_video_play_url(video_id, started=None, uid=""):
    return _get_video_play_source(video_id, started, uid).get("url", "")


def _video_media_ticket_signature(uid, video_id, expires):
    if not SCHEDULE_SECRET:
        return ""
    payload = "%s\n%s\n%s" % (
        _to_text(uid, 80).strip().lstrip("@"),
        _clean_drama_id(video_id),
        int(expires),
    )
    return hmac.new(
        SCHEDULE_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        "sha256",
    ).hexdigest()


def _video_media_ticket_url(uid, video_id, expires=None, origin=""):
    clean_id = _clean_drama_id(video_id)
    if not clean_id or not SCHEDULE_SECRET:
        return ""
    expires = int(expires or (time.time() + VIDEO_MEDIA_TICKET_TTL_SECONDS))
    params = {
        "uid": _to_text(uid, 80).strip().lstrip("@"),
        "video_id": clean_id,
        "expires": str(expires),
        "sig": _video_media_ticket_signature(uid, clean_id, expires),
    }
    path = "/drama-media?" + urllib.parse.urlencode(params)
    return (origin or "").rstrip("/") + path


def _video_media_ticket_valid(uid, video_id, expires, signature, now=None):
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        return False
    now = int(now or time.time())
    if expires < now or expires > now + VIDEO_MEDIA_TICKET_TTL_SECONDS + 300:
        return False
    expected = _video_media_ticket_signature(uid, video_id, expires)
    return bool(expected and signature and hmac.compare_digest(expected, str(signature)))


def _video_play_cookie_from_tree(data):
    token = _deep_find(data, ("tt_chain_token", "ttChainToken", "chain_token", "chainToken"))
    token = _to_text(token, 4096).strip()
    if not token:
        return ""
    # TikHub returns the cookie value rather than an entire Cookie header.  Do
    # not allow response data to inject additional cookie fields or headers.
    token = token.split(";", 1)[0].replace("\r", "").replace("\n", "").strip()
    return token


def _video_play_source_from_payload(data, endpoint=""):
    url = _video_play_url_from_tree(data)
    if not url:
        return {}
    return {
        "url": url,
        "tt_chain_token": _video_play_cookie_from_tree(data),
        "endpoint": endpoint or "",
    }


def _payload_reports_video_removed(data):
    status_code = _to_int(_deep_find(data, ("status_code", "statusCode")))
    status_message = _to_text(
        _deep_find(data, ("status_msg", "statusMsg", "message", "error_message")),
        300,
    ).lower()
    return status_code == 2053 or "video has been removed" in status_message


def _payload_is_short_drama_login_gated(data):
    if not isinstance(data, (dict, list)) or _video_play_url_from_tree(data):
        return False
    try:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return False
    lowered = text.lower()
    has_drama = any(marker in lowered for marker in (
        '"dramainfo"', '"drama_info"', '"dramavideodata"',
        "short-drama-login-gated-surface",
    ))
    is_preview = '"ispreview":true' in lowered or '"previewtype":4' in lowered
    return has_drama and (is_preview or '"playaddr":""' in lowered or '"play_addr":{}' in lowered)


def _clean_cookie_header(value):
    """Return a CR/LF-safe Cookie header without logging or exposing it."""
    raw = _to_text(value, 16384).replace("\r", "").replace("\n", "").strip()
    parts = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, cookie_value = part.split("=", 1)
        name = re.sub(r"[^A-Za-z0-9_!#$%&'*+.^`|~-]", "", name.strip())
        if name:
            parts.append(name + "=" + cookie_value.strip())
    return "; ".join(parts)


def _merge_cookie_headers(*values):
    merged = {}
    order = []
    for value in values:
        for part in _clean_cookie_header(value).split("; "):
            if not part or "=" not in part:
                continue
            name, cookie_value = part.split("=", 1)
            if name not in merged:
                order.append(name)
            merged[name] = cookie_value
    return "; ".join(name + "=" + merged[name] for name in order)


def _response_cookie_header(headers):
    values = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else []
    cookies = []
    for value in values or []:
        jar = SimpleCookie()
        try:
            jar.load(value)
        except Exception:
            continue
        for name, morsel in jar.items():
            cookies.append(name + "=" + morsel.value)
    return _merge_cookie_headers(*cookies)


def _fetch_tiktok_work_page_source(uid, video_id):
    account = _to_text(uid, 80).strip().lstrip("@")
    clean_id = _clean_drama_id(video_id)
    if not account or not clean_id:
        return {}
    url = _build_tiktok_video_url(account, clean_id)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": TIKTOK_HOST + "/@" + account,
        "User-Agent": DEFAULT_UA,
    }
    session_cookie = _clean_cookie_header(TIKTOK_SESSION_COOKIE)
    if session_cookie:
        headers["Cookie"] = session_cookie
    try:
        TIKTOK_RATE_LIMITER.wait()
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=VIDEO_PLAY_RESOLVE_TIMEOUT_SECONDS) as resp:
            page = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
            response_cookie = _response_cookie_header(resp.headers)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {
                "error_code": "tiktok_login_required",
                "error": "TikTok requires an authenticated session for this work.",
                "endpoint": "tiktok-work-page",
            }
        return {}
    except Exception:
        return {}

    cookie = _merge_cookie_headers(session_cookie, response_cookie)
    for payload in _script_json_from_html(page):
        source = _video_play_source_from_payload(payload, "tiktok-work-page")
        if source.get("url"):
            source["cookie"] = cookie
            return source
    lowered = page.lower()
    gated = "short-drama-login-gated-surface" in lowered
    if not gated:
        gated = any(_payload_is_short_drama_login_gated(payload) for payload in _script_json_from_html(page))
    if gated:
        return {
            "error_code": "tiktok_login_required",
            "error": "TikTok requires an authenticated session for this short drama.",
            "endpoint": "tiktok-work-page",
        }
    if "video has been removed" in lowered or "video currently unavailable" in lowered:
        return {
            "error_code": "video_removed_or_unavailable",
            "error": "TikTok reports that this work is unavailable.",
            "endpoint": "tiktok-work-page",
        }
    return {}


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
        for key in ("max_cursor", "cursor", "maxCursor", "next_cursor", "nextCursor", "offset", "next_offset", "nextOffset"):
            if item.get(key) not in (None, ""):
                cursor = item[key]
    return has_more, cursor


def _normalize_schedule_account(value):
    raw = urllib.parse.unquote(str(value or "")).strip().strip("\"'")
    if not raw:
        return ""
    candidate = raw
    if "://" in raw or re.match(r"^(?:www\.|m\.)?tiktok\.com/", raw, flags=re.I):
        try:
            parsed = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw)
        except Exception:
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
        if host != "tiktok.com" and not host.endswith(".tiktok.com"):
            return ""
        match = re.search(r"(?:^|/)@([^/?#]+)", parsed.path or "", flags=re.I)
        if not match:
            return ""
        candidate = urllib.parse.unquote(match.group(1))
    candidate = candidate.strip().lstrip("@").rstrip("/")
    candidate = candidate.split("?", 1)[0].split("#", 1)[0].strip()
    return candidate if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", candidate) else ""


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
        uid = _normalize_schedule_account(item)
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


def _schedule_account_seed():
    try:
        with open(SCHEDULE_ACCOUNTS_SEED_FILE, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except Exception:
        payload = None
    accounts = payload.get("accounts", payload) if isinstance(payload, dict) else payload
    if isinstance(accounts, list):
        accounts = _parse_accounts("\n".join(str(item) for item in accounts))
    else:
        accounts = _parse_accounts(accounts)
    return accounts


def _merge_accounts(*groups):
    merged, seen = [], set()
    for group in groups:
        for uid in _parse_accounts("\n".join(str(item) for item in (group or []))):
            key = uid.lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(uid)
    return merged


def _supabase_schedule_accounts(force=False):
    if not (SUPABASE_ENABLED and SUPABASE_SCHEDULE_ACCOUNTS and _supabase_configured()):
        return []
    now = time.time()
    if (
        not force
        and SUPABASE_SCHEDULE_ACCOUNT_CACHE_SECONDS
        and SUPABASE_SCHEDULE_ACCOUNT_CACHE.get("expires_at", 0) > now
    ):
        return list(SUPABASE_SCHEDULE_ACCOUNT_CACHE.get("accounts") or [])
    try:
        response = _supabase_request(
            "GET",
            "/accounts?select=account&account=not.is.null&order=account.asc&limit=10000",
            timeout=20,
        )
        accounts = []
        if isinstance(response, list):
            accounts = [item.get("account") for item in response if isinstance(item, dict)]
        accounts = _parse_accounts("\n".join(str(item) for item in accounts if item))
        SUPABASE_SCHEDULE_ACCOUNT_CACHE.update({
            "accounts": accounts,
            "error": "",
            "expires_at": now + SUPABASE_SCHEDULE_ACCOUNT_CACHE_SECONDS,
        })
        return accounts
    except Exception as exc:
        SUPABASE_SCHEDULE_ACCOUNT_CACHE.update({
            "error": str(exc),
            "expires_at": now + min(SUPABASE_SCHEDULE_ACCOUNT_CACHE_SECONDS or 60, 60),
        })
        return []


def _invalidate_supabase_schedule_account_cache():
    SUPABASE_SCHEDULE_ACCOUNT_CACHE.update({"expires_at": 0})


def _store_schedule_accounts_in_supabase(accounts):
    status = {
        "enabled": bool(SUPABASE_ENABLED and SUPABASE_SCHEDULE_ACCOUNTS),
        "configured": _supabase_configured(),
        "ok": False,
    }
    accounts = _parse_accounts("\n".join(str(item) for item in (accounts or [])))
    if not status["enabled"]:
        status.update({"ok": True, "skipped": "SUPABASE_SCHEDULE_ACCOUNTS is off"})
        return status
    if not status["configured"]:
        status["error"] = "SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured"
        return status
    if not accounts:
        status.update({"ok": True, "accounts": 0})
        return status
    now_iso = datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    rows = [{
        "account": uid,
        "profile_url": "https://www.tiktok.com/@%s" % uid,
        "last_seen_at": now_iso,
    } for uid in accounts]
    try:
        written = _supabase_upsert("accounts", rows, "account")
        _invalidate_supabase_schedule_account_cache()
        status.update({"ok": True, "accounts": written})
    except Exception as exc:
        status["error"] = str(exc)
    return status


def _configured_schedule_accounts():
    pool = _schedule_account_pool()
    env_accounts = _parse_accounts(SCHEDULE_ACCOUNTS)
    seed_accounts = _schedule_account_seed()
    supabase_accounts = _supabase_schedule_accounts()
    accounts = _merge_accounts(env_accounts, seed_accounts, pool["accounts"], supabase_accounts)
    sources = []
    if env_accounts:
        sources.append("SCHEDULE_ACCOUNTS")
    if seed_accounts:
        sources.append("seed")
    if pool["accounts"]:
        sources.append("backend_pool")
    if supabase_accounts:
        sources.append("supabase")
    return accounts, "+".join(sources)


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


def _append_schedule_accounts(accounts):
    current, _source = _configured_schedule_accounts()
    merged = list(current)
    seen = {item.lower() for item in merged}
    added = []
    for uid in _parse_accounts("\n".join(str(item) for item in (accounts or []))):
        key = uid.lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(uid)
            added.append(uid)
    saved = _write_schedule_account_pool(merged)
    supabase = _store_schedule_accounts_in_supabase(merged)
    return {"saved": saved, "added": added, "supabase": supabase}


def _parse_discovery_keywords(text):
    values = []
    raw = text if text not in (None, "") else DISCOVERY_KEYWORDS
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[\n,;，；]+", str(raw or ""))
    seen = set()
    for item in parts:
        text_item = re.sub(r"\s+", " ", str(item or "")).strip()
        key = text_item.lower()
        if text_item and key not in seen:
            seen.add(key)
            values.append(text_item)
        if len(values) >= DISCOVERY_MAX_KEYWORDS:
            break
    return values


def _read_discovered_accounts():
    try:
        with open(DISCOVERED_ACCOUNTS_FILE, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {
        "ok": True,
        "generated_at": "",
        "keywords": [],
        "accounts": [],
        "count": 0,
        "errors": [],
        "runtime_file": "reports/discovered_accounts.json",
    }


def _write_discovered_accounts(payload):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    payload["runtime_file"] = "reports/discovered_accounts.json"
    tmp = DISCOVERED_ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, DISCOVERED_ACCOUNTS_FILE)
    return payload


def _read_discovered_works():
    try:
        with open(DISCOVERED_WORKS_FILE, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {
        "ok": True,
        "mode": "works",
        "generated_at": "",
        "queries": [],
        "works": [],
        "count": 0,
        "errors": [],
        "runtime_file": "reports/discovered_works.json",
    }


def _write_discovered_works(payload):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    payload["runtime_file"] = "reports/discovered_works.json"
    tmp = DISCOVERED_WORKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, DISCOVERED_WORKS_FILE)
    return payload


def _discovery_error(layer, source, message, status=None, hint=""):
    item = {"layer": layer, "source": source, "message": str(message)}
    if status:
        item["status_code"] = status
    if hint:
        item["hint"] = hint
    return item


def _search_params(keyword, count, cursor):
    if DISCOVERY_SEARCH_MODE == "web":
        return {
            "keyword": keyword,
            "count": str(count),
            "offset": str(cursor or "0"),
        }
    return {
        "keyword": keyword,
        "offset": str(cursor or "0"),
        "count": str(count),
        "sort_type": "0",
        "publish_time": "0",
        "region": TIKTOK_REGION,
    }


def _discovery_runtime_exceeded(started):
    return bool(started) and time.time() - started > DISCOVERY_MAX_RUNTIME_SECONDS


def _discovery_remaining_seconds(started):
    if not started:
        return DISCOVERY_MAX_RUNTIME_SECONDS
    return max(0, DISCOVERY_MAX_RUNTIME_SECONDS - (time.time() - started))


def _fetch_discovery_search_page(keyword, count, cursor, started=None):
    if _discovery_runtime_exceeded(started):
        raise TikHubError("discovery runtime limit reached")
    endpoint = DISCOVERY_SEARCH_ENDPOINTS.get(DISCOVERY_SEARCH_MODE) or DISCOVERY_SEARCH_ENDPOINTS["app_v3"]
    data = _send_tikhub_get(
        endpoint,
        _search_params(keyword, count, cursor),
        "TikHub documented video search endpoint",
        timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS,
        retries=1,
    )
    batch = _video_batch_from_search_payload(data, count)
    if batch:
        return data, batch, endpoint
    raise TikHubError("TikHub documented video search endpoint returned no parseable video items")


def _send_tiktok_public_page(url, referer=None):
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer or (TIKTOK_HOST + "/"),
        "User-Agent": DEFAULT_UA,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 6) as resp:
        return resp.read().decode("utf-8", "replace")


def _send_tiktok_public_json(path, params, referer=None):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = TIKTOK_HOST + path + ("?" + query if query else "")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer or (TIKTOK_HOST + "/search?" + urllib.parse.urlencode({"q": params.get("keyword", "")})),
        "User-Agent": DEFAULT_UA,
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 6) as resp:
            text = resp.read().decode("utf-8", "replace")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:240]
        raise TikHubError("public TikTok web search failed with HTTP %s: %s" % (exc.code, body), exc.code)
    except Exception as exc:
        raise TikHubError("public TikTok web search request failed: %s" % exc)


def _tiktok_web_search_params(keyword, count, cursor):
    base = {
        "aid": "1988",
        "app_name": "tiktok_web",
        "app_language": TIKTOK_LANGUAGE,
        "browser_language": "zh-CN",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": DEFAULT_UA,
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "count": str(count),
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "search",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "keyword": keyword,
        "language": TIKTOK_LANGUAGE,
        "offset": str(cursor),
        "priority_region": "",
        "region": TIKTOK_REGION,
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "Asia/Shanghai",
    }
    with_cursor = dict(base)
    with_cursor["cursor"] = str(cursor)
    return [base, with_cursor]


def _collect_video_items(obj, depth=0):
    if depth > 9 or obj is None:
        return []
    if isinstance(obj, list):
        videos = []
        for item in obj:
            if _looks_like_video(item):
                videos.append(item)
            else:
                videos.extend(_collect_video_items(item, depth + 1))
        return videos
    if isinstance(obj, dict):
        for key in ("item", "aweme", "aweme_info", "awemeInfo", "item_info", "itemInfo"):
            item = obj.get(key)
            if _looks_like_video(item):
                return [item]
        videos = []
        for value in obj.values():
            videos.extend(_collect_video_items(value, depth + 1))
        return videos
    return []


def _dedupe_videos(videos, limit):
    out, seen = [], set()
    for video in videos:
        video_id = _get_video_id(video)
        key = video_id or json.dumps(video, ensure_ascii=False, sort_keys=True)[:200]
        if key in seen:
            continue
        seen.add(key)
        out.append(video)
        if len(out) >= limit:
            break
    return out


def _video_batch_from_search_payload(data, count):
    batch = _find_video_list(data)
    if not batch and isinstance(data, dict):
        item_module = data.get("ItemModule") or data.get("itemModule")
        if isinstance(item_module, dict):
            batch = [item for item in item_module.values() if _looks_like_video(item)]
    if not batch:
        batch = _collect_video_items(data)
    return _dedupe_videos(batch, count)


def _fetch_public_tiktok_search_api(keyword, count, cursor, started=None):
    if _discovery_runtime_exceeded(started):
        raise TikHubError("discovery runtime limit reached")
    last_error = None
    endpoints = ("/api/search/general/full/", "/api/search/item/full/")
    for endpoint in endpoints:
        for params in _tiktok_web_search_params(keyword, count, cursor):
            try:
                data = _send_tiktok_public_json(endpoint, params)
                batch = _video_batch_from_search_payload(data, count)
                if batch:
                    return data, batch, "public_tiktok_web_search:" + endpoint
                last_error = TikHubError("public TikTok web search returned no videos from %s" % endpoint)
            except TikHubError as exc:
                last_error = exc
                if exc.status in (401, 403):
                    break
                continue
    raise last_error or TikHubError("public TikTok web search returned no videos")


def _script_json_from_html(page):
    payloads = []
    for script_id in ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"):
        pattern = r'<script[^>]+id=["\']%s["\'][^>]*>(.*?)</script>' % re.escape(script_id)
        for match in re.finditer(pattern, page or "", flags=re.S | re.I):
            text = html.unescape(match.group(1) or "").strip()
            if not text:
                continue
            try:
                payloads.append(json.loads(text))
            except Exception:
                continue
    for match in re.finditer(r'window\[[\'"]SIGI_STATE[\'"]\]\s*=\s*({.*?});?\s*</script>', page or "", flags=re.S):
        try:
            payloads.append(json.loads(html.unescape(match.group(1)).strip()))
        except Exception:
            continue
    return payloads


def _fetch_public_tiktok_search_page(keyword, count, started=None):
    if _discovery_runtime_exceeded(started):
        raise TikHubError("discovery runtime limit reached")
    query = urllib.parse.urlencode({"q": keyword})
    url = TIKTOK_HOST + "/search?" + query
    try:
        page = _send_tiktok_public_page(url)
    except Exception as exc:
        raise TikHubError("public TikTok search page failed: %s" % exc)
    for payload in _script_json_from_html(page):
        batch = _video_batch_from_search_payload(payload, count)
        if batch:
            return {"data": batch[:count], "has_more": False}, batch[:count], "public_tiktok_search_page"
    raise TikHubError("public TikTok search page returned no visible video data")


def _author_container(video):
    if not isinstance(video, dict):
        return {}
    for key in AUTHOR_CONTAINER_KEYS:
        value = video.get(key)
        if isinstance(value, dict):
            return value
    for key in ("aweme_detail", "awemeDetail", "item", "aweme", "data"):
        nested = video.get(key)
        if isinstance(nested, dict):
            found = _author_container(nested)
            if found:
                return found
    return {}


def _extract_account_from_url(obj):
    text = json.dumps(obj, ensure_ascii=False) if isinstance(obj, (dict, list)) else _to_text(obj)
    match = re.search(r"tiktok\.com/@([A-Za-z0-9._-]+)", text)
    return match.group(1) if match else ""


def _author_from_video(video):
    author = _author_container(video)
    if not author and isinstance(video, dict) and _deep_find(video, AUTHOR_UNIQUE_KEYS) is not None:
        author = video
    account = _deep_find(author, AUTHOR_UNIQUE_KEYS) if author else None
    account = _to_text(account, 80).lstrip("@")
    if not account:
        account = _extract_account_from_url(video).lstrip("@")
    if not account:
        return None
    nickname = _to_text(_deep_find(author, NICK_KEYS), 120) or account
    secuid = _to_text(_deep_find(author, SECUID_KEYS), 160)
    avatar = _first_profile_image(author)
    stats = (video.get("authorStats") or video.get("author_stats")) if isinstance(video, dict) else {}
    if not isinstance(stats, dict):
        stats = author
    return {
        "account": account,
        "nickname": nickname,
        "secuid": secuid,
        "avatar": avatar,
        "followers_hint": _to_int(_deep_find(stats, FOLLOWER_KEYS)),
        "hearts_hint": _to_int(_deep_find(stats, HEART_KEYS)),
        "video_count_hint": _to_int(_deep_find(stats, VCOUNT_KEYS)),
    }


def _normalized_discovery_phrase(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _looks_like_account_query(text):
    value = _to_text(text, 180).strip()
    if not value or re.search(r"[\n,;锛岋紱]", value):
        return False
    lower = value.lower()
    if "tiktok.com/@" in lower or value.startswith("@"):
        return True
    phrase = _normalized_discovery_phrase(value)
    if not phrase or phrase in DISCOVERY_TOPIC_SEARCH_PHRASES:
        return False
    if re.fullmatch(r"[A-Za-z0-9._]{2,80}", value):
        return True
    words = phrase.split()
    return 1 < len(words) <= 4 and len(phrase) <= 80


def _account_lookup_candidates(keyword):
    value = _to_text(keyword, 220).strip()
    if not value:
        return []
    candidates = []

    def add(uid):
        uid = str(uid or "").strip().strip("/").split("?", 1)[0].lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9._]{2,80}", uid) and uid.lower() not in {x.lower() for x in candidates}:
            candidates.append(uid)

    for match in re.finditer(r"(?:https?://)?(?:www\.|m\.)?tiktok\.com/@([A-Za-z0-9._-]+)", value, flags=re.I):
        add(match.group(1))
    if value.startswith("@"):
        add(value)
    if re.fullmatch(r"[A-Za-z0-9._]{2,80}", value):
        add(value)
    words = re.findall(r"[A-Za-z0-9]+", value)
    if words:
        lower_words = [part.lower() for part in words]
        add("".join(lower_words))
        add("_".join(lower_words))
        add(".".join(lower_words))
    return candidates[:8]


def _user_container_from_search_item(item):
    if not isinstance(item, dict):
        return None
    for key in ("user_info", "userInfo", "user", "author", "authorInfo", "author_info"):
        value = item.get(key)
        if isinstance(value, dict) and (
            _deep_find(value, AUTHOR_UNIQUE_KEYS) is not None or _deep_find(value, SECUID_KEYS) is not None
        ):
            return value
    if any(key in item for key in AUTHOR_UNIQUE_KEYS) or any(key in item for key in SECUID_KEYS):
        return item
    return None


def _user_from_search_item(item):
    user = _user_container_from_search_item(item)
    if not user:
        return None
    account = _to_text(_deep_find(user, AUTHOR_UNIQUE_KEYS), 80).lstrip("@")
    if not account:
        account = _extract_account_from_url(item).lstrip("@")
    if not account:
        return None
    stats = {}
    for source in (item, user):
        if isinstance(source, dict):
            for key in ("stats", "statistics", "userStats", "user_stats", "authorStats", "author_stats"):
                value = source.get(key)
                if isinstance(value, dict):
                    stats = value
                    break
        if stats:
            break
    if not stats:
        stats = item if isinstance(item, dict) else user
    return {
        "account": account,
        "nickname": _to_text(_deep_find(user, NICK_KEYS), 120) or account,
        "secuid": _to_text(_deep_find(user, SECUID_KEYS), 180),
        "avatar": _first_profile_image(item) or _first_profile_image(user),
        "followers_hint": _to_int(_deep_find(stats, FOLLOWER_KEYS)) or _to_int(_deep_find(item, FOLLOWER_KEYS)),
        "hearts_hint": _to_int(_deep_find(stats, HEART_KEYS)) or _to_int(_deep_find(item, HEART_KEYS)),
        "video_count_hint": _to_int(_deep_find(stats, VCOUNT_KEYS)) or _to_int(_deep_find(item, VCOUNT_KEYS)),
    }


def _collect_user_search_items(obj, depth=0):
    if depth > 9 or obj is None:
        return []
    if isinstance(obj, list):
        out = []
        for item in obj:
            out.extend(_collect_user_search_items(item, depth + 1))
        return out
    if isinstance(obj, dict):
        if _user_from_search_item(obj):
            return [obj]
        out = []
        for value in obj.values():
            out.extend(_collect_user_search_items(value, depth + 1))
        return out
    return []


def _user_batch_from_search_payload(data, count):
    users, seen = [], set()
    for item in _collect_user_search_items(data):
        user = _user_from_search_item(item)
        if not user:
            continue
        key = user["account"].lower()
        if key in seen:
            continue
        seen.add(key)
        users.append(user)
        if len(users) >= count:
            break
    return users


def _fetch_discovery_user_search_page(keyword, count, cursor, started=None):
    if _discovery_runtime_exceeded(started):
        raise TikHubError("discovery runtime limit reached")
    data = _send_tikhub_get(
        DISCOVERY_USER_SEARCH_ENDPOINT,
        {
            "keyword": keyword,
            "offset": str(cursor or "0"),
            "count": str(count),
            "source": "search_history",
        },
        "TikHub documented user search endpoint",
        timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS,
        retries=1,
    )
    batch = _user_batch_from_search_payload(data, count)
    if batch:
        return data, batch, DISCOVERY_USER_SEARCH_ENDPOINT
    raise TikHubError("TikHub documented user search endpoint returned no parseable accounts")


def _discover_search_users(keyword, max_items, started):
    items, endpoint, errors = [], DISCOVERY_USER_SEARCH_ENDPOINT, []
    cursor = "0"
    try:
        if SERVER_API_KEY:
            data, batch, endpoint = _fetch_discovery_user_search_page(keyword, min(30, max_items), cursor, started)
            items.extend(batch)
        else:
            raise TikHubError("TikHub user search skipped: TIKHUB_API_KEY is not configured")
    except TikHubError as exc:
        errors.append(_discovery_error(
            "api",
            "tikhub:user-search",
            exc,
            getattr(exc, "status", None),
            "如果搜的是账号昵称，建议粘贴 TikTok 主页链接或 @uniqueId。",
        ))
    return items[:max_items], endpoint, errors


def _discovery_candidate_from_direct_account(keyword, account):
    secuid = _resolve_secuid(account, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS, retries=1)
    if not secuid:
        return None
    profile = _get_profile(account, secuid, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS, retries=1)
    if not any((profile.get("followers"), profile.get("hearts"), profile.get("videoCount"), profile.get("avatar"))):
        return None
    return {
        "account": account,
        "nickname": profile.get("nickname") or account,
        "avatar": profile.get("avatar") or "",
        "secuid": secuid,
        "followers_hint": _to_int(profile.get("followers")),
        "hearts_hint": _to_int(profile.get("hearts")),
        "video_count_hint": _to_int(profile.get("videoCount")),
        "sample_video_count": 0,
        "sample_views": 0,
        "sample_max_views": 0,
        "sample_video_id": "",
        "sample_video_link": "",
        "sample_desc": "",
        "source_keywords": [keyword],
        "source_endpoint": "direct-profile",
    }


def _discover_search_videos(keyword, max_items, started):
    items, seen, endpoint, errors = [], set(), "", []
    cursor = "0"
    for _page in range(1, DISCOVERY_SEARCH_PAGES + 1):
        if _discovery_runtime_exceeded(started) or len(items) >= max_items:
            break
        try:
            if SERVER_API_KEY:
                data, batch, endpoint = _fetch_discovery_search_page(keyword, min(30, max_items - len(items)), cursor, started)
            else:
                raise TikHubError("TikHub search skipped: TIKHUB_API_KEY is not configured")
        except TikHubError as exc:
            errors.append(_discovery_error(
                "api",
                "tikhub:%s" % (DISCOVERY_SEARCH_MODE or "app_v3"),
                exc,
                getattr(exc, "status", None),
                "检查 TIKHUB_API_KEY、余额、权限，以及 TikHub 文档中的搜索视频接口。",
            ))
            if not DISCOVERY_ENABLE_PUBLIC_TIKTOK:
                errors.append(_discovery_error(
                    "browser",
                    "public-tiktok",
                    "Public TikTok web fallback is disabled.",
                    hint="Render 免费服务抓 TikTok 搜索页经常 403；如确需兜底，请显式设置 DISCOVERY_ENABLE_PUBLIC_TIKTOK=1。",
                ))
                break
            try:
                data, batch, endpoint = _fetch_public_tiktok_search_api(keyword, min(30, max_items - len(items)), cursor, started)
            except TikHubError as api_exc:
                errors.append(_discovery_error("browser", "public-tiktok-api", api_exc, getattr(api_exc, "status", None)))
                try:
                    data, batch, endpoint = _fetch_public_tiktok_search_page(keyword, min(30, max_items - len(items)), started)
                except TikHubError as public_exc:
                    errors.append(_discovery_error("browser", "public-tiktok-page", public_exc, getattr(public_exc, "status", None)))
                    break
        for video in batch:
            video_id = _get_video_id(video)
            key = video_id or json.dumps(video, ensure_ascii=False, sort_keys=True)[:200]
            if key in seen:
                continue
            seen.add(key)
            items.append(video)
            if len(items) >= max_items:
                break
        has_more, next_cursor = _read_pagination(data)
        advanced = next_cursor not in (None, "", "0") and str(next_cursor) != str(cursor)
        if has_more in (False, 0, "0") or not advanced:
            break
        cursor = str(next_cursor)
        time.sleep(SCHEDULE_DELAY_MS / 1000.0)
    return items, endpoint, errors


def _discovery_tiktok_urls(value):
    urls, seen = [], set()
    pattern = r"https?://(?:[A-Za-z0-9-]+\.)?tiktok\.com/[^\s\"'<>]+"
    for match in re.finditer(pattern, _to_text(value, 2000), flags=re.I):
        url = html.unescape(match.group(0)).rstrip(").,;，；。】》")
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
        except Exception:
            continue
        if host != "tiktok.com" and not host.endswith(".tiktok.com"):
            continue
        key = url.lower()
        if key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def _discovery_video_id(value):
    text = _to_text(value, 2000)
    match = re.search(r"/video/(\d{8,30})(?:[/?#]|$)", text, flags=re.I)
    if match:
        return match.group(1)
    stripped = text.strip()
    return stripped if re.fullmatch(r"\d{8,30}", stripped) else ""


def _discovery_account(value):
    text = _to_text(value, 1000).strip()
    if _discovery_video_id(text):
        return ""
    match = re.search(r"(?:https?://)?(?:www\.|m\.)?tiktok\.com/@([A-Za-z0-9._-]+)(?:[/?#]|$)", text, flags=re.I)
    if match:
        return match.group(1)
    match = re.fullmatch(r"@([A-Za-z0-9._-]{2,80})", text)
    return match.group(1) if match else ""


def _first_video_from_payload(data):
    batch = _video_batch_from_search_payload(data, 1)
    if batch:
        return batch[0]

    def find(obj, depth=0):
        if depth > 10 or obj is None:
            return None
        if isinstance(obj, dict):
            if _get_video_id(obj) and any(key in obj for key in ("video", "statistics", "stats", "author", "authorInfo", "desc")):
                return obj
            for key in ("aweme_detail", "awemeDetail", "item_info", "itemInfo", "item", "aweme", "video_detail", "videoDetail", "data"):
                found = find(obj.get(key), depth + 1) if key in obj else None
                if found:
                    return found
            for nested in obj.values():
                if isinstance(nested, (dict, list)):
                    found = find(nested, depth + 1)
                    if found:
                        return found
        elif isinstance(obj, list):
            for nested in obj:
                found = find(nested, depth + 1)
                if found:
                    return found
        return None

    return find(data)


def _fetch_discovery_video_by_id(video_id, started=None):
    clean_id = _clean_drama_id(video_id)
    if not clean_id:
        raise TikHubError("作品链接中没有可用的作品 ID")
    last_error = None
    for endpoint in SINGLE_VIDEO_EP_CANDIDATES:
        if _discovery_runtime_exceeded(started):
            raise TikHubError("discovery runtime limit reached")
        params = {"aweme_id": clean_id}
        if endpoint.endswith("_v3"):
            params["region"] = TIKTOK_REGION
        try:
            data = _send_tikhub_get(
                endpoint,
                params,
                "TikHub single video discovery endpoint",
                timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 8,
                retries=1,
            )
            item = _first_video_from_payload(data)
            if item:
                return item, endpoint
            last_error = TikHubError("TikHub single video endpoint returned no parseable work")
        except TikHubError as exc:
            last_error = exc
    raise last_error or TikHubError("作品没有返回可解析的数据")


def _fetch_discovery_video_by_url(share_url, started=None):
    urls = _discovery_tiktok_urls(share_url)
    if not urls:
        raise TikHubError("没有识别到 TikTok 作品链接")
    clean_url = urls[0]
    last_error = None
    for endpoint, param_name in SHARE_VIDEO_EP_CANDIDATES:
        if _discovery_runtime_exceeded(started):
            raise TikHubError("discovery runtime limit reached")
        try:
            data = _send_tikhub_get(
                endpoint,
                {param_name: clean_url},
                "TikHub share link video endpoint",
                timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 12,
                retries=1,
            )
            item = _first_video_from_payload(data)
            if item:
                return item, endpoint
            last_error = TikHubError("TikHub share link endpoint returned no parseable work")
        except TikHubError as exc:
            last_error = exc
    raise last_error or TikHubError("作品分享链接解析失败")


def _fetch_discovery_account_videos(account, max_items, started=None):
    account = _to_text(account, 80).strip().lstrip("@")
    if not account:
        raise TikHubError("账号链接中没有可用的账号 ID")
    secuid = _resolve_secuid(account, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 5, retries=1)
    if not secuid:
        raise TikHubError("没有找到 @%s 的 secUid" % account)
    items, seen = [], set()
    cursor, locked_endpoint = "0", ""
    endpoints = [DEFAULT_ENDPOINTS["posts"]] + [ep for ep in POST_EP_CANDIDATES if ep != DEFAULT_ENDPOINTS["posts"]]
    for _page in range(1, DISCOVERY_SEARCH_PAGES + 1):
        if _discovery_runtime_exceeded(started) or len(items) >= max_items:
            break
        params = {
            "secUid": secuid,
            "sec_user_id": secuid,
            "unique_id": account,
            "count": str(min(30, max_items - len(items))),
            "cursor": str(cursor),
            "max_cursor": str(cursor),
        }
        data, batch, last_error = None, [], None
        candidates = [locked_endpoint] if locked_endpoint else endpoints
        for endpoint in candidates:
            try:
                data = _send_tikhub_get(
                    endpoint,
                    params,
                    "TikHub account works endpoint",
                    timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 8,
                    retries=1,
                )
                batch = _video_batch_from_search_payload(data, min(30, max_items - len(items)))
                if batch:
                    locked_endpoint = endpoint
                    break
                last_error = TikHubError("TikHub account works endpoint returned no parseable works")
            except TikHubError as exc:
                last_error = exc
                if exc.status not in (404, 422):
                    break
        if not batch:
            if items:
                break
            raise last_error or TikHubError("账号主页没有返回可解析的作品")
        for video in batch:
            video_id = _get_video_id(video)
            key = video_id or json.dumps(video, ensure_ascii=False, sort_keys=True)[:200]
            if key in seen:
                continue
            seen.add(key)
            items.append(video)
            if len(items) >= max_items:
                break
        has_more, next_cursor = _read_pagination(data)
        advanced = next_cursor not in (None, "", "0") and str(next_cursor) != str(cursor)
        if has_more in (False, 0, "0") or not advanced:
            break
        cursor = str(next_cursor)
        time.sleep(SCHEDULE_DELAY_MS / 1000.0)
    return items[:max_items], locked_endpoint or DEFAULT_ENDPOINTS["posts"]


def _get_video_metric(video, keys):
    if isinstance(video, dict):
        for container_key in ("statistics", "stats"):
            container = video.get(container_key)
            if isinstance(container, dict):
                for key in keys:
                    if key in container:
                        return _to_int(container[key])
    return _to_int(_deep_find(video, keys))


def _drama_reference_from_video(video):
    info_keys = {
        "dramaInfo", "drama_info", "shortDramaInfo", "short_drama_info",
        "seriesInfo", "series_info",
    }
    candidates = []

    def collect(obj, depth=0):
        if depth > 9 or obj is None:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in info_keys and isinstance(value, dict):
                    candidates.append(value)
                if isinstance(value, (dict, list)):
                    collect(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj:
                collect(value, depth + 1)

    collect(video)
    for info in candidates:
        drama_id = _deep_find(info, ("dramaID", "dramaId", "drama_id"))
        if drama_id is None:
            direct_id = info.get("id")
            if not isinstance(direct_id, (dict, list)):
                drama_id = direct_id
        clean_id = _clean_drama_id(drama_id)
        if not clean_id:
            continue
        title = _to_text(_deep_find(info, DRAMA_NAME_KEYS), 160)
        episodes = _to_int(_deep_find(info, DRAMA_COUNT_KEYS))
        return {
            "drama_id": clean_id,
            "drama_title": title,
            "episode_count": episodes,
            "source": "video-drama-info",
        }
    return {}


def _discovery_work_from_video(video, source_query, source_endpoint, fallback_url="", monitored=None):
    if not isinstance(video, dict):
        return None
    author = _author_from_video(video) or {}
    account = _to_text(author.get("account"), 80).lstrip("@")
    if not account:
        account = _extract_account_from_url(video).lstrip("@") or _extract_account_from_url(fallback_url).lstrip("@")
    video_id = _clean_drama_id(_get_video_id(video))
    if not video_id:
        video_id = _discovery_video_id(fallback_url)
    video_url = _video_link_from_item(account, video) or fallback_url
    if not video_id and not video_url:
        return None
    drama_ref = _drama_reference_from_video(video)
    return {
        "video_id": video_id,
        "description": _to_text(_get_desc(video), 320),
        "account": account,
        "nickname": _to_text(author.get("nickname"), 120) or account,
        "avatar": author.get("avatar") or "",
        "publish_time": _publish_time_of(video),
        "views": _get_play_count(video),
        "likes": _get_video_metric(video, LIKE_KEYS),
        "comments": _get_video_metric(video, COMMENT_KEYS),
        "shares": _get_video_metric(video, SHARE_KEYS),
        "video_url": video_url,
        "profile_url": ("https://www.tiktok.com/@" + account) if account else "",
        "source_queries": [source_query] if source_query else [],
        "source_endpoint": source_endpoint or "",
        "already_monitored": bool(account and account.lower() in (monitored or set())),
        "drama_id": drama_ref.get("drama_id") or "",
        "drama_title": drama_ref.get("drama_title") or "",
        "episode_count": _to_int(drama_ref.get("episode_count")),
    }


def _merge_discovered_work(works, item):
    if not item:
        return
    key = item.get("video_id") or str(item.get("video_url") or "").lower()
    if not key:
        return
    existing = works.get(key)
    if not existing:
        works[key] = item
        return
    for query in item.get("source_queries") or []:
        if query and query not in existing["source_queries"]:
            existing["source_queries"].append(query)
    for field in ("views", "likes", "comments", "shares"):
        existing[field] = max(_to_int(existing.get(field)), _to_int(item.get(field)))
    for field in ("description", "account", "nickname", "avatar", "publish_time", "video_url", "profile_url", "source_endpoint", "drama_id", "drama_title"):
        if not existing.get(field) and item.get(field):
            existing[field] = item[field]
    existing["episode_count"] = max(_to_int(existing.get("episode_count")), _to_int(item.get("episode_count")))
    existing["already_monitored"] = bool(existing.get("already_monitored") or item.get("already_monitored"))


def _discover_works(queries, limit, max_videos_per_query, persist=True):
    started = time.time()
    queries = _parse_discovery_keywords(queries)
    limit = max(1, min(int(limit or DISCOVERY_MAX_CANDIDATES), 200))
    max_videos_per_query = max(1, min(int(max_videos_per_query or DISCOVERY_MAX_VIDEOS_PER_KEYWORD), 200))
    configured_accounts, _source = _configured_schedule_accounts()
    monitored = {item.lower() for item in configured_accounts}
    works, errors = {}, []
    for query in queries:
        if _discovery_runtime_exceeded(started) or len(works) >= limit:
            if _discovery_runtime_exceeded(started):
                errors.append(_discovery_error(
                    "runtime", "discover-works", "discovery runtime limit reached",
                    hint="减少关键词、作品数量或每词搜索数后重试。",
                ))
            break
        remaining = max(1, min(max_videos_per_query, limit - len(works)))
        urls = _discovery_tiktok_urls(query)
        video_id = _discovery_video_id(query)
        account = _discovery_account(query)
        videos, endpoint, query_errors = [], "", []
        fallback_url = urls[0] if urls else ""
        try:
            if video_id:
                video, endpoint = _fetch_discovery_video_by_id(video_id, started)
                videos = [video]
            elif urls and not account:
                video, endpoint = _fetch_discovery_video_by_url(urls[0], started)
                videos = [video]
            elif account:
                videos, endpoint = _fetch_discovery_account_videos(account, remaining, started)
            else:
                videos, endpoint, query_errors = _discover_search_videos(query, remaining, started)
        except Exception as exc:
            errors.append(_discovery_error(
                "works", endpoint or ("direct-link" if urls else "search"), exc,
                getattr(exc, "status", None),
                "可输入关键词、@账号、TikTok 主页链接、作品链接或分享短链接。",
            ) | {"query": query})
            continue
        for error in query_errors:
            item = dict(error) if isinstance(error, dict) else _discovery_error("search", endpoint or "unknown", error)
            item["query"] = query
            errors.append(item)
        for video in videos:
            item = _discovery_work_from_video(video, query, endpoint, fallback_url, monitored)
            _merge_discovered_work(works, item)
            if len(works) >= limit:
                break
    result = sorted(works.values(), key=lambda item: (_to_int(item.get("views")), _to_int(item.get("likes"))), reverse=True)[:limit]
    payload = {
        "ok": True,
        "mode": "works",
        "generated_at": datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "queries": queries,
        "count": len(result),
        "works": result,
        "errors": errors[:80],
        "monitored_count": len(monitored),
        "runtime_seconds": round(time.time() - started, 2),
        "runtime_limit_seconds": DISCOVERY_MAX_RUNTIME_SECONDS,
        "runtime_file": "reports/discovered_works.json",
    }
    return _write_discovered_works(payload) if persist else payload


def _public_drama_search_query(value):
    query = _to_text(value, 120).strip()
    if len(query) < 2:
        raise ValueError("请输入至少 2 个字的短剧名称")
    lowered = query.lower()
    if query.startswith("@") or "tiktok.com/" in lowered or "tiktokv.com/" in lowered:
        raise ValueError("这里只搜索短剧名称，不支持账号或 TikTok 链接")
    return query


def _public_drama_search_cache_key(query, limit):
    return "%s:%s" % (str(query).casefold(), int(limit))


def _public_drama_search_payload(query, limit=20):
    query = _public_drama_search_query(query)
    limit = max(1, min(_to_int(limit) or 20, 20))
    cache_key = _public_drama_search_cache_key(query, limit)
    now = time.time()
    with PUBLIC_DRAMA_SEARCH_CACHE_LOCK:
        cached = PUBLIC_DRAMA_SEARCH_CACHE.get(cache_key)
        if cached and now < cached.get("expires_at", 0):
            payload = dict(cached.get("payload") or {})
            payload["cached"] = True
            return payload
        if cached:
            PUBLIC_DRAMA_SEARCH_CACHE.pop(cache_key, None)

    discovered = _discover_works(query, limit, limit, persist=False)
    grouped = {}
    for work in discovered.get("works") or []:
        if not isinstance(work, dict):
            continue
        drama_id = _clean_drama_id(work.get("drama_id"))
        video_id = _clean_drama_id(work.get("video_id"))
        account = _to_text(work.get("account"), 80).lstrip("@")
        title = _to_text(work.get("drama_title"), 180) or _to_text(work.get("description"), 180)
        if not title:
            title = "TikTok 短剧作品"
        key = ("drama:" + drama_id) if drama_id else ("video:" + video_id)
        if not key or key.endswith(":"):
            continue
        item = grouped.get(key)
        if not item:
            params = {"uid": account, "drama_id": drama_id, "target": "list", "redirect": "1"}
            item = {
                "kind": "drama" if drama_id else "video",
                "drama_id": drama_id,
                "title": title,
                "description": _to_text(work.get("description"), 240),
                "account": account,
                "nickname": _to_text(work.get("nickname"), 120) or account,
                "avatar": _to_text(work.get("avatar"), 800),
                "episode_count": _to_int(work.get("episode_count")),
                "views": _to_int(work.get("views")),
                "likes": _to_int(work.get("likes")),
                "publish_time": _to_text(work.get("publish_time"), 40),
                "video_id": video_id,
                "video_url": _to_text(work.get("video_url"), 1000),
                "profile_url": ("https://www.tiktok.com/@" + account) if account else "",
                "list_url": ("/drama-link?" + urllib.parse.urlencode(params)) if drama_id and account else "",
            }
            grouped[key] = item
            continue
        for field in ("views", "likes", "episode_count"):
            item[field] = max(_to_int(item.get(field)), _to_int(work.get(field)))
        for field, maximum in (("title", 180), ("description", 240), ("account", 80), ("nickname", 120),
                               ("avatar", 800), ("publish_time", 40), ("video_id", 40), ("video_url", 1000)):
            if not item.get(field) and work.get(field):
                item[field] = _to_text(work.get(field), maximum)

    results = sorted(
        grouped.values(),
        key=lambda item: (_to_int(item.get("views")), _to_int(item.get("likes"))),
        reverse=True,
    )[:limit]
    payload = {
        "ok": True,
        "query": query,
        "count": len(results),
        "generated_at": datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "results": results,
        "partial": bool(discovered.get("errors")),
        "cached": False,
    }
    with PUBLIC_DRAMA_SEARCH_CACHE_LOCK:
        if len(PUBLIC_DRAMA_SEARCH_CACHE) >= PUBLIC_DRAMA_SEARCH_CACHE_MAX_ITEMS:
            oldest = min(
                PUBLIC_DRAMA_SEARCH_CACHE,
                key=lambda item_key: PUBLIC_DRAMA_SEARCH_CACHE[item_key].get("created_at", 0),
            )
            PUBLIC_DRAMA_SEARCH_CACHE.pop(oldest, None)
        PUBLIC_DRAMA_SEARCH_CACHE[cache_key] = {
            "created_at": now,
            "expires_at": now + PUBLIC_DRAMA_SEARCH_CACHE_SECONDS,
            "payload": payload,
        }
    return payload


def _public_drama_search_rate_allowed(client_key):
    now = time.time()
    window_start = now - PUBLIC_DRAMA_SEARCH_RATE_WINDOW_SECONDS
    key = str(client_key or "unknown")[:160]
    with PUBLIC_DRAMA_SEARCH_RATE_LOCK:
        timestamps = [stamp for stamp in PUBLIC_DRAMA_SEARCH_RATE.get(key, []) if stamp >= window_start]
        if len(timestamps) >= PUBLIC_DRAMA_SEARCH_RATE_LIMIT:
            PUBLIC_DRAMA_SEARCH_RATE[key] = timestamps
            return False
        timestamps.append(now)
        PUBLIC_DRAMA_SEARCH_RATE[key] = timestamps
        if len(PUBLIC_DRAMA_SEARCH_RATE) > 1000:
            for stale_key in list(PUBLIC_DRAMA_SEARCH_RATE):
                if not PUBLIC_DRAMA_SEARCH_RATE.get(stale_key) or PUBLIC_DRAMA_SEARCH_RATE[stale_key][-1] < window_start:
                    PUBLIC_DRAMA_SEARCH_RATE.pop(stale_key, None)
        return True


def _resolve_drama_reference_for_video(uid, video_id):
    started = time.time()
    clean_video_id = _clean_drama_id(video_id)
    if not clean_video_id:
        raise TikHubError("missing video_id")
    video, _endpoint = _fetch_discovery_video_by_id(clean_video_id, started)
    author = _author_from_video(video) or {}
    account = _to_text(uid or author.get("account"), 80).strip().lstrip("@")
    direct = _drama_reference_from_video(video)
    if direct:
        direct["account"] = account
        return direct
    if not account:
        return {}
    secuid = _resolve_secuid(account, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 6, retries=1)
    if not secuid:
        return {}
    dramas = _get_tiktok_drama_library(
        secuid,
        account,
        started=started,
        max_pages=4,
        timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 10,
        retries=1,
        include_episode_publish_time=False,
    )
    work_title_key = _title_key(_get_desc(video))

    def score(drama):
        title = drama.get("english_title") or drama.get("name") or ""
        title_key = _title_key(title)
        if work_title_key and title_key == work_title_key:
            return 4
        if work_title_key and title_key and (work_title_key in title_key or title_key in work_title_key):
            return 3
        return 0

    ranked = sorted(dramas, key=lambda drama: (score(drama), _to_int(drama.get("views"))), reverse=True)
    likely = [drama for drama in ranked if score(drama) > 0]
    candidates = (likely or ranked)[:8]
    for drama in candidates:
        drama_id = _clean_drama_id(drama.get("drama_id"))
        if not drama_id:
            continue
        limit = min(DRAMA_LINK_MAX_EPISODES, max(50, _to_int(drama.get("episodes"))))
        episode_items = _get_drama_episode_items(drama_id, account, started=started, limit=limit)
        if any(_clean_drama_id(_get_video_id(item)) == clean_video_id for item in episode_items):
            return {
                "account": account,
                "drama_id": drama_id,
                "drama_title": drama.get("english_title") or drama.get("name") or "",
                "episode_count": len(episode_items) or _to_int(drama.get("episodes")),
                "source": "account-drama-library",
            }
    if likely and score(likely[0]) >= 4:
        drama = likely[0]
        return {
            "account": account,
            "drama_id": _clean_drama_id(drama.get("drama_id")),
            "drama_title": drama.get("english_title") or drama.get("name") or "",
            "episode_count": _to_int(drama.get("episodes")),
            "source": "account-drama-title",
        }
    return {}


def _discovery_raw_account_score(item):
    return max(_to_int(item.get("sample_max_views")), _to_int(item.get("sample_views")), _to_int(item.get("followers_hint")))


def _discovery_account_score(item):
    return max(_to_int(item.get("sample_max_views")), _to_int(item.get("total_views")), _to_int(item.get("followers")))


def _discover_accounts(keywords, max_accounts, min_followers, min_dramas, max_videos_per_keyword):
    started = time.time()
    keywords = _parse_discovery_keywords(keywords)
    max_accounts = max(1, min(int(max_accounts or DISCOVERY_MAX_CANDIDATES), 500))
    max_videos_per_keyword = max(1, min(int(max_videos_per_keyword or DISCOVERY_MAX_VIDEOS_PER_KEYWORD), 200))
    min_followers = max(0, int(min_followers or 0))
    min_dramas = max(0, int(min_dramas or 0))
    configured_accounts, _source = _configured_schedule_accounts()
    monitored = {item.lower() for item in configured_accounts}
    candidates, errors = {}, []
    raw_candidate_target = min(500, max(max_accounts * 4, max_accounts + 40))
    for keyword in keywords:
        if _discovery_runtime_exceeded(started):
            errors.append(_discovery_error(
                "runtime",
                "discover",
                "runtime limit reached while searching",
                hint="减少关键词、候选数量或搜索视频上限后重试。",
            ))
            break
        if candidates and _discovery_remaining_seconds(started) <= DISCOVERY_ENRICH_RESERVE_SECONDS:
            errors.append(_discovery_error(
                "runtime",
                "discover",
                "search stopped early to reserve time for account enrichment",
                hint="已保留时间补全候选账号资料；想覆盖更多关键词可以降低搜索视频数或分批搜索。",
            ))
            break
        if len(candidates) >= raw_candidate_target:
            errors.append(_discovery_error(
                "search",
                "candidate-budget",
                "raw candidate target reached",
                hint="已找到足够原始候选，优先补全播放量较高的账号以避免请求超时。",
            ))
            break
        account_query = _looks_like_account_query(keyword)
        if account_query:
            before_accounts = len(candidates)
            for direct_account in _account_lookup_candidates(keyword):
                key = direct_account.lower()
                if key in candidates:
                    continue
                try:
                    direct_item = _discovery_candidate_from_direct_account(keyword, direct_account)
                except Exception as exc:
                    errors.append(_discovery_error(
                        "account_lookup",
                        "tikhub:profile",
                        exc,
                        getattr(exc, "status", None),
                        "账号直查失败；会继续尝试 TikHub 用户搜索。",
                    ) | {"keyword": keyword, "account": direct_account})
                    continue
                if direct_item:
                    candidates[key] = direct_item
            users, user_endpoint, user_errors = _discover_search_users(keyword, min(max_accounts, 10), started)
            for err in user_errors:
                if isinstance(err, dict):
                    item = dict(err)
                    item["keyword"] = keyword
                    errors.append(item)
            for user in users:
                account = user["account"]
                key = account.lower()
                item = candidates.setdefault(key, {
                    "account": account,
                    "nickname": user.get("nickname") or account,
                    "avatar": user.get("avatar") or "",
                    "secuid": user.get("secuid") or "",
                    "followers_hint": user.get("followers_hint") or 0,
                    "hearts_hint": user.get("hearts_hint") or 0,
                    "video_count_hint": user.get("video_count_hint") or 0,
                    "sample_video_count": 0,
                    "sample_views": 0,
                    "sample_max_views": 0,
                    "sample_video_id": "",
                    "sample_video_link": "",
                    "sample_desc": "",
                    "source_keywords": [],
                    "source_endpoint": user_endpoint,
                })
                if keyword not in item["source_keywords"]:
                    item["source_keywords"].append(keyword)
                if user.get("secuid") and not item.get("secuid"):
                    item["secuid"] = user["secuid"]
                if user.get("avatar") and not item.get("avatar"):
                    item["avatar"] = user["avatar"]
                item["followers_hint"] = max(_to_int(item.get("followers_hint")), _to_int(user.get("followers_hint")))
                item["hearts_hint"] = max(_to_int(item.get("hearts_hint")), _to_int(user.get("hearts_hint")))
                item["video_count_hint"] = max(_to_int(item.get("video_count_hint")), _to_int(user.get("video_count_hint")))
            if len(candidates) > before_accounts:
                continue
            errors.append(_discovery_error(
                "account_lookup",
                "direct-or-user-search",
                "No account candidates matched this account-like query.",
                hint="账号昵称不一定等于 @uniqueId；请粘贴 TikTok 主页链接或 @账号ID。",
            ) | {"keyword": keyword})
        video_limit = min(max_videos_per_keyword, 12) if account_query else max_videos_per_keyword
        videos, endpoint, search_errors = _discover_search_videos(keyword, video_limit, started)
        for err in search_errors:
            if isinstance(err, dict):
                item = dict(err)
                item["keyword"] = keyword
                errors.append(item)
            else:
                errors.append(_discovery_error("search", endpoint or "unknown", err, hint="搜索接口没有返回可用视频作者。"))
        for video in videos:
            author = _author_from_video(video)
            if not author:
                continue
            account = author["account"]
            key = account.lower()
            video_id = _get_video_id(video)
            views = _get_play_count(video)
            item = candidates.setdefault(key, {
                "account": account,
                "nickname": author.get("nickname") or account,
                "avatar": author.get("avatar") or "",
                "secuid": author.get("secuid") or "",
                "followers_hint": author.get("followers_hint") or 0,
                "hearts_hint": author.get("hearts_hint") or 0,
                "video_count_hint": author.get("video_count_hint") or 0,
                "sample_video_count": 0,
                "sample_views": 0,
                "sample_max_views": 0,
                "sample_video_id": "",
                "sample_video_link": "",
                "sample_desc": "",
                "source_keywords": [],
                "source_endpoint": endpoint,
            })
            item["sample_video_count"] += 1
            item["sample_views"] += views
            if keyword not in item["source_keywords"]:
                item["source_keywords"].append(keyword)
            if views > item.get("sample_max_views", 0):
                item["sample_max_views"] = views
                item["sample_video_id"] = video_id
                item["sample_video_link"] = _video_link_from_item(account, video)
                item["sample_desc"] = _to_text(_get_desc(video), 160)
            if author.get("avatar") and not item.get("avatar"):
                item["avatar"] = author["avatar"]
    enriched = []
    raw_candidates = sorted(candidates.values(), key=_discovery_raw_account_score, reverse=True)
    for item in raw_candidates[:max_accounts * 2]:
        if _discovery_runtime_exceeded(started):
            errors.append(_discovery_error(
                "runtime",
                "discover",
                "runtime limit reached while enriching accounts",
                hint="候选账号已部分返回，可以减少候选数量后重试以补全粉丝和短剧库数据。",
            ))
            break
        account = item["account"]
        profile = {}
        dramas = []
        secuid = item.get("secuid") or ""
        enrich_error = ""
        try:
            if SERVER_API_KEY:
                if not secuid:
                    secuid = _resolve_secuid(account, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS, retries=1)
                if secuid:
                    profile = _get_profile(account, secuid, timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS, retries=1)
                    try:
                        dramas = _get_tiktok_drama_library(
                            secuid,
                            account,
                            started=started,
                            max_pages=3,
                            timeout=DISCOVERY_SEARCH_TIMEOUT_SECONDS + 8,
                            retries=1,
                            include_episode_publish_time=False,
                        )
                    except TikHubError:
                        dramas = []
        except Exception as exc:
            enrich_error = str(exc)
            errors.append(_discovery_error(
                "enrich",
                "tikhub:profile_or_drama",
                enrich_error,
                getattr(exc, "status", None),
                "账号来自搜索结果，但资料或短剧库补全失败；候选仍可加入监控池。",
            ) | {"account": account})
        total_views = sum(_to_int(drama.get("views")) for drama in dramas) or _to_int(item.get("sample_views"))
        top_drama = max(dramas, key=lambda drama: _to_int(drama.get("views")), default={})
        followers = _to_int(profile.get("followers")) or _to_int(item.get("followers_hint"))
        sample_max_views = max(_to_int(item.get("sample_max_views")), _to_int(top_drama.get("views")))
        sample_views = _to_int(item.get("sample_views")) or total_views
        profile_url = "https://www.tiktok.com/@" + account
        drama_count = len(dramas)
        if followers < min_followers or drama_count < min_dramas:
            continue
        enriched.append({
            "account": account,
            "nickname": profile.get("nickname") or item.get("nickname") or account,
            "avatar": profile.get("avatar") or item.get("avatar") or "",
            "followers": followers,
            "hearts": _to_int(profile.get("hearts")) or _to_int(item.get("hearts_hint")),
            "video_count": _to_int(profile.get("videoCount")) or _to_int(item.get("video_count_hint")),
            "dramas": drama_count,
            "total_views": total_views,
            "top_drama": top_drama.get("english_title") or top_drama.get("name") or "",
            "top_drama_views": _to_int(top_drama.get("views")),
            "sample_video_count": _to_int(item.get("sample_video_count")),
            "sample_views": sample_views,
            "sample_max_views": sample_max_views,
            "sample_video_id": item.get("sample_video_id") or "",
            "sample_video_link": item.get("sample_video_link") or profile_url,
            "sample_desc": item.get("sample_desc") or "",
            "source_keywords": item.get("source_keywords") or [],
            "source_endpoint": item.get("source_endpoint") or "",
            "profile_url": profile_url,
            "already_monitored": account.lower() in monitored,
            "enrich_error": enrich_error,
        })
    enriched.sort(key=_discovery_account_score, reverse=True)
    payload = {
        "ok": True,
        "generated_at": datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "keywords": keywords,
        "count": len(enriched[:max_accounts]),
        "accounts": enriched[:max_accounts],
        "errors": errors[:80],
        "search_candidates": len(raw_candidates),
        "monitored_count": len(monitored),
        "runtime_seconds": round(time.time() - started, 2),
        "runtime_limit_seconds": DISCOVERY_MAX_RUNTIME_SECONDS,
        "runtime_file": "reports/discovered_accounts.json",
    }
    return _write_discovered_accounts(payload)


def _resolve_secuid(uid, timeout=90, retries=None):
    try:
        data = _send_tikhub_get(
            DEFAULT_ENDPOINTS["secuid"],
            {"username": uid, "unique_id": uid},
            "secUid endpoint",
            timeout=timeout,
            retries=retries,
        )
        found = _deep_find(data, SECUID_KEYS)
        return "" if found is None else str(found)
    except TikHubError as exc:
        if exc.status in (401, 402, 403):
            raise
    return ""


def _get_profile(uid, secuid, timeout=90, retries=None):
    profile = {"nickname": uid, "followers": 0, "hearts": 0, "videoCount": 0, "avatar": ""}
    try:
        data = _send_tikhub_get(DEFAULT_ENDPOINTS["profile"], {
            "sec_user_id": secuid,
            "secUid": secuid,
            "unique_id": uid,
        }, "profile endpoint", timeout=timeout, retries=retries)
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


def _apply_previous_profile_metrics(profile, previous_metrics=None):
    """Keep the last valid account totals when the profile API temporarily returns zero."""
    clean = dict(profile or {})
    previous = previous_metrics if isinstance(previous_metrics, dict) else {}
    for key in ("followers", "hearts"):
        current_value = _to_int(clean.get(key))
        previous_value = _to_int(previous.get(key))
        if current_value <= 0 and previous_value > 0:
            clean[key] = previous_value
    return clean


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
            play_url = _get_video_play_url(_get_video_id(item), started, uid)
            if play_url:
                return play_url
        if not fallback:
            fallback = _video_link_from_item(uid, item)
    return "" if prefer_play else fallback


def _get_tiktok_drama_library(secuid, uid, started=None, max_pages=None, timeout=90, retries=None, include_episode_publish_time=True):
    if not secuid:
        return []
    local_started = time.time()
    dramas, seen = [], set()
    cursor, prev, stall = "0", None, 0
    page_limit = max_pages or SCHEDULE_MAX_PAGES
    for _page in range(1, page_limit + 1):
        if started is not None:
            if _discovery_runtime_exceeded(started):
                break
        elif _runtime_exceeded(local_started):
            break
        data = _send_tiktok_get("/api/drama/user/drama_list/", {
            "secUid": secuid,
            "aid": TIKTOK_AID,
            "language": TIKTOK_LANGUAGE,
            "region": TIKTOK_REGION,
            "count": str(SCHEDULE_DRAMA_PAGE_SIZE),
            "cursor": str(cursor),
        }, "TikTok drama library endpoint", uid, timeout=timeout, retries=retries)
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
            if include_episode_publish_time and not detail.get("publish_time") and drama_key:
                detail["publish_time"] = _get_drama_first_episode_publish_time(drama_key, uid, started or local_started)
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


def _scrape_account(uid, previous_metrics=None):
    secuid = _resolve_secuid(uid)
    profile = _apply_previous_profile_metrics(
        _get_profile(uid, secuid),
        previous_metrics,
    )
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


def _report_file_path(name):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.normpath(os.path.join(REPORTS_DIR, name))
    try:
        safe = os.path.commonpath((os.path.normpath(REPORTS_DIR), path)) == os.path.normpath(REPORTS_DIR)
    except ValueError:
        safe = False
    if not safe:
        raise RuntimeError("bad report path")
    return path


def _write_text_file(name, content):
    path = _report_file_path(name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


def _write_csv_file(name, columns, rows):
    """Write rows directly so a multi-megabyte CSV is never duplicated in RAM."""
    path = _report_file_path(name)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_json_file(name, payload):
    """Stream JSON encoding to disk instead of building one giant string."""
    path = _report_file_path(name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return path


REPORT_ARCHIVE_RE = re.compile(
    r"^scheduled_(?:report|dramas)_(\d{8}-\d{6})\.(?:json|csv)$",
    re.IGNORECASE,
)


def _retention_cutoff(now=None):
    current = now or datetime.datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    return current.astimezone(BEIJING_TZ) - datetime.timedelta(days=REPORT_RETENTION_DAYS)


def _report_archive_datetime(name):
    match = REPORT_ARCHIVE_RE.match(os.path.basename(str(name or "")))
    if not match:
        return None
    try:
        return datetime.datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        return None


def _cleanup_runtime_report_files(now=None):
    status = {"ok": True, "retention_days": REPORT_RETENTION_DAYS, "deleted": []}
    cutoff = _retention_cutoff(now)
    if not os.path.isdir(REPORTS_DIR):
        return status
    try:
        for name in os.listdir(REPORTS_DIR):
            archived_at = _report_archive_datetime(name)
            if not archived_at or archived_at >= cutoff:
                continue
            full = os.path.normpath(os.path.join(REPORTS_DIR, name))
            if os.path.dirname(full) != os.path.normpath(REPORTS_DIR) or not os.path.isfile(full):
                continue
            os.remove(full)
            status["deleted"].append(name)
        status["deleted_count"] = len(status["deleted"])
    except Exception as exc:
        status["ok"] = False
        status["error"] = str(exc)
    return status


def _supabase_project_url():
    url = SUPABASE_URL.strip().rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[:-len("/rest/v1")].rstrip("/")
    return url


def _supabase_configured():
    return bool(_supabase_project_url() and SUPABASE_SERVICE_KEY)


def _supabase_uses_new_api_key():
    key = SUPABASE_SERVICE_KEY.strip()
    return key.startswith("sb_secret_") or key.startswith("sb_publishable_")


def _supabase_request(method, path, payload=None, prefer="", timeout=45):
    base_url = _supabase_project_url()
    if not base_url or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured")
    url = base_url + "/rest/v1" + path
    body = None
    headers = {
        "Accept": "application/json",
        "apikey": SUPABASE_SERVICE_KEY,
        "User-Agent": SUPABASE_USER_AGENT,
    }
    if not _supabase_uses_new_api_key():
        headers["Authorization"] = "Bearer " + SUPABASE_SERVICE_KEY
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            return json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError("Supabase %s %s failed with HTTP %s: %s" % (method, path, exc.code, detail))


def _catalog_now():
    return datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _empty_admin_catalog():
    return {
        "version": 2,
        "revision": 0,
        "updated_at": "",
        "dramas": {},
        "sources": {},
    }


def _catalog_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _catalog_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _catalog_identifier(value, prefix="drama"):
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip()).strip("-")[:80]
    return value or ((prefix + "-") if prefix else "") + uuid.uuid4().hex[:12]


def _catalog_aliases(value):
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[\n,，;；]+", str(value or ""))
    aliases, seen = [], set()
    for part in parts:
        alias = _to_text(part, 300).strip()
        key = alias.lower()
        if alias and key not in seen:
            seen.add(key)
            aliases.append(alias)
        if len(aliases) >= 30:
            break
    return aliases


def _sanitize_admin_catalog(payload):
    source = payload if isinstance(payload, dict) else {}
    result = _empty_admin_catalog()
    result["revision"] = max(0, _to_int(source.get("revision")))
    result["updated_at"] = _to_text(source.get("updated_at"), 60)

    raw_dramas = source.get("dramas") if isinstance(source.get("dramas"), dict) else {}
    dramas = {}
    for raw_id, raw in list(raw_dramas.items())[:10000]:
        if not isinstance(raw, dict):
            continue
        drama_id = _catalog_identifier(raw.get("id") or raw_id)
        if drama_id in dramas:
            continue
        created_at = _to_text(raw.get("created_at"), 60) or _catalog_now()
        raw_bound_accounts = raw.get("bound_accounts") or []
        if isinstance(raw_bound_accounts, list):
            bound_accounts = _parse_accounts("\n".join(str(item) for item in raw_bound_accounts[:5000]))
        else:
            bound_accounts = _parse_accounts(str(raw_bound_accounts))
        dramas[drama_id] = {
            "id": drama_id,
            "chinese_title": _to_text(raw.get("chinese_title") or raw.get("cn"), 300),
            "english_title": _to_text(raw.get("english_title") or raw.get("en"), 300),
            "writer": _to_text(raw.get("writer"), 160),
            "producer": _to_text(raw.get("producer"), 160),
            "director": _to_text(raw.get("director"), 160),
            "cast": _to_text(raw.get("cast"), 500),
            "aliases": _catalog_aliases(raw.get("aliases")),
            "bound_accounts": bound_accounts,
            "notes": _to_text(raw.get("notes"), 2000),
            "online": _catalog_bool(raw.get("online")),
            "order": max(1, min(_to_int(raw.get("order")) or len(dramas) + 1, 1000000)),
            "created_at": created_at,
            "updated_at": _to_text(raw.get("updated_at"), 60) or created_at,
        }
    result["dramas"] = dramas

    raw_sources = source.get("sources") if isinstance(source.get("sources"), dict) else {}
    sources = {}
    for raw_key, raw in list(raw_sources.items())[:50000]:
        if not isinstance(raw, dict):
            continue
        source_key = _to_text(raw_key, 500).strip()
        if not source_key:
            continue
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in ("pending", "owned", "ignored"):
            status = "pending"
        drama_id = _catalog_identifier(raw.get("drama_id"), prefix="") if raw.get("drama_id") else ""
        if status == "owned" and drama_id not in dramas:
            status, drama_id = "pending", ""
        if status != "owned":
            drama_id = ""
        sources[source_key] = {
            "status": status,
            "drama_id": drama_id,
            "updated_at": _to_text(raw.get("updated_at"), 60) or _catalog_now(),
        }
    result["sources"] = sources
    return result


def _read_admin_catalog_file():
    try:
        with open(ADMIN_CATALOG_FILE, "r", encoding="utf-8-sig") as handle:
            return _sanitize_admin_catalog(json.load(handle))
    except Exception:
        return _empty_admin_catalog()


def _write_admin_catalog_file(catalog):
    os.makedirs(os.path.dirname(ADMIN_CATALOG_FILE), exist_ok=True)
    tmp = ADMIN_CATALOG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, ADMIN_CATALOG_FILE)


def _read_admin_catalog_supabase():
    rows = _supabase_request(
        "GET",
        "/report_runs?select=id,raw,generated_at,created_at&source=eq.%s&order=id.desc&limit=1"
        % urllib.parse.quote(ADMIN_CATALOG_SOURCE, safe=""),
        timeout=20,
    )
    if not isinstance(rows, list) or not rows:
        return _empty_admin_catalog(), 0
    raw = rows[0].get("raw") if isinstance(rows[0], dict) else {}
    catalog = raw.get("catalog") if isinstance(raw, dict) and isinstance(raw.get("catalog"), dict) else raw
    return _sanitize_admin_catalog(catalog), _to_int(rows[0].get("id"))


def _load_admin_catalog(force=False):
    now = time.time()
    with ADMIN_CATALOG_LOCK:
        cached = ADMIN_CATALOG_CACHE.get("catalog")
        if not force and cached is not None and now < ADMIN_CATALOG_CACHE.get("expires_at", 0):
            return _catalog_copy(cached), ADMIN_CATALOG_CACHE.get("storage") or "cache"

        storage = "runtime_file"
        catalog = None
        if SUPABASE_ENABLED and _supabase_configured():
            try:
                catalog, _row_id = _read_admin_catalog_supabase()
                storage = "supabase"
                if catalog.get("revision") or catalog.get("dramas") or catalog.get("sources"):
                    _write_admin_catalog_file(catalog)
                elif os.path.isfile(ADMIN_CATALOG_FILE):
                    local_catalog = _read_admin_catalog_file()
                    if local_catalog.get("revision") or local_catalog.get("dramas") or local_catalog.get("sources"):
                        catalog = local_catalog
                        storage = "runtime_file"
            except Exception:
                catalog = None
                storage = "runtime_file"
        if catalog is None:
            catalog = _read_admin_catalog_file()

        ADMIN_CATALOG_CACHE.update({
            "catalog": _catalog_copy(catalog),
            "storage": storage,
            "expires_at": now + ADMIN_CATALOG_CACHE_SECONDS,
        })
        return _catalog_copy(catalog), storage


def _persist_admin_catalog(payload, expected_revision=None):
    with ADMIN_CATALOG_LOCK:
        current, _storage = _load_admin_catalog(force=True)
        current_revision = _to_int(current.get("revision"))
        if expected_revision is not None and _to_int(expected_revision) != current_revision:
            raise AdminCatalogConflict(
                "catalog changed on the server (expected revision %s, current revision %s)"
                % (_to_int(expected_revision), current_revision)
            )
        catalog = _sanitize_admin_catalog(payload)
        catalog["revision"] = current_revision + 1
        catalog["updated_at"] = _catalog_now()
        storage = "runtime_file"

        if SUPABASE_ENABLED and _supabase_configured():
            _existing, row_id = _read_admin_catalog_supabase()
            row = {
                "generated_at": catalog["updated_at"],
                "source": ADMIN_CATALOG_SOURCE,
                "accounts_count": 0,
                "dramas_count": len(catalog["dramas"]),
                "raw": {"kind": ADMIN_CATALOG_SOURCE, "catalog": catalog},
            }
            if row_id:
                _supabase_request(
                    "PATCH",
                    "/report_runs?id=eq.%s" % row_id,
                    row,
                    prefer="return=minimal",
                    timeout=20,
                )
            else:
                _supabase_request("POST", "/report_runs", row, prefer="return=minimal", timeout=20)
            storage = "supabase"

        _write_admin_catalog_file(catalog)
        ADMIN_CATALOG_CACHE.update({
            "catalog": _catalog_copy(catalog),
            "storage": storage,
            "expires_at": time.time() + ADMIN_CATALOG_CACHE_SECONDS,
        })
        return _catalog_copy(catalog), storage


def _latest_catalog_report():
    if _supabase_report_read_enabled():
        try:
            return _supabase_latest_report_payload()
        except Exception:
            pass
    for path in (
        os.path.join(REPORTS_DIR, "latest_report.json"),
        os.path.join(PUBLIC_REPORTS_DIR, "latest_report.json"),
    ):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue
    return {"generated_at": "", "summary": [], "dramas_detail": []}


def _admin_source_row(row):
    if not isinstance(row, dict):
        return None
    account = _to_text(row.get("Account / 账号") or row.get("账号") or row.get("a"), 100).strip().lstrip("@")
    english_title = _to_text(
        row.get("English Title / 英文剧名") or row.get("短剧名") or row.get("en"),
        300,
    )
    chinese_title = _to_text(row.get("Chinese Title / 中文剧名") or row.get("cn"), 300)
    drama_id = _clean_drama_id(row.get("Drama ID / 短剧ID") or row.get("短剧ID") or row.get("id"))
    source_key = _drama_cache_key(account, drama_id, english_title or chinese_title)
    if not account or not source_key:
        return None
    return {
        "key": source_key,
        "account": account,
        "nickname": _to_text(row.get("Nickname / 昵称") or row.get("昵称") or row.get("n"), 160) or account,
        "drama_id": drama_id,
        "english_title": english_title,
        "chinese_title": chinese_title,
        "publish_time": _to_text(row.get("Publish Time / 发布时间") or row.get("发布时间") or row.get("p"), 60),
        "episodes": _to_int(row.get("Episodes / 集数") or row.get("集数") or row.get("e")),
        "views": _to_int(row.get("Views / 观看数") or row.get("累计观看") or row.get("v")),
        "themes": _to_text(
            row.get("Chinese Themes / 中文题材") or row.get("English Themes / 英文题材")
            or row.get("ct") or row.get("et"),
            500,
        ),
        "description": _to_text(
            row.get("Chinese Description / 中文简介") or row.get("English Description Preview / 英文简介预览"),
            2000,
        ),
        "link": _to_text(row.get("Drama Link / 短剧链接") or row.get("短剧链接"), 800),
        "profile_url": _to_text(row.get("Source Profile URL / 来源主页") or row.get("主页链接"), 800),
    }


def _admin_catalog_context():
    report = _latest_catalog_report()
    source_rows = []
    source_map = {}
    for raw in report.get("dramas_detail") or []:
        row = _admin_source_row(raw)
        if not row or row["key"] in source_map:
            continue
        source_map[row["key"]] = row
        source_rows.append(row)

    accounts = []
    for raw in report.get("summary") or []:
        if not isinstance(raw, dict):
            continue
        account = _to_text(raw.get("账号") or raw.get("Account / 账号") or raw.get("a"), 100).strip().lstrip("@")
        if not account:
            continue
        accounts.append({
            "account": account,
            "nickname": _to_text(raw.get("昵称") or raw.get("Nickname / 昵称") or raw.get("n"), 160) or account,
            "followers": _to_int(raw.get("粉丝")),
            "dramas": _to_int(raw.get("短剧数") or raw.get("d")),
            "views": _to_int(raw.get("累计观看") or raw.get("v")),
            "profile_url": _to_text(raw.get("主页链接"), 800),
        })
    return report, source_rows, source_map, accounts


def _curated_catalog_payload(include_offline=False):
    catalog, storage = _load_admin_catalog()
    report, _source_rows, source_map, _accounts = _admin_catalog_context()
    grouped_keys = {}
    for source_key, relation in catalog.get("sources", {}).items():
        if relation.get("status") != "owned" or not relation.get("drama_id"):
            continue
        grouped_keys.setdefault(relation["drama_id"], []).append(source_key)

    dramas = []
    for drama_id, drama in catalog.get("dramas", {}).items():
        if not include_offline and not drama.get("online"):
            continue
        keys = grouped_keys.get(drama_id, [])
        sources = [source_map[key] for key in keys if key in source_map]
        source_accounts = [item.get("account") for item in sources if item.get("account")]
        accounts = sorted(
            set(source_accounts + list(drama.get("bound_accounts") or [])),
            key=lambda value: str(value).lower(),
        )
        total_views = sum(_to_int(item.get("views")) for item in sources)
        episodes = max([_to_int(item.get("episodes")) for item in sources] or [0])
        latest_publish_time = max([item.get("publish_time") or "" for item in sources] or [""])
        themes = []
        for item in sources:
            theme = _to_text(item.get("themes"), 500)
            if theme and theme not in themes:
                themes.append(theme)
        dramas.append({
            "id": drama_id,
            "chinese_title": drama.get("chinese_title") or "",
            "english_title": drama.get("english_title") or "",
            "writer": drama.get("writer") or "",
            "producer": drama.get("producer") or "",
            "director": drama.get("director") or "",
            "cast": drama.get("cast") or "",
            "aliases": drama.get("aliases") or [],
            "notes": (drama.get("notes") or "") if include_offline else "",
            "online": bool(drama.get("online")),
            "order": _to_int(drama.get("order")),
            "created_at": drama.get("created_at") or "",
            "updated_at": drama.get("updated_at") or "",
            "accounts": accounts,
            "source_count": len(keys),
            "active_source_count": len(sources),
            "total_views": total_views,
            "episodes": episodes,
            "latest_publish_time": latest_publish_time,
            "themes": themes,
            "sources": sources,
        })

    ranking = sorted(dramas, key=lambda item: (-_to_int(item.get("total_views")), item.get("order") or 1000000))
    for index, item in enumerate(ranking, 1):
        item["rank"] = index
    by_manual_order = sorted(
        dramas,
        key=lambda item: (item.get("order") or 1000000, item.get("created_at") or "", item.get("id") or ""),
    )
    return {
        "ok": True,
        "generated_at": report.get("generated_at") or "",
        "updated_at": catalog.get("updated_at") or "",
        "revision": catalog.get("revision") or 0,
        "storage": storage,
        "count": len(by_manual_order),
        "dramas": by_manual_order,
        "ranking": ranking,
    }


def _supabase_timestamp(value, fallback_now=False):
    epoch = _publish_epoch(value)
    if epoch is not None:
        return datetime.datetime.fromtimestamp(epoch, BEIJING_TZ).isoformat(timespec="seconds")
    if fallback_now:
        return datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    return None


def _supabase_upsert(table, rows, on_conflict):
    path = "/" + urllib.parse.quote(table, safe="")
    if on_conflict:
        path += "?on_conflict=" + urllib.parse.quote(on_conflict, safe=",")
    prefer = "resolution=merge-duplicates,return=minimal"
    written = 0
    batch = []
    for row in rows or ():
        if not isinstance(row, dict) or not row:
            continue
        batch.append(row)
        if len(batch) < SUPABASE_BATCH_SIZE:
            continue
        _supabase_request("POST", path, batch, prefer=prefer)
        written += len(batch)
        batch = []
    if batch:
        _supabase_request("POST", path, batch, prefer=prefer)
        written += len(batch)
    return written


def _summary_account(row):
    account = _to_text(row.get("账号") or row.get("Account / 账号"), 100).strip().lstrip("@")
    return account


def _supabase_account_rows(summary_rows, generated_at):
    rows = {}
    for row in summary_rows or []:
        if not isinstance(row, dict):
            continue
        account = _summary_account(row)
        if not account:
            continue
        rows[account.lower()] = {
            "account": account,
            "nickname": _to_text(row.get("昵称"), 160),
            "avatar": _to_text(row.get("头像"), 500),
            "profile_url": _to_text(row.get("主页链接"), 500),
            "last_seen_at": generated_at,
        }
    return list(rows.values())


def _supabase_account_snapshot_rows(run_id, summary_rows):
    rows = []
    for row in summary_rows or []:
        if not isinstance(row, dict):
            continue
        account = _summary_account(row)
        if not account:
            continue
        rows.append({
            "run_id": run_id,
            "account": account,
            "followers": _to_int(row.get("粉丝")),
            "hearts": _to_int(row.get("点赞")),
            "video_count": _to_int(row.get("总集数")),
            "drama_count": _to_int(row.get("短剧数")),
            "total_views": _to_int(row.get("累计观看")),
            "top_drama": _to_text(row.get("最高观看短剧"), 220),
            "top_drama_views": _to_int(row.get("最高观看")),
            "raw": row,
        })
    return rows


def _supabase_drama_key(row):
    account = _summary_account(row)
    if not account:
        return ""
    drama_id = _clean_drama_id(row.get("Drama ID / 短剧ID"))
    if drama_id:
        return "%s|id:%s" % (account.lower(), drama_id)
    title = _to_text(
        row.get("English Title / 英文剧名") or row.get("短剧名") or row.get("Chinese Title / 中文剧名"),
        220,
    )
    if not title:
        return ""
    stable = "%s|%s|%s" % (
        account.lower(),
        _title_key(title) or title.lower(),
        _to_text(row.get("Rank in Account / 账号内排序"), 30),
    )
    return "%s|auto:%s" % (account.lower(), uuid.uuid5(uuid.NAMESPACE_URL, stable).hex[:16])


def _supabase_drama_rows(drama_rows, generated_at):
    rows = {}
    for row in drama_rows or []:
        if not isinstance(row, dict):
            continue
        account = _summary_account(row)
        drama_key = _supabase_drama_key(row)
        if not account or not drama_key:
            continue
        english_themes = _to_text(row.get("English Themes / 英文题材"), 500)
        chinese_themes = _to_text(row.get("Chinese Themes / 中文题材"), 500)
        rows[drama_key] = {
            "drama_key": drama_key,
            "account": account,
            "drama_id": _clean_drama_id(row.get("Drama ID / 短剧ID")),
            "english_title": _to_text(row.get("English Title / 英文剧名") or row.get("短剧名"), 300),
            "chinese_title": _to_text(row.get("Chinese Title / 中文剧名"), 300),
            "themes": " / ".join(part for part in (english_themes, chinese_themes) if part),
            "last_seen_at": generated_at,
        }
    return list(rows.values())


def _supabase_drama_snapshot_rows(run_id, drama_rows):
    rows = []
    for row in drama_rows or []:
        if not isinstance(row, dict):
            continue
        account = _summary_account(row)
        drama_key = _supabase_drama_key(row)
        if not account or not drama_key:
            continue
        rows.append({
            "run_id": run_id,
            "drama_key": drama_key,
            "account": account,
            "views": _to_int(row.get("Views / 观看数") or row.get("累计观看")),
            "episodes": _to_int(row.get("Episodes / 集数") or row.get("集数")),
            "publish_time": _supabase_timestamp(row.get("Publish Time / 发布时间")),
            "raw": row,
        })
    return rows


def _supabase_insert_report_run(payload):
    row = {
        "generated_at": _supabase_timestamp(payload.get("generated_at"), fallback_now=True),
        "source": "render",
        "accounts_count": _to_int(payload.get("accounts")),
        "dramas_count": _to_int(payload.get("dramas")),
        "raw": payload,
    }
    response = _supabase_request("POST", "/report_runs?select=id", row, prefer="return=representation")
    if isinstance(response, list) and response:
        return _to_int(response[0].get("id"))
    if isinstance(response, dict):
        return _to_int(response.get("id"))
    return 0


def _cache_supabase_report_payload(payload, run_id=None, latest=False):
    if not isinstance(payload, dict):
        return
    clean_run_id = _to_int(run_id or payload.get("supabase_run_id"))
    with SUPABASE_REPORT_CACHE_LOCK:
        if latest:
            # Latest-report reads are the common path. Do not also retain the same
            # multi-megabyte payload in the historical cache.
            SUPABASE_REPORT_CACHE["by_id"].clear()
            SUPABASE_REPORT_CACHE["latest"] = payload
            SUPABASE_REPORT_CACHE["latest_expires_at"] = time.time() + SUPABASE_LATEST_CACHE_SECONDS
        elif clean_run_id:
            cache = SUPABASE_REPORT_CACHE["by_id"]
            cache[str(clean_run_id)] = payload
            while len(cache) > SUPABASE_REPORT_CACHE_MAX_ITEMS:
                cache.pop(next(iter(cache)))


def _cached_supabase_report_payload(run_id=None, latest=False):
    with SUPABASE_REPORT_CACHE_LOCK:
        if latest:
            if time.time() < SUPABASE_REPORT_CACHE.get("latest_expires_at", 0):
                return SUPABASE_REPORT_CACHE.get("latest")
            SUPABASE_REPORT_CACHE["latest"] = None
            SUPABASE_REPORT_CACHE["latest_expires_at"] = 0
            return None
        clean_run_id = _to_int(run_id)
        return SUPABASE_REPORT_CACHE["by_id"].get(str(clean_run_id)) if clean_run_id else None


def _clear_supabase_report_cache():
    """Drop large report objects before a scheduled scrape starts."""
    with SUPABASE_REPORT_CACHE_LOCK:
        SUPABASE_REPORT_CACHE["latest"] = None
        SUPABASE_REPORT_CACHE["latest_expires_at"] = 0
        SUPABASE_REPORT_CACHE["by_id"].clear()


def _load_previous_account_metrics(accounts=None):
    """Load recent non-zero account totals without retaining complete historical reports."""
    wanted = {
        str(account or "").strip().lstrip("@").lower()
        for account in (accounts or [])
        if str(account or "").strip().lstrip("@")
    }
    metrics = {}

    def merge(account, followers=0, hearts=0):
        key = str(account or "").strip().lstrip("@").lower()
        if not key or (wanted and key not in wanted):
            return
        entry = metrics.setdefault(key, {})
        follower_value = _to_int(followers)
        heart_value = _to_int(hearts)
        if follower_value > 0 and _to_int(entry.get("followers")) <= 0:
            entry["followers"] = follower_value
        if heart_value > 0 and _to_int(entry.get("hearts")) <= 0:
            entry["hearts"] = heart_value

    if SUPABASE_ENABLED and _supabase_configured():
        try:
            rows = _supabase_request(
                "GET",
                "/account_snapshots"
                "?select=account,followers,hearts,run_id"
                "&followers=gt.0"
                "&order=run_id.desc"
                "&limit=5000",
                timeout=30,
            )
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    merge(
                        row.get("account"),
                        row.get("followers"),
                        row.get("hearts"),
                    )
        except Exception:
            pass

    followers_complete = bool(wanted) and all(
        _to_int(metrics.get(account, {}).get("followers")) > 0
        for account in wanted
    )
    if followers_complete:
        return metrics

    checked = set()
    for directory in (REPORTS_DIR, PUBLIC_REPORTS_DIR):
        path = os.path.abspath(os.path.join(directory, "latest_report.json"))
        if path in checked or not os.path.isfile(path):
            continue
        checked.add(path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for row in payload.get("summary") or []:
                if not isinstance(row, dict):
                    continue
                merge(
                    _summary_account(row),
                    row.get("粉丝") or row.get("Followers / 粉丝"),
                    row.get("点赞") or row.get("Likes / 点赞"),
                )
            del payload
        except Exception:
            continue
    return metrics


def _drop_supabase_report_cache(run_ids):
    clean_ids = {str(_to_int(run_id)) for run_id in (run_ids or []) if _to_int(run_id)}
    if not clean_ids:
        return
    with SUPABASE_REPORT_CACHE_LOCK:
        for run_id in clean_ids:
            SUPABASE_REPORT_CACHE["by_id"].pop(run_id, None)
        latest = SUPABASE_REPORT_CACHE.get("latest")
        if isinstance(latest, dict) and str(_to_int(latest.get("supabase_run_id"))) in clean_ids:
            SUPABASE_REPORT_CACHE["latest"] = None
            SUPABASE_REPORT_CACHE["latest_expires_at"] = 0


def _save_report_to_supabase(payload, cache_payload=True):
    status = {
        "enabled": bool(SUPABASE_ENABLED),
        "configured": _supabase_configured(),
        "ok": False,
    }
    if not SUPABASE_ENABLED:
        status["ok"] = True
        status["skipped"] = "SUPABASE_ENABLED is off"
        return status
    if not _supabase_configured():
        status["error"] = "SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured"
        return status
    try:
        summary_rows = payload.get("summary") if isinstance(payload, dict) else []
        drama_rows = payload.get("dramas_detail") if isinstance(payload, dict) else []
        if not isinstance(summary_rows, list):
            summary_rows = []
        if not isinstance(drama_rows, list):
            drama_rows = []
        generated_at = _supabase_timestamp(payload.get("generated_at"), fallback_now=True)
        run_id = _supabase_insert_report_run(payload)
        if not run_id:
            raise RuntimeError("Supabase did not return report_runs.id")
        account_rows = _supabase_account_rows(summary_rows, generated_at)
        accounts_written = _supabase_upsert("accounts", account_rows, "account")
        del account_rows
        account_snapshot_rows = _supabase_account_snapshot_rows(run_id, summary_rows)
        account_snapshots_written = _supabase_upsert("account_snapshots", account_snapshot_rows, "run_id,account")
        del account_snapshot_rows
        drama_table_rows = _supabase_drama_rows(drama_rows, generated_at)
        dramas_written = _supabase_upsert("dramas", drama_table_rows, "drama_key")
        del drama_table_rows
        gc.collect()
        drama_snapshot_rows = _supabase_drama_snapshot_rows(run_id, drama_rows)
        drama_snapshots_written = _supabase_upsert("drama_snapshots", drama_snapshot_rows, "run_id,drama_key")
        del drama_snapshot_rows
        gc.collect()
        status.update({
            "ok": True,
            "run_id": run_id,
            "accounts": accounts_written,
            "account_snapshots": account_snapshots_written,
            "dramas": dramas_written,
            "drama_snapshots": drama_snapshots_written,
        })
        if cache_payload:
            cached_payload = dict(payload)
            cached_payload["storage_source"] = "supabase"
            cached_payload["supabase_run_id"] = run_id
            _cache_supabase_report_payload(cached_payload, run_id=run_id, latest=True)
    except Exception as exc:
        status["error"] = str(exc)
    return status


def _supabase_report_read_enabled():
    return bool(SUPABASE_ENABLED and SUPABASE_REPORT_READ and _supabase_configured())


def _supabase_report_payload_from_row(row):
    if not isinstance(row, dict):
        raise RuntimeError("Supabase report row is invalid")
    raw = row.get("raw")
    if not isinstance(raw, dict):
        raise RuntimeError("Supabase report raw payload is empty")
    payload = dict(raw)
    payload.setdefault("generated_at", row.get("generated_at") or row.get("created_at"))
    payload.setdefault("accounts", _to_int(row.get("accounts_count")))
    payload.setdefault("dramas", _to_int(row.get("dramas_count")))
    payload["storage_source"] = "supabase"
    payload["supabase_run_id"] = row.get("id")
    return payload


def _compact_report_payload(payload):
    if not isinstance(payload, dict):
        return payload

    def first(row, keys, default=""):
        for key in keys:
            if row.get(key) not in (None, ""):
                return row.get(key)
        return default

    summaries = []
    for row in payload.get("summary") or []:
        if not isinstance(row, dict):
            continue
        summaries.append({
            "a": first(row, ("账号", "Account / 账号")),
            "n": first(row, ("昵称", "Nickname / 昵称")),
            "f": _to_int(first(row, ("粉丝", "Followers / 粉丝"), 0)),
            "h": _to_int(first(row, ("点赞", "Likes / 点赞"), 0)),
            "d": _to_int(first(row, ("短剧数", "Dramas / 短剧数"), 0)),
            "e": _to_int(first(row, ("总集数", "Episodes / 总集数"), 0)),
            "v": _to_int(first(row, ("累计观看", "Total Views / 累计观看"), 0)),
        })

    dramas = []
    for row in payload.get("dramas_detail") or []:
        if not isinstance(row, dict):
            continue
        dramas.append({
            "a": first(row, ("账号", "Account / 账号")),
            "n": first(row, ("昵称", "Nickname / 昵称")),
            "id": first(row, ("Drama ID / 短剧ID", "短剧ID", "Drama ID")),
            "en": first(row, ("English Title / 英文剧名", "短剧名")),
            "cn": first(row, ("Chinese Title / 中文剧名", "中文剧名")),
            "p": first(row, ("Publish Time / 发布时间", "发布时间")),
            "e": _to_int(first(row, ("Episodes / 集数", "集数"), 0)),
            "v": _to_int(first(row, ("Views / 观看数", "累计观看"), 0)),
            "et": first(row, ("English Themes / 英文题材",)),
            "ct": first(row, ("Chinese Themes / 中文题材",)),
        })

    return {
        "generated_at": payload.get("generated_at") or "",
        "accounts": _to_int(payload.get("accounts")),
        "dramas": _to_int(payload.get("dramas")),
        "storage_source": payload.get("storage_source") or "",
        "supabase_run_id": payload.get("supabase_run_id"),
        "summary": summaries,
        "dramas_detail": dramas,
    }


def _supabase_latest_report_payload():
    if not _supabase_report_read_enabled():
        raise RuntimeError("Supabase report read is not configured")
    cached = _cached_supabase_report_payload(latest=True)
    if cached:
        return cached
    rows = _supabase_request(
        "GET",
        "/report_runs?select=id,generated_at,accounts_count,dramas_count,created_at,raw"
        "&source=neq.%s&order=generated_at.desc&limit=1" % urllib.parse.quote(ADMIN_CATALOG_SOURCE, safe=""),
        timeout=20,
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Supabase report_runs is empty")
    payload = _supabase_report_payload_from_row(rows[0])
    _cache_supabase_report_payload(payload, run_id=rows[0].get("id"), latest=True)
    return payload


def _supabase_report_payload_by_id(run_id):
    if not _supabase_report_read_enabled():
        raise RuntimeError("Supabase report read is not configured")
    run_id = _to_int(run_id)
    if not run_id:
        raise RuntimeError("missing Supabase report id")
    cached = _cached_supabase_report_payload(run_id=run_id)
    if cached:
        return cached
    rows = _supabase_request(
        "GET",
        "/report_runs?select=id,generated_at,accounts_count,dramas_count,created_at,raw"
        "&source=neq.%s&id=eq.%s&limit=1"
        % (urllib.parse.quote(ADMIN_CATALOG_SOURCE, safe=""), run_id),
        timeout=20,
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Supabase report not found")
    payload = _supabase_report_payload_from_row(rows[0])
    _cache_supabase_report_payload(payload, run_id=run_id)
    return payload


def _supabase_report_history(limit=None):
    if not _supabase_report_read_enabled():
        raise RuntimeError("Supabase report read is not configured")
    limit = max(1, min(_to_int(limit) or SUPABASE_REPORT_HISTORY_LIMIT, 200))
    cutoff = urllib.parse.quote(_retention_cutoff().isoformat(timespec="seconds"), safe="")
    rows = _supabase_request(
        "GET",
        "/report_runs?select=id,generated_at,accounts_count,dramas_count,created_at"
        "&source=neq.%s&generated_at=gte.%s&order=generated_at.desc&limit=%s"
        % (urllib.parse.quote(ADMIN_CATALOG_SOURCE, safe=""), cutoff, limit),
        timeout=20,
    )
    reports = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        run_id = row.get("id")
        reports.append({
            "name": "supabase_report_%s.json" % run_id,
            "title": row.get("generated_at") or row.get("created_at") or ("run " + str(run_id)),
            "generated_at": row.get("generated_at") or row.get("created_at") or "",
            "modified": row.get("created_at") or row.get("generated_at") or "",
            "accounts": _to_int(row.get("accounts_count")),
            "dramas": _to_int(row.get("dramas_count")),
            "path": "/supabase/report?id=%s" % urllib.parse.quote(str(run_id)),
            "source": "supabase",
        })
    return reports


def _cleanup_supabase_report_history(now=None):
    status = {
        "enabled": bool(SUPABASE_ENABLED),
        "configured": _supabase_configured(),
        "ok": False,
        "retention_days": REPORT_RETENTION_DAYS,
        "deleted_runs": 0,
    }
    if not SUPABASE_ENABLED:
        status.update({"ok": True, "skipped": "SUPABASE_ENABLED is off"})
        return status
    if not _supabase_configured():
        status["error"] = "SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured"
        return status
    cutoff = urllib.parse.quote(_retention_cutoff(now).isoformat(timespec="seconds"), safe="")
    try:
        while True:
            rows = _supabase_request(
                "GET",
                "/report_runs?select=id&source=neq.%s&generated_at=lt.%s&order=id.asc&limit=%s"
                % (urllib.parse.quote(ADMIN_CATALOG_SOURCE, safe=""), cutoff, SUPABASE_BATCH_SIZE),
                timeout=20,
            )
            run_ids = [_to_int(row.get("id")) for row in (rows or []) if isinstance(row, dict)]
            run_ids = [run_id for run_id in run_ids if run_id]
            if not run_ids:
                break
            in_filter = "in.(%s)" % ",".join(str(run_id) for run_id in run_ids)
            _supabase_request("DELETE", "/account_snapshots?run_id=" + in_filter, prefer="return=minimal")
            _supabase_request("DELETE", "/drama_snapshots?run_id=" + in_filter, prefer="return=minimal")
            _supabase_request("DELETE", "/report_runs?id=" + in_filter, prefer="return=minimal")
            _drop_supabase_report_cache(run_ids)
            status["deleted_runs"] += len(run_ids)
            if len(run_ids) < SUPABASE_BATCH_SIZE:
                break
        status["ok"] = True
    except Exception as exc:
        status["error"] = str(exc)
    return status


def _write_report_bundle(rows, drama_rows, errors):
    now = datetime.datetime.now(BEIJING_TZ)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    summary_name = "scheduled_report_%s.csv" % stamp
    drama_name = "scheduled_dramas_%s.csv" % stamp
    json_name = "scheduled_report_%s.json" % stamp
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "accounts": len(rows),
        "dramas": len(drama_rows),
        "errors": errors,
        "summary": rows,
        "dramas_detail": drama_rows,
    }
    _write_csv_file(summary_name, SUMMARY_COLUMNS, rows)
    _write_csv_file(drama_name, DRAMA_COLUMNS, drama_rows)
    _write_json_file(json_name, payload)
    _write_csv_file("latest_report.csv", SUMMARY_COLUMNS, rows)
    _write_csv_file("latest_dramas.csv", DRAMA_COLUMNS, drama_rows)
    _write_json_file("latest_report.json", payload)
    return {
        "summary": summary_name,
        "dramas": drama_name,
        "json": json_name,
        "latest_summary": "latest_report.csv",
        "latest_dramas": "latest_dramas.csv",
        "latest_json": "latest_report.json",
    }, payload


def _scrape_scheduled_accounts(accounts, scrape=None):
    """Scrape accounts concurrently while preserving configured account order."""
    clean_accounts = list(accounts or [])
    if not clean_accounts:
        return [], [], []
    scrape = scrape or _scrape_account
    worker_count = min(SCHEDULE_ACCOUNT_WORKERS, len(clean_accounts))
    ordered_results = [None] * len(clean_accounts)
    ordered_errors = [None] * len(clean_accounts)
    active_accounts = {}
    progress_lock = threading.Lock()

    LAST_JOB.update({
        "accounts_completed": 0,
        "accounts_succeeded": 0,
        "accounts_failed": 0,
        "current_account": "",
        "current_accounts": [],
        "account_workers": worker_count,
        "tikhub_rps_limit": TIKHUB_RPS_LIMIT,
    })

    def update_active():
        current = [active_accounts[key] for key in sorted(active_accounts)]
        LAST_JOB["current_accounts"] = current
        LAST_JOB["current_account"] = current[0] if current else ""

    def run_one(index, uid):
        with progress_lock:
            active_accounts[index] = uid
            update_active()
        try:
            summary, dramas = scrape(uid)
            return index, summary, dramas, None
        except Exception as exc:
            return index, None, None, {"account": uid, "error": str(exc)}
        finally:
            with progress_lock:
                active_accounts.pop(index, None)
                update_active()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="scheduled-account",
    ) as pool:
        next_index = 0
        pending = {}

        def submit_next():
            nonlocal next_index
            if next_index >= len(clean_accounts):
                return
            index = next_index
            next_index += 1
            pending[pool.submit(run_one, index, clean_accounts[index])] = index

        for _ in range(worker_count):
            submit_next()
        while pending:
            completed, _waiting = concurrent.futures.wait(
                tuple(pending),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in completed:
                pending.pop(future, None)
                index, summary, dramas, error = future.result()
                if error:
                    ordered_errors[index] = error
                else:
                    ordered_results[index] = (summary, dramas or [])
                with progress_lock:
                    LAST_JOB["accounts_completed"] += 1
                    if error:
                        LAST_JOB["accounts_failed"] += 1
                    else:
                        LAST_JOB["accounts_succeeded"] += 1
                submit_next()

    rows, drama_rows = [], []
    for index, result in enumerate(ordered_results):
        if not result:
            continue
        summary, dramas = result
        rows.append(summary)
        drama_rows.extend(dramas)
        ordered_results[index] = None
    errors = [error for error in ordered_errors if error]
    return rows, drama_rows, errors


def _run_scheduled_job(accounts):
    previous_metrics = _load_previous_account_metrics(accounts)
    _clear_supabase_report_cache()
    gc.collect()
    LAST_JOB.update({
        "phase": "accounts",
        "accounts_total": len(accounts),
        "accounts_completed": 0,
        "accounts_succeeded": 0,
        "accounts_failed": 0,
        "current_account": "",
        "current_accounts": [],
        "account_workers": min(SCHEDULE_ACCOUNT_WORKERS, max(1, len(accounts))),
        "tikhub_rps_limit": TIKHUB_RPS_LIMIT,
    })
    def scrape_with_previous_metrics(uid):
        key = str(uid or "").strip().lstrip("@").lower()
        return _scrape_account(uid, previous_metrics.get(key))

    rows, drama_rows, errors = _scrape_scheduled_accounts(
        accounts,
        scrape=scrape_with_previous_metrics,
    )
    previous_metrics.clear()
    LAST_JOB.update({"phase": "reports", "current_account": "", "current_accounts": []})
    try:
        _save_drama_detail_cache()
    except Exception:
        pass
    _release_drama_detail_cache()
    gc.collect()
    files, report_payload = _write_report_bundle(rows, drama_rows, errors)
    generated_at = report_payload.get("generated_at") or datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    drama_count = len(drama_rows)
    runtime_retention = _cleanup_runtime_report_files()
    LAST_JOB["phase"] = "supabase"
    supabase = _save_report_to_supabase(report_payload, cache_payload=False)
    del report_payload
    gc.collect()
    supabase_retention = _cleanup_supabase_report_history()
    LAST_JOB["phase"] = "episode_history"
    try:
        episode_history = _save_scheduled_episode_history(drama_rows)
    except Exception as exc:
        episode_history = {
            "enabled": bool(SCHEDULE_SAVE_EPISODE_HISTORY),
            "ok": False,
            "error": str(exc),
            "directory": "reports/episode_history",
        }
    del drama_rows
    gc.collect()
    LAST_JOB["phase"] = "finishing"
    return {
        "ok": True,
        "generated_at": generated_at,
        "accounts_requested": len(accounts),
        "accounts_ok": len(rows),
        "accounts_failed": len(errors),
        "dramas": drama_count,
        "files": files,
        "supabase": supabase,
        "retention": {
            "days": REPORT_RETENTION_DAYS,
            "runtime": runtime_retention,
            "supabase": supabase_retention,
        },
        "episode_history": episode_history,
        "errors": errors,
    }


def _execute_scheduled_job(accounts, trigger="manual", scheduled_slot=""):
    LAST_JOB.update({"running": True, "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                     "finished_at": None, "result": None, "error": None,
                     "phase": "starting", "accounts_total": len(accounts),
                     "accounts_completed": 0, "accounts_succeeded": 0,
                     "accounts_failed": 0, "current_account": "", "current_accounts": [],
                     "account_workers": min(SCHEDULE_ACCOUNT_WORKERS, max(1, len(accounts))),
                     "tikhub_rps_limit": TIKHUB_RPS_LIMIT,
                     "trigger": trigger, "scheduled_slot": scheduled_slot})
    try:
        result = _run_scheduled_job(accounts)
        LAST_JOB.update({"running": False, "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
                         "result": result, "error": None, "phase": "completed",
                         "current_account": "", "current_accounts": []})
        return result
    except Exception as exc:
        LAST_JOB.update({"running": False, "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
                         "result": None, "error": str(exc), "phase": "failed",
                         "current_account": "", "current_accounts": []})
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


def _episode_history_account_name(uid):
    account = str(uid or "").strip().lstrip("@").lower()
    safe = re.sub(r"[^a-z0-9._-]+", "_", account).strip("._-")
    return safe or "unknown"


def _episode_history_paths(uid):
    name = _episode_history_account_name(uid) + ".json"
    return (
        os.path.join(DRAMA_EPISODE_HISTORY_DIR, name),
        os.path.join(PUBLIC_DRAMA_EPISODE_HISTORY_DIR, name),
    )


def _read_drama_episode_history(uid):
    data = {}
    for path in _episode_history_paths(uid):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            break
        except Exception:
            data = {}
    items = data.get("items") if isinstance(data, dict) else {}
    if not isinstance(items, dict):
        items = {}
    return {"version": 2, "account": _episode_history_account_name(uid), "items": items}


def _write_drama_episode_history(history, uid):
    items = history.get("items") if isinstance(history, dict) else {}
    if not isinstance(items, dict):
        items = {}
    os.makedirs(DRAMA_EPISODE_HISTORY_DIR, exist_ok=True)
    path = _episode_history_paths(uid)[0]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({
            "version": 2,
            "account": _episode_history_account_name(uid),
            "items": items,
        }, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


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


def _prune_episode_history(history, now_ms):
    items = history.get("items") if isinstance(history, dict) else None
    if not isinstance(items, dict):
        return 0
    deleted = 0
    for key in list(items):
        entry = items.get(key)
        if not isinstance(entry, dict):
            items.pop(key, None)
            deleted += 1
            continue
        points = _trim_episode_history_points(entry.get("points") or [], now_ms)
        if not points:
            items.pop(key, None)
            deleted += 1
            continue
        entry["points"] = points
    return deleted


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
        history = _read_drama_episode_history(uid)
        pruned = _prune_episode_history(history, now_ms)
        metrics, changed, _recorded = _record_episode_history_entries(history, uid, drama_id, episodes, now_ms, now_text, collect_metrics=True)
        if changed or pruned:
            try:
                _write_drama_episode_history(history, uid)
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


def _safe_download_name(value, limit=90):
    text = _to_text(value, limit) or "video"
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    return text or "video"


def _unique_archive_name(name, used):
    base, ext = os.path.splitext(name)
    candidate = name
    idx = 2
    while candidate in used:
        candidate = "%s-%s%s" % (base, idx, ext)
        idx += 1
    used.add(candidate)
    return candidate


def _drama_zip_filename(uid, drama_id):
    parts = [part for part in (_safe_download_name(uid or "account", 36), _clean_drama_id(drama_id) or "drama") if part]
    return _safe_download_name("-".join(parts), 120) + "-videos.zip"


def _attachment_header(filename):
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "videos.zip"
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (
        ascii_name,
        urllib.parse.quote(filename),
    )


def _episode_direct_play_source(item, started=None, uid=""):
    video_id = _get_video_id(item)
    if video_id:
        source = _get_video_play_source(video_id, started, uid)
        if source.get("url"):
            return source
    # Historical reports can contain a usable direct address.  Keep it as the
    # final fallback, but resolve by video ID first so expired CDN URLs are not
    # preferred over a fresh address.
    url = _video_play_url_from_item(item)
    return {"url": url, "tt_chain_token": "", "endpoint": "report"} if url else {}


def _episode_direct_play_url(item, started=None, uid=""):
    return _episode_direct_play_source(item, started, uid).get("url", "")


def _media_extension(content_type, url):
    parsed_ext = os.path.splitext(urllib.parse.urlparse(url or "").path)[1].lower()
    if parsed_ext in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
        return parsed_ext
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype == "video/webm":
        return ".webm"
    if ctype in ("video/quicktime", "video/mov"):
        return ".mov"
    if ctype in ("video/mp4", "application/octet-stream", "binary/octet-stream"):
        return ".mp4"
    return ".mp4"


def _open_video_download(source, uid, range_header=""):
    if isinstance(source, str):
        source = {"url": source}
    source = dict(source or {})
    url = source.get("url") or ""
    if not url:
        raise TikHubError("play source not found")
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": TIKTOK_HOST + ("/@" + uid if uid else "/"),
        "User-Agent": DEFAULT_UA,
    }
    cookie = _clean_cookie_header(source.get("cookie"))
    token = _to_text(source.get("tt_chain_token"), 4096).strip()
    if token:
        cookie = _merge_cookie_headers(cookie, "tt_chain_token=" + token)
    if cookie:
        headers["Cookie"] = cookie
    if range_header:
        headers["Range"] = _to_text(range_header, 200)
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=DRAMA_ZIP_DOWNLOAD_TIMEOUT_SECONDS)


def _download_episode_to_temp(index, item, account, started=None):
    summary = _drama_episode_summary(item, account, index)
    video_id = summary.get("video_id") or ""
    episode_no = summary.get("episode_no") or index
    title = summary.get("title") or ("Episode %s" % episode_no)
    temp_path = ""
    try:
        source = _episode_direct_play_source(item, started, account)
        direct_url = source.get("url") or ""
        if not direct_url:
            raise TikHubError("play source not found")
        with _open_video_download(source, account) as resp:
            final_url = resp.geturl() or direct_url
            ext = _media_extension(resp.headers.get("Content-Type", ""), final_url)
            size = 0
            with tempfile.NamedTemporaryFile(prefix="thr_video_", suffix=ext, delete=False) as temp_file:
                temp_path = temp_file.name
                while True:
                    chunk = resp.read(DRAMA_ZIP_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if DRAMA_ZIP_MAX_VIDEO_BYTES and size > DRAMA_ZIP_MAX_VIDEO_BYTES:
                        raise TikHubError("video exceeds DRAMA_ZIP_MAX_VIDEO_BYTES")
                    temp_file.write(chunk)
        return {
            "ok": True,
            "index": index,
            "episode": episode_no,
            "video_id": video_id,
            "title": title,
            "ext": ext,
            "bytes": size,
            "temp_path": temp_path,
        }
    except Exception as exc:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return {
            "ok": False,
            "index": index,
            "episode": episode_no,
            "video_id": video_id,
            "title": title,
            "error": str(exc),
        }


def _local_download_output_dir(account, drama_id):
    stamp = datetime.datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")
    name = "%s-%s-%s" % (account or "account", _clean_drama_id(drama_id) or "drama", stamp)
    return os.path.join(LOCAL_DOWNLOAD_DIR, _safe_download_name(name, 140))


def _powershell_single_quoted(value):
    return "'" + _to_text(value).replace("'", "''") + "'"


def _build_drama_local_downloader_script(account, drama_id, episode_items, origin=""):
    used_names = set()
    items, errors = [], []
    origin = (origin or PUBLIC_BASE_URL).rstrip("/")
    ticket_expires = int(time.time() + VIDEO_MEDIA_TICKET_TTL_SECONDS)

    def prepare(index, item):
        summary = _drama_episode_summary(item, account, index)
        video_id = summary.get("video_id") or ""
        episode_no = summary.get("episode_no") or index
        title = summary.get("title") or ("Episode %s" % episode_no)
        try:
            direct_url = _video_media_ticket_url(account, video_id, ticket_expires, origin)
            if not direct_url:
                raise TikHubError("protected download URL could not be prepared")
            base = "%03d-%s" % (_to_int(episode_no) or index, _safe_download_name(title, 70))
            if video_id:
                base += "-" + _safe_download_name(video_id[-10:], 12)
            return True, base + _media_extension("", direct_url), {
                "episode": episode_no,
                "video_id": video_id,
                "title": title,
                "url": direct_url,
                "work_url": _build_tiktok_video_url(account, video_id),
            }
        except Exception as exc:
            return False, "", {
                "episode": episode_no,
                "video_id": video_id,
                "title": title,
                "error": str(exc),
            }

    max_workers = min(LOCAL_DOWNLOAD_WORKERS, max(1, len(episode_items)))
    prepared = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(prepare, index, item) for index, item in enumerate(episode_items, 1)]
        for future in concurrent.futures.as_completed(futures):
            prepared.append(future.result())
    prepared.sort(key=lambda result: _to_int(result[2].get("episode")) or 0)
    for ok, base_name, result in prepared:
        if ok:
            result["file"] = _unique_archive_name(base_name, used_names)
            items.append(result)
        else:
            errors.append(result)
    stamp = datetime.datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")
    folder_prefix = _safe_download_name("%s-%s" % (account or "account", _clean_drama_id(drama_id) or "drama"), 100)
    folder_name = _safe_download_name("%s-%s" % (folder_prefix, stamp), 120)
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    error_payload = json.dumps(errors, ensure_ascii=False, indent=2)
    script = """# TikHub drama local downloader
# Generated: %s
# Account: @%s
# Drama ID: %s

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
$ua = %s
$referer = %s
$baseDir = if ($env:TIKHUB_DOWNLOAD_BASE_DIR) { $env:TIKHUB_DOWNLOAD_BASE_DIR } else { $PSScriptRoot }
$folderPrefix = %s
$folderName = %s
$existingDir = Get-ChildItem -LiteralPath $baseDir -Directory -Filter ($folderPrefix + "-*") -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$downloadDir = if ($null -ne $existingDir) { $existingDir.FullName } else { Join-Path $baseDir $folderName }
$itemsJson = @'
%s
'@
$errorsJson = @'
%s
'@
$items = $itemsJson | ConvertFrom-Json
$errors = $errorsJson | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
$itemsJson | Out-File -LiteralPath (Join-Path $downloadDir "download_items.json") -Encoding utf8
$errorsJson | Out-File -LiteralPath (Join-Path $downloadDir "prepare_errors.json") -Encoding utf8
$useSessionCookies = $false
$cookieJar = Join-Path $env:TEMP ("tikhub_tiktok_" + [guid]::NewGuid().ToString("N") + ".txt")
$ytDlp = ""

function Install-VerifiedYtDlp {
  $toolsDir = Join-Path $baseDir ".tikhub-tools"
  $binary = Join-Path $toolsDir "yt-dlp.exe"
  if (Test-Path -LiteralPath $binary) { return $binary }

  New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
  $download = $binary + ".download"
  $checksums = Join-Path $toolsDir "SHA2-256SUMS"
  Write-Host "Preparing the verified local downloader..."
  Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $download
  Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS" -OutFile $checksums
  $line = Get-Content -LiteralPath $checksums | Where-Object { $_ -match "\\syt-dlp\\.exe$" } | Select-Object -First 1
  if (-not $line) {
    Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
    throw "Could not verify yt-dlp.exe: checksum entry missing"
  }
  $expected = (($line.Trim() -split "\\s+")[0]).ToUpperInvariant()
  $actual = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToUpperInvariant()
  if ($actual -ne $expected) {
    Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
    throw "Could not verify yt-dlp.exe: SHA256 mismatch"
  }
  Move-Item -LiteralPath $download -Destination $binary -Force
  return $binary
}

function Test-TikTokCookieFile {
  param([string]$Path)
  if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
  try {
    $header = Get-Content -LiteralPath $Path -TotalCount 2 -ErrorAction Stop
    if (-not (@($header) -match '^# (?:HTTP Cookie File|Netscape HTTP Cookie File)')) { return $false }
    return [bool](Select-String -LiteralPath $Path -Pattern 'tiktok\\.com\\t' -Quiet -ErrorAction Stop)
  } catch {
    return $false
  }
}

function Copy-TikTokCookiesOnly {
  param([string]$Source, [string]$Destination)
  if (-not (Test-TikTokCookieFile $Source)) { return $false }
  $cookieRows = @(Get-Content -LiteralPath $Source | Where-Object {
    $_ -match '^(?:#HttpOnly_)?[^\\t]*tiktok\\.com\\t'
  })
  if ($cookieRows.Count -eq 0) { return $false }
  @('# Netscape HTTP Cookie File', '# TikHub temporary TikTok-only cookie jar') + $cookieRows |
    Set-Content -LiteralPath $Destination -Encoding ASCII
  return $true
}

function Get-DownloadsFolders {
  $folders = @()
  try {
    $shellFolders = Get-ItemProperty -LiteralPath 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\User Shell Folders'
    $configured = $shellFolders.'{374DE290-123F-4565-9164-39C4925E467B}'
    if ($configured) { $folders += [Environment]::ExpandEnvironmentVariables($configured) }
  } catch {}
  if ($env:USERPROFILE) { $folders += (Join-Path $env:USERPROFILE 'Downloads') }
  return @($folders | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)
}

function Find-TikTokCookieExport {
  $explicit = $env:TIKHUB_TIKTOK_COOKIE_FILE
  if ($explicit -and (Test-TikTokCookieFile $explicit)) { return $explicit }
  $folders = @($baseDir) + @(Get-DownloadsFolders)
  $files = @()
  foreach ($folder in ($folders | Select-Object -Unique)) {
    $files += @(Get-ChildItem -LiteralPath $folder -File -Filter '*.txt' -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match '(?i)(tiktok.*cookie|cookie.*tiktok|^cookies(?: \\(\\d+\\))?\\.txt$)' })
  }
  foreach ($file in ($files | Sort-Object LastWriteTime -Descending)) {
    if (Test-TikTokCookieFile $file.FullName) { return $file.FullName }
  }
  return ""
}

function Test-TikTokSession {
  param([string]$CookieFile, [string]$WorkUrl)
  if (-not (Test-TikTokCookieFile $CookieFile)) { return $false }
  & $ytDlp --cookies $CookieFile --user-agent $ua --add-header 'Referer:https://www.tiktok.com/' --no-playlist --simulate --no-warnings $WorkUrl | Out-Host
  return ($LASTEXITCODE -eq 0)
}

function Try-FirefoxTikTokSession {
  param([string]$WorkUrl)
  Remove-Item -LiteralPath $cookieJar -Force -ErrorAction SilentlyContinue
  & $ytDlp --cookies-from-browser firefox --cookies $cookieJar --user-agent $ua --add-header 'Referer:https://www.tiktok.com/' --no-playlist --simulate --no-warnings $WorkUrl | Out-Host
  return ($LASTEXITCODE -eq 0 -and (Test-TikTokCookieFile $cookieJar))
}

function Import-TikTokCookieExport {
  param([string]$Source, [string]$WorkUrl)
  Remove-Item -LiteralPath $cookieJar -Force -ErrorAction SilentlyContinue
  if (-not (Copy-TikTokCookiesOnly $Source $cookieJar)) { return $false }
  return (Test-TikTokSession $cookieJar $WorkUrl)
}

function Request-ChromeTikTokCookieExport {
  param([string]$WorkUrl)
  $extensionUrl = 'https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc'
  Write-Host ""
  Write-Host "当前 Chrome 使用“应用绑定加密”保护登录信息。"
  Write-Host "本机下载器不能直接解密，因此关闭 Chrome 后重试也不会生效。"
  Write-Host ""
  Write-Host "请为已经登录的 Chrome 完成一次本地授权（登录失效后再更新）："
  Write-Host "1. 在自动打开的页面安装推荐扩展：Get cookies.txt LOCALLY。"
  Write-Host "2. 回到已经登录的 tiktok.com 页面。"
  Write-Host "3. 点击该扩展，只导出当前 TikTok 站点的 cookies。"
  Write-Host "4. 保持默认 .txt 文件名保存到下载目录，然后返回此窗口。"
  try { Start-Process $extensionUrl } catch { Write-Host $extensionUrl }
  $null = Read-Host "导出完成后按 Enter 继续"
  $export = Find-TikTokCookieExport
  if (-not $export) { return $false }
  Write-Host ("Found TikTok cookie export: " + $export)
  return (Import-TikTokCookieExport $export $WorkUrl)
}

$firstItem = @($items) | Select-Object -First 1
if ($null -ne $firstItem) {
  $probe = Join-Path $env:TEMP ("tikhub_probe_" + [guid]::NewGuid().ToString("N") + ".tmp")
  & curl.exe -sS -L --fail-with-body --connect-timeout 20 --max-time 60 --range 0-0 -A $ua -e $referer -o $probe $firstItem.url
  $probeCode = $LASTEXITCODE
  $probeDetail = ""
  if (Test-Path -LiteralPath $probe) {
    if ($probeCode -ne 0) {
      try { $probeDetail = Get-Content -LiteralPath $probe -Raw -Encoding UTF8 } catch {}
    }
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
  }
  if ($probeCode -ne 0 -and $probeDetail -match '"error_code"\\s*:\\s*"tiktok_login_required"') {
    $useSessionCookies = $true
    $ytDlp = Install-VerifiedYtDlp
    Write-Host ""
    Write-Host "这部短剧需要使用你已经登录的 TikTok 会话。"
    $sessionReady = $false
    $export = Find-TikTokCookieExport
    if ($export) {
      Write-Host ("Trying the latest local TikTok cookie export: " + $export)
      $sessionReady = Import-TikTokCookieExport $export $firstItem.work_url
    }
    if (-not $sessionReady) {
      Write-Host "正在检查本机 Firefox 的 TikTok 登录状态……"
      $sessionReady = Try-FirefoxTikTokSession $firstItem.work_url
    }
    if (-not $sessionReady) { $sessionReady = Request-ChromeTikTokCookieExport $firstItem.work_url }
    if (-not $sessionReady) {
      Remove-Item -LiteralPath $cookieJar -Force -ErrorAction SilentlyContinue
      throw "没有找到可用的 TikTok 登录会话。请重新导出 tiktok.com cookies 后再运行。"
    }
    Write-Host "TikTok login session loaded. Starting all episodes..."
  } elseif ($probeCode -ne 0) {
    if ($probeDetail.Length -gt 500) { $probeDetail = $probeDetail.Substring(0, 500) }
    throw ("Download preflight failed" + $(if ($probeDetail) { ": " + $probeDetail } else { "" }))
  }
}

$maxJobs = if ($useSessionCookies) { 2 } else { 4 }
$jobs = @()

function Receive-FinishedJobs {
  param([array]$CurrentJobs)
  $running = @()
  foreach ($job in $CurrentJobs) {
    if ($job.State -eq "Running") {
      $running += $job
    } else {
      Receive-Job -Job $job -ErrorAction Continue 2>&1 | Out-Host
      Remove-Job -Job $job
    }
  }
  return $running
}

foreach ($item in $items) {
  while (($jobs | Where-Object { $_.State -eq "Running" }).Count -ge $maxJobs) {
    $jobs = @(Receive-FinishedJobs $jobs)
    Start-Sleep -Milliseconds 500
  }
  $job = Start-Job -ArgumentList $item.url, $item.work_url, $item.file, $downloadDir, $ua, $referer, $useSessionCookies, $ytDlp, $cookieJar -ScriptBlock {
    param($url, $workUrl, $file, $dir, $ua, $referer, $useSessionCookies, $ytDlp, $cookieJar)
    $out = Join-Path $dir $file
    if (Test-Path -LiteralPath $out) {
      Write-Host "Skip existing: $file"
      return
    }
    Write-Host "Downloading: $file"
    if ($useSessionCookies) {
      & $ytDlp --cookies $cookieJar --user-agent $ua --add-header 'Referer:https://www.tiktok.com/' --no-playlist --no-overwrites --no-part --format best --output $out $workUrl
    } else {
      & curl.exe -sS -L --fail-with-body --retry 3 --retry-delay 2 --connect-timeout 20 -A $ua -e $referer -o $out $url
    }
    if ($LASTEXITCODE -ne 0) {
      $detail = ""
      if (Test-Path -LiteralPath $out) {
        try { $detail = (Get-Content -LiteralPath $out -Raw -Encoding UTF8).Trim() } catch {}
        Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
      }
      if ($detail.Length -gt 500) { $detail = $detail.Substring(0, 500) }
      throw ("Download failed: " + $file + $(if ($detail) { " - " + $detail } else { "" }))
    }
  }
  $jobs = @($jobs) + @($job)
}

while ($jobs.Count -gt 0) {
  $jobs = @(Receive-FinishedJobs $jobs)
  Start-Sleep -Milliseconds 500
}

if (Test-Path -LiteralPath $cookieJar) {
  Remove-Item -LiteralPath $cookieJar -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Done: $downloadDir"
if ($errors.Count -gt 0) {
  Write-Host "Some play URLs were not prepared. See prepare_errors.json"
}
Read-Host "Press Enter to close"
""" % (
        datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        account,
        _clean_drama_id(drama_id),
        _powershell_single_quoted(DEFAULT_UA),
        _powershell_single_quoted(TIKTOK_HOST + ("/@" + account if account else "/")),
        _powershell_single_quoted(folder_prefix),
        _powershell_single_quoted(folder_name),
        payload,
        error_payload,
    )
    return script.encode("utf-8-sig"), len(items), errors


def _wrap_powershell_downloader_cmd(script_bytes):
    script_text = script_bytes.decode("utf-8-sig")
    wrapper = """@echo off
setlocal
set "SCRIPT_PATH=%~f0"
set "TIKHUB_DOWNLOAD_BASE_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $lines=Get-Content -LiteralPath $env:SCRIPT_PATH -Encoding UTF8; $idx=[Array]::IndexOf($lines,'__POWERSHELL__'); if($idx -lt 0){throw 'script marker not found'}; $temp=Join-Path $env:TEMP ('tikhub_downloader_' + [guid]::NewGuid().ToString('N') + '.ps1'); $lines[($idx+1)..($lines.Count-1)] | Set-Content -LiteralPath $temp -Encoding UTF8; & powershell -NoProfile -ExecutionPolicy Bypass -File $temp; $code=$LASTEXITCODE; Remove-Item -LiteralPath $temp -ErrorAction SilentlyContinue; exit $code"
if errorlevel 1 (
  echo.
  echo Downloader failed. Please copy the error above and send it to Codex.
) else (
  echo.
  echo Downloader finished.
)
pause
exit /b
__POWERSHELL__
"""
    # cmd.exe does not parse LF-only batch files reliably.  In particular,
    # downloaded wrappers can be split in the middle of SET/PowerShell lines,
    # causing the console to exit before the final PAUSE is reached.  Keep the
    # wrapper BOM-free, but normalize every line (including the embedded
    # PowerShell payload) to native Windows CRLF endings.
    combined = (wrapper + script_text).replace("\r\n", "\n").replace("\r", "\n")
    return combined.replace("\n", "\r\n").encode("utf-8")


def _local_download_job_snapshot(job_id):
    with LOCAL_DOWNLOAD_JOBS_LOCK:
        job = LOCAL_DOWNLOAD_JOBS.get(job_id)
        if not job:
            return None
        snapshot = dict(job)
        snapshot["files"] = list(job.get("files") or [])
        snapshot["errors"] = list(job.get("errors") or [])
        return snapshot


def _prune_local_download_jobs_locked():
    if len(LOCAL_DOWNLOAD_JOBS) <= LOCAL_DOWNLOAD_MAX_JOBS:
        return
    removable = [
        (job.get("started_ts", 0), job_id)
        for job_id, job in LOCAL_DOWNLOAD_JOBS.items()
        if not job.get("running")
    ]
    removable.sort()
    overflow = len(LOCAL_DOWNLOAD_JOBS) - LOCAL_DOWNLOAD_MAX_JOBS
    for _started, job_id in removable[:overflow]:
        LOCAL_DOWNLOAD_JOBS.pop(job_id, None)


def _move_temp_file(src, dst):
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass
    with open(src, "rb") as source, open(dst, "wb") as target:
        while True:
            chunk = source.read(DRAMA_ZIP_CHUNK_BYTES)
            if not chunk:
                break
            target.write(chunk)
    try:
        os.remove(src)
    except OSError:
        pass


def _record_local_download_result(job_id, result, used_names, output_dir):
    episode_no = result.get("episode") or result.get("index") or ""
    video_id = result.get("video_id") or ""
    title = result.get("title") or ""
    if not result.get("ok"):
        with LOCAL_DOWNLOAD_JOBS_LOCK:
            job = LOCAL_DOWNLOAD_JOBS.get(job_id)
            if job:
                job["processed"] = job.get("processed", 0) + 1
                job["errors"].append({
                    "episode": episode_no,
                    "video_id": video_id,
                    "title": title,
                    "error": result.get("error") or "download failed",
                })
        return
    temp_path = result.get("temp_path") or ""
    try:
        base = "%03d-%s" % (_to_int(episode_no) or result.get("index") or 0, _safe_download_name(title, 70))
        if video_id:
            base += "-" + _safe_download_name(video_id[-10:], 12)
        filename = _unique_archive_name(base + (result.get("ext") or ".mp4"), used_names)
        dest = os.path.join(output_dir, filename)
        _move_temp_file(temp_path, dest)
        file_meta = {
            "episode": episode_no,
            "video_id": video_id,
            "title": title,
            "file": filename,
            "path": dest,
            "bytes": result.get("bytes") or 0,
        }
        with LOCAL_DOWNLOAD_JOBS_LOCK:
            job = LOCAL_DOWNLOAD_JOBS.get(job_id)
            if job:
                job["processed"] = job.get("processed", 0) + 1
                job["downloaded"] = job.get("downloaded", 0) + 1
                job["files"].append(file_meta)
    except Exception as exc:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        with LOCAL_DOWNLOAD_JOBS_LOCK:
            job = LOCAL_DOWNLOAD_JOBS.get(job_id)
            if job:
                job["processed"] = job.get("processed", 0) + 1
                job["errors"].append({
                    "episode": episode_no,
                    "video_id": video_id,
                    "title": title,
                    "error": str(exc),
                })


def _write_local_download_manifest(job_id):
    snapshot = _local_download_job_snapshot(job_id)
    if not snapshot:
        return ""
    manifest = {
        "id": snapshot.get("id"),
        "uid": snapshot.get("uid"),
        "drama_id": snapshot.get("drama_id"),
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "output_dir": snapshot.get("output_dir"),
        "total": snapshot.get("total"),
        "downloaded": snapshot.get("downloaded"),
        "errors": snapshot.get("errors"),
        "files": snapshot.get("files"),
    }
    path = os.path.join(snapshot.get("output_dir") or LOCAL_DOWNLOAD_DIR, "manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _run_local_download_job(job_id, account, drama_id, episode_items, output_dir):
    started = time.time()
    try:
        os.makedirs(output_dir, exist_ok=True)
        used_names = set()
        max_workers = min(LOCAL_DOWNLOAD_WORKERS, max(1, len(episode_items)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_download_episode_to_temp, index, item, account, started)
                for index, item in enumerate(episode_items, 1)
            ]
            for future in concurrent.futures.as_completed(futures):
                _record_local_download_result(job_id, future.result(), used_names, output_dir)
        finished = datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
        with LOCAL_DOWNLOAD_JOBS_LOCK:
            job = LOCAL_DOWNLOAD_JOBS.get(job_id)
            if job:
                job["running"] = False
                job["finished_at"] = finished
                job["workers"] = max_workers
                job["files"].sort(key=lambda item: _to_int(item.get("episode")) or 0)
                job["errors"].sort(key=lambda item: _to_int(item.get("episode")) or 0)
        manifest_path = _write_local_download_manifest(job_id)
        with LOCAL_DOWNLOAD_JOBS_LOCK:
            job = LOCAL_DOWNLOAD_JOBS.get(job_id)
            if job:
                job["manifest_path"] = manifest_path
    except Exception as exc:
        with LOCAL_DOWNLOAD_JOBS_LOCK:
            job = LOCAL_DOWNLOAD_JOBS.get(job_id)
            if job:
                job["running"] = False
                job["finished_at"] = datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
                job["error"] = str(exc)


def _start_local_download_job(account, drama_id, episode_items):
    job_id = uuid.uuid4().hex[:12]
    output_dir = _local_download_output_dir(account, drama_id)
    now = datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    job = {
        "id": job_id,
        "uid": account,
        "drama_id": _clean_drama_id(drama_id),
        "running": True,
        "started_at": now,
        "started_ts": time.time(),
        "finished_at": None,
        "output_dir": output_dir,
        "total": len(episode_items),
        "processed": 0,
        "downloaded": 0,
        "workers": min(LOCAL_DOWNLOAD_WORKERS, max(1, len(episode_items))),
        "files": [],
        "errors": [],
        "error": "",
        "manifest_path": "",
    }
    with LOCAL_DOWNLOAD_JOBS_LOCK:
        LOCAL_DOWNLOAD_JOBS[job_id] = job
        _prune_local_download_jobs_locked()
    thread = threading.Thread(
        target=_run_local_download_job,
        args=(job_id, account, drama_id, episode_items, output_dir),
        daemon=True,
    )
    thread.start()
    return job_id


def _render_local_download_status_page(job):
    running = bool(job.get("running"))
    refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    status_text = "下载中" if running else ("已完成" if not job.get("error") else "失败")
    total = _to_int(job.get("total"))
    processed = _to_int(job.get("processed"))
    downloaded = _to_int(job.get("downloaded"))
    error_count = len(job.get("errors") or [])
    progress = int((processed / total) * 100) if total else 0
    file_rows = []
    for item in (job.get("files") or [])[-30:]:
        file_rows.append("<li>第%s集 · %s · %s</li>" % (
            _html_text(item.get("episode") or ""),
            _html_text(item.get("file") or ""),
            _html_text(_format_chinese_count(item.get("bytes") or 0) + "B"),
        ))
    error_rows = []
    for item in (job.get("errors") or [])[-20:]:
        error_rows.append("<li>第%s集 · %s</li>" % (
            _html_text(item.get("episode") or ""),
            _html_text(item.get("error") or ""),
        ))
    body = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
%s
<title>本机下载任务</title>
<style>
body{margin:0;background:#f5f7fb;color:#172033;font:14px/1.55 Arial,"Microsoft YaHei",sans-serif}.wrap{max-width:860px;margin:28px auto;padding:0 18px}.panel{background:#fff;border:1px solid #e6eaf1;border-radius:8px;box-shadow:0 10px 28px rgba(31,41,55,.08);padding:22px}h1{margin:0 0 8px;font-size:22px}.muted{color:#667085}.bar{height:10px;background:#e9edf5;border-radius:999px;overflow:hidden;margin:18px 0}.fill{height:100%%;background:#405cff;width:%s%%}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:18px 0}.stat{border:1px solid #e6eaf1;border-radius:8px;padding:12px}.label{color:#667085;font-size:12px}.value{font-weight:800;font-size:18px}.path{word-break:break-all;background:#f8fafc;border:1px solid #e6eaf1;border-radius:8px;padding:10px}.btn{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:7px 12px;border-radius:6px;text-decoration:none;border:1px solid #e6eaf1;background:#fff;color:#172033;margin-right:8px}.primary{background:#405cff;border-color:#405cff;color:#fff}ul{padding-left:20px}.err{color:#e11d48}@media(max-width:640px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <section class="panel">
    <h1>本机下载任务：%s</h1>
    <div class="muted">@%s · 短剧ID %s · workers %s</div>
    <div class="bar"><div class="fill"></div></div>
    <div class="grid">
      <div class="stat"><div class="label">进度</div><div class="value">%s/%s</div></div>
      <div class="stat"><div class="label">成功 / 失败</div><div class="value">%s / %s</div></div>
    </div>
    <div class="label">保存目录</div>
    <div class="path">%s</div>
    <p class="muted">这个页面会自动刷新；下载完成后视频已经在上面的本机目录里。</p>
    <p><a class="btn primary" href="javascript:location.reload()">刷新状态</a><a class="btn" href="/">返回报表</a></p>
    %s
    %s
  </section>
</div>
</body>
</html>""" % (
        refresh,
        max(0, min(100, progress)),
        _html_text(status_text),
        _html_text(job.get("uid") or ""),
        _html_text(job.get("drama_id") or ""),
        _html_text(job.get("workers") or ""),
        processed,
        total,
        downloaded,
        error_count,
        _html_text(job.get("output_dir") or ""),
        ("<h2>最近保存</h2><ul>%s</ul>" % "".join(file_rows)) if file_rows else "",
        ("<h2 class=\"err\">失败记录</h2><ul>%s</ul>" % "".join(error_rows)) if error_rows else "",
    )
    return body.encode("utf-8")


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


def _flush_scheduled_episode_history(uid, fetched, now_ms, now_text):
    """Persist a small episode-history batch and release it immediately."""
    if not fetched:
        return
    with DRAMA_EPISODE_HISTORY_LOCK:
        history = _read_drama_episode_history(uid)
        changed = bool(_prune_episode_history(history, now_ms))
        for drama_id, episodes in fetched:
            _metrics, item_changed, _recorded = _record_episode_history_entries(
                history, uid, drama_id, episodes, now_ms, now_text, collect_metrics=False
            )
            changed = changed or item_changed
        if changed:
            _write_drama_episode_history(history, uid)
    fetched.clear()


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
        "directory": "reports/episode_history",
    }
    if not SCHEDULE_SAVE_EPISODE_HISTORY:
        return result
    targets = _scheduled_episode_history_targets(drama_rows)
    result["targets_total"] = len(targets)
    if not targets:
        return result
    started = time.time()
    max_episodes = SCHEDULE_EPISODE_HISTORY_MAX_EPISODES or DRAMA_LINK_MAX_EPISODES
    now_ms = int(time.time() * 1000)
    now_text = datetime.datetime.fromtimestamp(now_ms / 1000.0, BEIJING_TZ).isoformat(timespec="seconds")
    targets_by_account = {}
    for target in targets:
        targets_by_account.setdefault(target["uid"], []).append(target)
    for uid, account_targets in targets_by_account.items():
        if _runtime_exceeded(started):
            result["runtime_limited"] = True
            break
        fetched = []
        for target in account_targets:
            if _runtime_exceeded(started):
                result["runtime_limited"] = True
                break
            drama_id = target["drama_id"]
            result["attempted"] += 1
            try:
                items = _get_drama_episode_items(drama_id, uid, started=started, limit=max_episodes)
                episodes = [_drama_episode_summary(item, uid, idx + 1) for idx, item in enumerate(items)]
                if episodes:
                    fetched.append((drama_id, episodes))
                    result["dramas_ok"] += 1
                    result["episodes_saved"] += len(episodes)
                    if len(fetched) >= SCHEDULE_EPISODE_HISTORY_FLUSH_DRAMAS:
                        _flush_scheduled_episode_history(uid, fetched, now_ms, now_text)
                else:
                    result["dramas_empty"] += 1
            except Exception as exc:
                if len(result["errors"]) < 20:
                    result["errors"].append({"uid": uid, "drama_id": drama_id, "error": str(exc)})
            if SCHEDULE_EPISODE_HISTORY_DELAY_MS:
                time.sleep(SCHEDULE_EPISODE_HISTORY_DELAY_MS / 1000.0)
        _flush_scheduled_episode_history(uid, fetched, now_ms, now_text)
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
        play_link = '<a class="link primary" href="%s" data-private-action="open-json">&#25773;&#25918;&#28304;</a>' % _html_text(episode["play_url"]) if episode.get("play_url") else '<span class="muted">&#26080;</span>'
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
    zip_url = "/drama-link?" + urllib.parse.urlencode({
        "uid": account,
        "drama_id": _clean_drama_id(drama_id),
        "target": "zip",
    })
    local_url = "/drama-link?" + urllib.parse.urlencode({
        "uid": account,
        "drama_id": _clean_drama_id(drama_id),
        "target": "local_save",
    })
    local_script_url = "/drama-link?" + urllib.parse.urlencode({
        "uid": account,
        "drama_id": _clean_drama_id(drama_id),
        "target": "local_script",
    })
    body = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>&#30701;&#21095;&#35270;&#39057;&#21015;&#34920;</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#667085;--line:#e6eaf1;--head:#1d2633;--bg:#f5f7fb;--blue:#405cff;--ok:#067647;--err:#b42318}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 Arial,"Microsoft YaHei",sans-serif}.wrap{max-width:1180px;margin:24px auto;padding:0 18px}.panel{background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 10px 28px rgba(31,41,55,.08);overflow:hidden}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 20px;border-bottom:1px solid var(--line)}h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px}.tools{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}.secret-box{display:flex;align-items:center;gap:6px}.secret-input{width:170px;min-height:34px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;color:var(--ink);background:#fff}.btn,.link{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:7px 12px;border-radius:6px;text-decoration:none;border:1px solid var(--line);white-space:nowrap;cursor:pointer}.btn{color:var(--ink);background:#fff}.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}.link.primary{background:var(--blue);border-color:var(--blue);color:#fff}.link.ghost{color:var(--blue);background:#fff;margin-left:8px}.private-status{display:none;padding:9px 20px;border-bottom:1px solid var(--line);font-size:13px}.private-status.show{display:block}.private-status.ok{color:var(--ok);background:#ecfdf3}.private-status.err{color:var(--err);background:#fef3f2}table{width:100%%;border-collapse:collapse;table-layout:fixed}thead th{background:var(--head);color:#fff;text-align:left;font-weight:700;padding:12px 14px}tbody td{border-top:1px solid var(--line);padding:12px 14px;vertical-align:top}tbody tr:nth-child(even){background:#fafbfe}.idx{width:64px;color:var(--muted)}.time-col{width:154px}.view-col{width:96px}.growth-col{width:118px}.action-col{width:178px}.name{font-weight:700;word-break:break-word}.meta{margin-top:3px;color:var(--muted);font-size:12px;word-break:break-all}.growth-cell{font-weight:800;white-space:nowrap}.growth-up{display:inline-flex;align-items:center;gap:4px;color:#e11d48}.growth-flat,.growth-empty{color:#98a2b3;font-weight:700}.trend-arrow{font-size:16px;line-height:1}.actions{white-space:nowrap}.empty{text-align:center;color:var(--muted);padding:34px}.note{color:var(--muted);font-size:12px;margin-top:12px}@media(max-width:760px){.top{display:block}.tools{justify-content:flex-start;margin-top:12px}.secret-box{width:100%%}.secret-input{flex:1;min-width:0}table{table-layout:auto}.hide-sm{display:none}.actions{white-space:normal}.link.ghost{margin-left:0;margin-top:6px}}
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
        <div class="secret-box">
          <input class="secret-input" id="backendSecretInput" type="password" autocomplete="off" spellcheck="false" placeholder="先填写后端密码">
          <button class="btn" id="saveBackendSecret" type="button">&#20445;&#23384;&#23494;&#30721;</button>
        </div>
        <a class="btn" href="/" target="_self">&#36820;&#22238;&#25253;&#34920;</a>
        <a class="btn primary" href="%s" data-private-action="download">&#19979;&#36733;&#20840;&#37096; ZIP</a>
        <a class="btn" href="%s" data-private-action="download">&#26412;&#26426;&#19979;&#36733;&#33050;&#26412;</a>
        <a class="btn" href="%s" data-private-action="navigate">&#20445;&#23384;&#21040;&#26412;&#26426;</a>
        <a class="btn" href="%s" target="_blank" rel="noopener">JSON</a>
      </div>
    </div>
    <div class="private-status" id="privateStatus" role="status" aria-live="polite"></div>
    <table>
      <thead><tr><th class="idx">&#38598;&#25968;</th><th>&#35270;&#39057;</th><th class="hide-sm time-col">&#21457;&#24067;&#26102;&#38388;</th><th class="hide-sm view-col">&#35266;&#30475;</th><th class="hide-sm growth-col">&#21608;&#19978;&#28072;&#28909;&#24230;</th><th class="hide-sm growth-col">&#26376;&#19978;&#28072;&#28909;&#24230;</th><th class="action-col">&#38142;&#25509;</th></tr></thead>
      <tbody>%s</tbody>
    </table>
  </section>
  <div class="note">&#25773;&#25918;&#28304;&#21644;&#19979;&#36733;&#24517;&#39035;&#20808;&#22635;&#20889;&#27491;&#30830;&#30340;&#21518;&#31471;&#23494;&#30721;&#12290;&#23494;&#30721;&#21482;&#36890;&#36807;&#35831;&#27714;&#22836;&#21457;&#36865;&#65292;&#19981;&#20250;&#25918;&#20837; URL&#12290;&#8220;&#26412;&#26426;&#19979;&#36733;&#33050;&#26412;&#8221;&#21487;&#20197;&#22312;&#32447;&#19978;&#39029;&#38754;&#29983;&#25104;&#65292;&#8220;&#20445;&#23384;&#21040;&#26412;&#26426;&#8221;&#38656;&#35201;&#29992;&#26412;&#22320;&#20195;&#29702;&#25171;&#24320;&#39029;&#38754;&#12290;</div>
</div>
<script>
(function(){
  "use strict";
  var storageKey="thr_backend_secret";
  var input=document.getElementById("backendSecretInput");
  var saveButton=document.getElementById("saveBackendSecret");
  var statusBox=document.getElementById("privateStatus");

  function savedSecret(){
    try{return String(localStorage.getItem(storageKey)||"").trim();}catch(_){return "";}
  }
  function currentSecret(){
    return String((input&&input.value)||savedSecret()||"").trim();
  }
  function storeSecret(value){
    try{
      if(value)localStorage.setItem(storageKey,value);
      else localStorage.removeItem(storageKey);
    }catch(_){}
  }
  function setStatus(message,ok){
    statusBox.textContent=message||"";
    statusBox.className="private-status"+(message?" show "+(ok?"ok":"err"):"");
  }
  function requireSecret(){
    var secret=currentSecret();
    if(!secret){
      setStatus("请先填写并保存后端密码，再打开播放源或下载。",false);
      if(input)input.focus();
      return "";
    }
    storeSecret(secret);
    return secret;
  }
  function responseFilename(response,fallback){
    var disposition=response.headers.get("Content-Disposition")||"";
    var encoded=/filename\\*=UTF-8''([^;]+)/i.exec(disposition);
    var plain=/filename="?([^";]+)"?/i.exec(disposition);
    try{return decodeURIComponent((encoded&&encoded[1])||(plain&&plain[1])||fallback);}catch(_){return fallback;}
  }
  async function responseError(response){
    var text=await response.text();
    try{
      var payload=JSON.parse(text);
      if(payload&&payload.error)return payload.error;
    }catch(_){}
    return text||("HTTP "+response.status);
  }
  async function runPrivateAction(anchor){
    var secret=requireSecret();
    if(!secret)return;
    var action=anchor.getAttribute("data-private-action")||"open-json";
    var requestUrl=new URL(anchor.getAttribute("href"),location.href);
    if(action==="open-json")requestUrl.searchParams.set("redirect","0");
    var popup=(action==="open-json"||action==="navigate")?window.open("about:blank","_blank"):null;
    if(popup)popup.opener=null;
    anchor.setAttribute("aria-busy","true");
    setStatus(action==="download"?"正在准备下载…":"正在验证密码并获取最新地址…",true);
    try{
      var response=await fetch(requestUrl.toString(),{
        headers:{"X-Schedule-Secret":secret},
        cache:"no-store"
      });
      if(!response.ok)throw new Error(await responseError(response));
      if(action==="open-json"){
        var data=await response.json();
        if(!data||!data.url)throw new Error("后端没有返回可用播放地址");
        var mediaUrl=new URL(data.url,location.href).toString();
        if(popup)popup.location.replace(mediaUrl);
        else window.open(mediaUrl,"_blank","noopener");
        setStatus("密码验证成功，已打开最新播放源。",true);
      }else if(action==="navigate"){
        if(popup)popup.location.replace(response.url);
        else window.open(response.url,"_blank","noopener");
        setStatus("密码验证成功，已打开本机保存页面。",true);
      }else{
        var blob=await response.blob();
        var blobUrl=URL.createObjectURL(blob);
        var downloader=document.createElement("a");
        downloader.href=blobUrl;
        downloader.download=responseFilename(response,"download");
        document.body.appendChild(downloader);
        downloader.click();
        downloader.remove();
        setTimeout(function(){URL.revokeObjectURL(blobUrl);},60000);
        setStatus("密码验证成功，下载已经开始。",true);
      }
    }catch(error){
      if(popup)popup.close();
      var message=String((error&&error.message)||error||"请求失败");
      if(/secret|403/i.test(message))message="后端密码错误或后端尚未配置密码。";
      setStatus(message,false);
    }finally{
      anchor.removeAttribute("aria-busy");
    }
  }

  if(input)input.value=savedSecret();
  if(saveButton)saveButton.addEventListener("click",function(){
    var secret=String((input&&input.value)||"").trim();
    storeSecret(secret);
    setStatus(secret?"后端密码已保存在当前浏览器，可以播放和下载。":"后端密码已清除。",!!secret);
  });
  document.addEventListener("click",function(event){
    var anchor=event.target.closest("[data-private-action]");
    if(!anchor)return;
    event.preventDefault();
    runPrivateAction(anchor);
  });
})();
</script>
</body>
</html>""" % (
        _html_text(account),
        len(episodes),
        _html_text(_clean_drama_id(drama_id)),
        _html_text(zip_url),
        _html_text(local_script_url),
        _html_text(local_url),
        _html_text(source_url),
        "\n".join(rows),
    )
    return body.encode("utf-8")

def _parse_internal_schedule_times(value=None):
    raw = INTERNAL_SCHEDULE_TIMES if value is None else value
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item).strip() for item in raw]
    else:
        parts = [item.strip() for item in re.split(r"[,;\s]+", str(raw or ""))]
    minutes = set()
    for part in parts:
        if not part:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            minutes.add(hour * 60 + minute)
    return tuple(sorted(minutes))


def _as_beijing_datetime(value=None):
    if value is None:
        return datetime.datetime.now(BEIJING_TZ)
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def _internal_schedule_slot(now=None, schedule_minutes=None):
    now = _as_beijing_datetime(now) or datetime.datetime.now(BEIJING_TZ)
    schedule_minutes = tuple(schedule_minutes or _parse_internal_schedule_times())
    if not schedule_minutes:
        return None
    candidates = []
    for day_offset in (0, -1):
        day = (now + datetime.timedelta(days=day_offset)).date()
        for minute_of_day in schedule_minutes:
            candidate = datetime.datetime.combine(
                day,
                datetime.time(minute_of_day // 60, minute_of_day % 60),
                tzinfo=BEIJING_TZ,
            )
            if candidate <= now:
                candidates.append(candidate)
    return max(candidates) if candidates else None


def _next_internal_schedule_slot(now=None, schedule_minutes=None):
    now = _as_beijing_datetime(now) or datetime.datetime.now(BEIJING_TZ)
    schedule_minutes = tuple(schedule_minutes or _parse_internal_schedule_times())
    if not schedule_minutes:
        return None
    candidates = []
    for day_offset in (0, 1):
        day = (now + datetime.timedelta(days=day_offset)).date()
        for minute_of_day in schedule_minutes:
            candidate = datetime.datetime.combine(
                day,
                datetime.time(minute_of_day // 60, minute_of_day % 60),
                tzinfo=BEIJING_TZ,
            )
            if candidate > now:
                candidates.append(candidate)
    return min(candidates) if candidates else None


def _latest_persisted_report_at():
    if _supabase_report_read_enabled():
        rows = _supabase_request(
            "GET",
            "/report_runs?select=generated_at,created_at&source=neq.%s"
            "&order=generated_at.desc&limit=1"
            % urllib.parse.quote(ADMIN_CATALOG_SOURCE, safe=""),
            timeout=20,
        )
        if isinstance(rows, list) and rows:
            row = rows[0] if isinstance(rows[0], dict) else {}
            return _as_beijing_datetime(row.get("generated_at") or row.get("created_at"))
        return None

    for directory in (REPORTS_DIR, PUBLIC_REPORTS_DIR):
        path = os.path.join(directory, "latest_report.json")
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            generated_at = payload.get("generated_at") if isinstance(payload, dict) else ""
            parsed = _as_beijing_datetime(generated_at)
            if parsed:
                return parsed
        except Exception:
            continue
    return None


def _internal_scheduler_status(now=None):
    now = _as_beijing_datetime(now) or datetime.datetime.now(BEIJING_TZ)
    schedule_minutes = _parse_internal_schedule_times()
    next_slot = _next_internal_schedule_slot(now, schedule_minutes)
    with INTERNAL_SCHEDULER_STATE_LOCK:
        state = dict(INTERNAL_SCHEDULER_STATE)
    state.update({
        "enabled": bool(INTERNAL_SCHEDULER_ENABLED),
        "timezone": "Asia/Shanghai",
        "times": ["%02d:%02d" % divmod(item, 60) for item in schedule_minutes],
        "poll_seconds": INTERNAL_SCHEDULER_POLL_SECONDS,
        "trigger_window_seconds": INTERNAL_SCHEDULER_TRIGGER_WINDOW_SECONDS,
        "max_attempts": INTERNAL_SCHEDULER_MAX_ATTEMPTS,
        "next_run_at": next_slot.isoformat(timespec="seconds") if next_slot else "",
    })
    state.pop("next_retry_timestamp", None)
    state.pop("needs_report_check", None)
    return state


def _record_internal_scheduler_failure(slot_key, message, now=None, increment_attempt=False):
    now = _as_beijing_datetime(now) or datetime.datetime.now(BEIJING_TZ)
    with INTERNAL_SCHEDULER_STATE_LOCK:
        if INTERNAL_SCHEDULER_STATE.get("active_slot") != slot_key:
            return
        if increment_attempt:
            INTERNAL_SCHEDULER_STATE["attempts"] += 1
        attempts = int(INTERNAL_SCHEDULER_STATE.get("attempts") or 0)
        INTERNAL_SCHEDULER_STATE["running_slot"] = ""
        INTERNAL_SCHEDULER_STATE["last_error"] = str(message or "scheduled job failed")
        if attempts < INTERNAL_SCHEDULER_MAX_ATTEMPTS:
            delay = min(INTERNAL_SCHEDULER_RETRY_SECONDS * (2 ** max(0, attempts - 1)), 3600)
            retry_at = now + datetime.timedelta(seconds=delay)
            INTERNAL_SCHEDULER_STATE["next_retry_timestamp"] = retry_at.timestamp()
            INTERNAL_SCHEDULER_STATE["next_retry_at"] = retry_at.isoformat(timespec="seconds")
        else:
            INTERNAL_SCHEDULER_STATE["next_retry_timestamp"] = 0.0
            INTERNAL_SCHEDULER_STATE["next_retry_at"] = ""


def _internal_scheduled_result_error(result):
    if not isinstance(result, dict):
        return "scheduled job returned an invalid result"
    if _to_int(result.get("accounts_requested")) and not _to_int(result.get("accounts_ok")):
        return "all scheduled accounts failed"
    supabase = result.get("supabase")
    if isinstance(supabase, dict) and supabase.get("configured") and not supabase.get("ok"):
        return "Supabase save failed: %s" % (supabase.get("error") or "unknown error")
    return ""


def _start_internal_scheduled_job(slot, now=None):
    now = _as_beijing_datetime(now) or datetime.datetime.now(BEIJING_TZ)
    slot = _as_beijing_datetime(slot)
    if not slot:
        return False
    slot_key = slot.isoformat(timespec="minutes")
    if not SERVER_API_KEY:
        _record_internal_scheduler_failure(slot_key, "TIKHUB_API_KEY is not configured", now, increment_attempt=True)
        return False
    accounts, source = _configured_schedule_accounts()
    if not accounts:
        _record_internal_scheduler_failure(slot_key, "schedule account pool is empty", now, increment_attempt=True)
        return False
    if not JOB_LOCK.acquire(blocking=False):
        with INTERNAL_SCHEDULER_STATE_LOCK:
            if INTERNAL_SCHEDULER_STATE.get("active_slot") == slot_key:
                retry_at = now + datetime.timedelta(seconds=INTERNAL_SCHEDULER_POLL_SECONDS)
                INTERNAL_SCHEDULER_STATE["needs_report_check"] = True
                INTERNAL_SCHEDULER_STATE["next_retry_timestamp"] = retry_at.timestamp()
                INTERNAL_SCHEDULER_STATE["next_retry_at"] = retry_at.isoformat(timespec="seconds")
        return False

    with INTERNAL_SCHEDULER_STATE_LOCK:
        if INTERNAL_SCHEDULER_STATE.get("active_slot") != slot_key:
            JOB_LOCK.release()
            return False
        INTERNAL_SCHEDULER_STATE["attempts"] += 1
        INTERNAL_SCHEDULER_STATE["running_slot"] = slot_key
        INTERNAL_SCHEDULER_STATE["last_triggered_at"] = now.isoformat(timespec="seconds")
        INTERNAL_SCHEDULER_STATE["last_error"] = ""
        INTERNAL_SCHEDULER_STATE["next_retry_timestamp"] = 0.0
        INTERNAL_SCHEDULER_STATE["next_retry_at"] = ""

    def background():
        try:
            result = _execute_scheduled_job(
                accounts,
                trigger="internal_scheduler",
                scheduled_slot=slot_key,
            )
            result_error = _internal_scheduled_result_error(result)
            if result_error:
                LAST_JOB.update({"phase": "failed", "error": result_error})
                raise RuntimeError(result_error)
            completed_at = datetime.datetime.now(BEIJING_TZ)
            with INTERNAL_SCHEDULER_STATE_LOCK:
                if INTERNAL_SCHEDULER_STATE.get("active_slot") == slot_key:
                    INTERNAL_SCHEDULER_STATE.update({
                        "completed_slot": slot_key,
                        "running_slot": "",
                        "last_completed_at": completed_at.isoformat(timespec="seconds"),
                        "last_error": "",
                        "next_retry_at": "",
                        "next_retry_timestamp": 0.0,
                        "needs_report_check": False,
                    })
        except Exception as exc:
            _record_internal_scheduler_failure(slot_key, str(exc))
        finally:
            JOB_LOCK.release()

    try:
        threading.Thread(
            target=background,
            name="internal-report-scheduler-job",
            daemon=True,
        ).start()
    except Exception as exc:
        JOB_LOCK.release()
        _record_internal_scheduler_failure(slot_key, str(exc))
        return False
    return True


def _internal_scheduler_tick(now=None):
    if not INTERNAL_SCHEDULER_ENABLED:
        return False
    now = _as_beijing_datetime(now) or datetime.datetime.now(BEIJING_TZ)
    schedule_minutes = _parse_internal_schedule_times()
    slot = _internal_schedule_slot(now, schedule_minutes)
    if not slot:
        return False
    slot_key = slot.isoformat(timespec="minutes")
    slot_age_seconds = max(0.0, (now - slot).total_seconds())

    with INTERNAL_SCHEDULER_STATE_LOCK:
        if INTERNAL_SCHEDULER_STATE.get("active_slot") != slot_key:
            INTERNAL_SCHEDULER_STATE.update({
                "active_slot": slot_key,
                "skipped_slot": "",
                "attempts": 0,
                "last_error": "",
                "next_retry_at": "",
                "next_retry_timestamp": 0.0,
                "needs_report_check": True,
            })
            if slot_age_seconds > INTERNAL_SCHEDULER_TRIGGER_WINDOW_SECONDS:
                INTERNAL_SCHEDULER_STATE.update({
                    "skipped_slot": slot_key,
                    "needs_report_check": False,
                })
                return False
        if INTERNAL_SCHEDULER_STATE.get("skipped_slot") == slot_key:
            return False
        if INTERNAL_SCHEDULER_STATE.get("completed_slot") == slot_key:
            return False
        if INTERNAL_SCHEDULER_STATE.get("running_slot") == slot_key:
            return False
        if now.timestamp() < float(INTERNAL_SCHEDULER_STATE.get("next_retry_timestamp") or 0):
            return False
        if int(INTERNAL_SCHEDULER_STATE.get("attempts") or 0) >= INTERNAL_SCHEDULER_MAX_ATTEMPTS:
            return False
        needs_report_check = bool(INTERNAL_SCHEDULER_STATE.get("needs_report_check"))

    if needs_report_check:
        try:
            latest_report_at = _latest_persisted_report_at()
        except Exception as exc:
            retry_at = now + datetime.timedelta(seconds=max(60, INTERNAL_SCHEDULER_POLL_SECONDS))
            with INTERNAL_SCHEDULER_STATE_LOCK:
                if INTERNAL_SCHEDULER_STATE.get("active_slot") == slot_key:
                    INTERNAL_SCHEDULER_STATE.update({
                        "last_error": "latest report check failed: %s" % exc,
                        "needs_report_check": True,
                        "next_retry_timestamp": retry_at.timestamp(),
                        "next_retry_at": retry_at.isoformat(timespec="seconds"),
                    })
            return False
        with INTERNAL_SCHEDULER_STATE_LOCK:
            if INTERNAL_SCHEDULER_STATE.get("active_slot") != slot_key:
                return False
            INTERNAL_SCHEDULER_STATE["needs_report_check"] = False
            if latest_report_at and latest_report_at >= slot:
                INTERNAL_SCHEDULER_STATE.update({
                    "completed_slot": slot_key,
                    "last_completed_at": latest_report_at.isoformat(timespec="seconds"),
                    "last_error": "",
                })
                return False

    return _start_internal_scheduled_job(slot, now)


def _internal_scheduler_loop():
    while True:
        try:
            _internal_scheduler_tick()
        except Exception as exc:
            with INTERNAL_SCHEDULER_STATE_LOCK:
                INTERNAL_SCHEDULER_STATE["last_error"] = "scheduler tick failed: %s" % exc
        time.sleep(INTERNAL_SCHEDULER_POLL_SECONDS)


def _start_internal_scheduler():
    if not INTERNAL_SCHEDULER_ENABLED:
        return False
    schedule_minutes = _parse_internal_schedule_times()
    if not schedule_minutes:
        with INTERNAL_SCHEDULER_STATE_LOCK:
            INTERNAL_SCHEDULER_STATE["last_error"] = "INTERNAL_SCHEDULE_TIMES has no valid HH:MM values"
        return False
    with INTERNAL_SCHEDULER_STATE_LOCK:
        if INTERNAL_SCHEDULER_STATE.get("thread_started"):
            return False
        INTERNAL_SCHEDULER_STATE["thread_started"] = True
    try:
        threading.Thread(
            target=_internal_scheduler_loop,
            name="internal-report-scheduler",
            daemon=True,
        ).start()
    except Exception as exc:
        with INTERNAL_SCHEDULER_STATE_LOCK:
            INTERNAL_SCHEDULER_STATE["thread_started"] = False
            INTERNAL_SCHEDULER_STATE["last_error"] = str(exc)
        return False
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
            accounts, source = _configured_schedule_accounts()
            if not self._schedule_secret_matches(qs):
                job = {
                    "running": bool(LAST_JOB.get("running")),
                    "started_at": LAST_JOB.get("started_at"),
                    "finished_at": LAST_JOB.get("finished_at"),
                }
            self._send_json(200, {
                "ok": True,
                "job": job,
                "internal_scheduler": _internal_scheduler_status(),
                "schedule_account_count": len(accounts),
                "schedule_account_source": source or "empty",
            })
        elif parsed.path == "/schedule-accounts":
            self._schedule_accounts_endpoint(qs)
        elif parsed.path == "/discover-accounts":
            self._discover_accounts_endpoint(qs)
        elif parsed.path == "/public/drama-search":
            self._public_drama_search_endpoint(qs)
        elif parsed.path == "/admin/catalog":
            self._admin_catalog_endpoint(qs)
        elif parsed.path == "/admin/access":
            self._admin_access_endpoint(qs)
        elif parsed.path == "/curated-catalog":
            self._curated_catalog_endpoint(qs)
        elif parsed.path == "/drama-media":
            self._serve_drama_media(qs)
        elif parsed.path == "/drama-link":
            self._resolve_drama_link(qs)
        elif parsed.path == "/supabase/latest":
            self._supabase_latest_report_endpoint(qs)
        elif parsed.path == "/supabase/reports":
            self._supabase_reports_endpoint(qs)
        elif parsed.path == "/supabase/report":
            self._supabase_report_endpoint(qs)
        elif parsed.path == "/reports":
            self._list_reports(qs)
        elif parsed.path.startswith("/reports/"):
            self._serve_report(parsed.path, qs)
        elif parsed.path in ("/admin", "/admin/"):
            self._serve_static("/admin.html")
        elif parsed.path in ("/catalog", "/catalog/"):
            self._serve_static("/catalog.html")
        elif "url" in qs:
            if self._require_private_access(qs):
                self._proxy("GET", qs["url"][0])
        else:
            self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/schedule-accounts":
            self._schedule_accounts_endpoint(qs)
            return
        if parsed.path == "/discover-accounts":
            self._discover_accounts_endpoint(qs)
            return
        if parsed.path == "/admin/catalog":
            self._admin_catalog_endpoint(qs)
            return
        if parsed.path == "/save":
            if self._require_private_access(qs):
                self._save_file()
            return
        if parsed.path == "/translate-titles":
            self._translate_titles()
            return
        target = qs.get("url", [None])[0]
        if not target:
            self._send_bytes(400, b'{"error":"missing url param"}', "application/json")
            return
        if self._require_private_access(qs):
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
        supplied = self.headers.get("X-Schedule-Secret", "")
        if supplied and hmac.compare_digest(str(supplied), SCHEDULE_SECRET):
            return True
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            morsel = cookies.get(ADMIN_SESSION_COOKIE_NAME)
            session_value = morsel.value if morsel else ""
        except Exception:
            session_value = ""
        token = _admin_session_token()
        return bool(token and session_value and hmac.compare_digest(str(session_value), token))

    def _allow_report_read(self, qs):
        if PUBLIC_REPORTS:
            return True
        return self._require_schedule_secret(qs)

    def _require_private_access(self, qs):
        if self._is_local_request():
            return True
        return self._require_schedule_secret(qs)

    def _is_local_request(self):
        client = (self.client_address[0] if self.client_address else "").lower()
        return ALLOW_LOOPBACK_PRIVATE_ACCESS and client in ("127.0.0.1", "::1")

    def _request_origin(self):
        proto = str(self.headers.get("X-Forwarded-Proto", "") or "").split(",", 1)[0].strip().lower()
        if proto not in ("http", "https"):
            proto = "https" if os.environ.get("RENDER") else "http"
        host = str(self.headers.get("Host", "") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", host):
            return PUBLIC_BASE_URL
        return "%s://%s" % (proto, host)

    def _send_local_download_unavailable(self):
        body = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>需要本地代理</title></head>
<body style="margin:0;background:#f5f7fb;color:#172033;font:14px/1.6 Arial,'Microsoft YaHei',sans-serif">
<div style="max-width:720px;margin:40px auto;padding:24px;background:#fff;border:1px solid #e6eaf1;border-radius:8px">
<h1 style="margin-top:0">保存到本机需要本地代理</h1>
<p>线上 Render 服务器不能直接写入你的电脑硬盘。请在项目目录运行 <b>启动代理.bat</b>，用 <b>http://localhost:8787/</b> 打开页面后再点“保存到本机”。</p>
<p style="color:#667085">这样视频会从视频源直接下载到你电脑的项目 <b>downloads</b> 目录，不再走浏览器下载 ZIP。</p>
<p><a href="/" style="display:inline-flex;padding:8px 12px;border:1px solid #e6eaf1;border-radius:6px;text-decoration:none;color:#172033">返回</a></p>
</div>
</body></html>"""
        self._send_bytes(403, body.encode("utf-8"), "text/html; charset=utf-8", no_cache=True)

    def _send_local_download_status(self, job_id):
        if not self._is_local_request():
            self._send_local_download_unavailable()
            return
        job = _local_download_job_snapshot(job_id)
        if not job:
            self._send_json(404, {"ok": False, "error": "local download job not found"})
            return
        self._send_bytes(200, _render_local_download_status_page(job), "text/html; charset=utf-8", no_cache=True)

    def _start_drama_local_download(self, uid, drama_id, qs):
        if not self._is_local_request():
            self._send_local_download_unavailable()
            return
        if not drama_id:
            self._send_json(400, {"ok": False, "error": "missing drama_id"})
            return
        account = (uid or "").strip().lstrip("@")
        try:
            episode_items = _get_drama_episode_items(drama_id, account, limit=DRAMA_ZIP_MAX_EPISODES)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if not episode_items:
            self._send_json(404, {"ok": False, "error": "no episodes found"})
            return
        job_id = _start_local_download_job(account, drama_id, episode_items)
        status_url = "/drama-link?" + urllib.parse.urlencode({"target": "local_status", "job_id": job_id})
        redirect = str(qs.get("redirect", ["1"])[0]).lower() not in ("0", "false", "no")
        if not redirect:
            self._send_json(202, {"ok": True, "job_id": job_id, "status_url": status_url})
            return
        self.send_response(302)
        self.send_header("Location", status_url)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_drama_local_downloader_script(self, uid, drama_id):
        if not drama_id:
            self._send_json(400, {"ok": False, "error": "missing drama_id"})
            return
        account = (uid or "").strip().lstrip("@")
        try:
            episode_items = _get_drama_episode_items(drama_id, account, limit=DRAMA_ZIP_MAX_EPISODES)
            if not episode_items:
                self._send_json(404, {"ok": False, "error": "no episodes found"})
                return
            script, count, errors = _build_drama_local_downloader_script(
                account, drama_id, episode_items, origin=self._request_origin()
            )
            script = _wrap_powershell_downloader_cmd(script)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if not count and errors:
            self._send_json(502, {"ok": False, "error": "no playable URLs prepared", "errors": errors[:10]})
            return
        filename = _safe_download_name("%s-%s-downloader.cmd" % (account or "account", _clean_drama_id(drama_id) or "drama"), 120)
        self.send_response(200)
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", _attachment_header(filename))
        self.send_header("Content-Length", str(len(script)))
        self.end_headers()
        self.wfile.write(script)

    def _send_drama_episode_zip(self, uid, drama_id, episode_items):
        account = (uid or "").strip().lstrip("@")
        clean_drama_id = _clean_drama_id(drama_id)
        if not episode_items:
            self._send_json(404, {"ok": False, "error": "no episodes found"})
            return
        filename = _drama_zip_filename(account, clean_drama_id)
        started = time.time()
        self.send_response(200)
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", _attachment_header(filename))
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        manifest = {
            "uid": account,
            "drama_id": clean_drama_id,
            "generated_at": datetime.datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
            "workers": min(DRAMA_ZIP_WORKERS, max(1, len(episode_items))),
            "count": 0,
            "files": [],
            "errors": [],
        }
        used_names = set()
        try:
            with zipfile.ZipFile(self.wfile, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                max_workers = manifest["workers"]
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = [
                        pool.submit(_download_episode_to_temp, index, item, account, started)
                        for index, item in enumerate(episode_items, 1)
                    ]
                    for future in concurrent.futures.as_completed(futures):
                        result = future.result()
                        episode_no = result.get("episode") or result.get("index") or ""
                        video_id = result.get("video_id") or ""
                        title = result.get("title") or ""
                        if not result.get("ok"):
                            manifest["errors"].append({
                                "episode": episode_no,
                                "video_id": video_id,
                                "title": title,
                                "error": result.get("error") or "download failed",
                            })
                            continue
                        temp_path = result.get("temp_path") or ""
                        try:
                            base = "%03d-%s" % (_to_int(episode_no) or result.get("index") or 0, _safe_download_name(title, 70))
                            if video_id:
                                base += "-" + _safe_download_name(video_id[-10:], 12)
                            archive_name = _unique_archive_name(base + (result.get("ext") or ".mp4"), used_names)
                            zf.write(temp_path, archive_name)
                            manifest["files"].append({
                                "episode": episode_no,
                                "video_id": video_id,
                                "title": title,
                                "file": archive_name,
                                "bytes": result.get("bytes") or 0,
                            })
                        finally:
                            if temp_path:
                                try:
                                    os.remove(temp_path)
                                except OSError:
                                    pass
                manifest["files"].sort(key=lambda item: _to_int(item.get("episode")) or 0)
                manifest["errors"].sort(key=lambda item: _to_int(item.get("episode")) or 0)
                manifest["count"] = len(manifest["files"])
                zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                if manifest["errors"]:
                    lines = [
                        "Some episodes could not be downloaded.",
                        "uid=%s drama_id=%s" % (account, clean_drama_id),
                        "",
                    ]
                    for item in manifest["errors"]:
                        lines.append("Episode %s %s: %s" % (
                            item.get("episode") or "",
                            item.get("video_id") or "",
                            item.get("error") or "",
                        ))
                    zf.writestr("download_errors.txt", "\n".join(lines))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_drama_media(self, qs):
        uid = (qs.get("uid", [""])[0] or "").strip().lstrip("@")
        video_id = qs.get("video_id", [""])[0] or qs.get("item_id", [""])[0]
        expires = qs.get("expires", [""])[0]
        signature = qs.get("sig", [""])[0]
        if not _video_media_ticket_valid(uid, video_id, expires, signature):
            self._send_json(403, {"ok": False, "error": "download link expired or invalid"})
            return

        upstream = None
        last_error = None
        for attempt in range(2):
            source = _get_video_play_source(video_id, uid=uid)
            if not source.get("url"):
                last_error = TikHubError(source.get("error") or "play source unavailable")
                break
            try:
                upstream = _open_video_download(source, uid, self.headers.get("Range", ""))
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if attempt == 0 and exc.code in (401, 403, 404, 410):
                    _video_play_cache_remove(video_id)
                    continue
                break
            except Exception as exc:
                last_error = exc
                break
        if upstream is None:
            source = source if isinstance(source, dict) else {}
            error_code = source.get("error_code") or "video_source_download_failed"
            if error_code == "tiktok_login_required":
                status = 409
            elif error_code == "video_removed_or_unavailable":
                status = 410
            else:
                code = getattr(last_error, "code", 502)
                status = 502 if not isinstance(code, int) or code < 400 else code
            self._send_json(status, {
                "ok": False,
                "error_code": error_code,
                "error": source.get("error") or "video source download failed",
                "video_id": _clean_drama_id(video_id),
            })
            return

        try:
            status = getattr(upstream, "status", None) or upstream.getcode() or 200
            content_type = upstream.headers.get("Content-Type", "video/mp4")
            if "text/html" in content_type.lower():
                upstream.close()
                self._send_json(502, {"ok": False, "error": "video source returned a web page"})
                return
            self.send_response(status)
            self._cors()
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Type", content_type)
            for name in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
                value = upstream.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.end_headers()
            while True:
                chunk = upstream.read(DRAMA_ZIP_CHUNK_BYTES)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _resolve_drama_link(self, qs):
        uid = (qs.get("uid", [""])[0] or qs.get("account", [""])[0]).strip().lstrip("@")
        drama_id = qs.get("drama_id", [""])[0] or qs.get("dramaID", [""])[0]
        video_id = qs.get("video_id", [""])[0] or qs.get("item_id", [""])[0]
        target = qs.get("target", ["play"])[0] or "play"
        target_norm = str(target or "").strip().lower()
        redirect = str(qs.get("redirect", ["1"])[0]).lower() not in ("0", "false", "no")
        private_targets = {
            "", "play", "source", "direct", "media",
            "series", "drama", "full_series", "whole_drama",
            "zip", "download", "download_zip", "archive",
            "local_status", "local_download_status",
            "local", "local_save", "save_local", "local_download",
            "local_script", "download_script", "ps1",
        }
        if target_norm in private_targets and not self._require_private_access(qs):
            return
        if target_norm in ("local_status", "local_download_status"):
            self._send_local_download_status(qs.get("job_id", [""])[0])
            return
        if target_norm in ("local", "local_save", "save_local", "local_download"):
            self._start_drama_local_download(uid, drama_id, qs)
            return
        if target_norm in ("local_script", "download_script", "ps1"):
            self._send_drama_local_downloader_script(uid, drama_id)
            return
        if target_norm in ("zip", "download", "download_zip", "archive"):
            if not drama_id:
                self._send_json(400, {"ok": False, "error": "missing drama_id"})
                return
            episode_items = _get_drama_episode_items(drama_id, uid, limit=DRAMA_ZIP_MAX_EPISODES)
            self._send_drama_episode_zip(uid, drama_id, episode_items)
            return
        if target_norm in ("series", "drama", "full_series", "whole_drama"):
            try:
                reference = {
                    "account": uid,
                    "drama_id": _clean_drama_id(drama_id),
                    "episode_count": 0,
                    "drama_title": "",
                    "source": "request",
                } if drama_id else _resolve_drama_reference_for_video(uid, video_id)
            except Exception as exc:
                self._send_json(502, {"ok": False, "error": "无法识别这集所属短剧：%s" % exc})
                return
            resolved_drama_id = _clean_drama_id(reference.get("drama_id"))
            resolved_uid = _to_text(reference.get("account") or uid, 80).strip().lstrip("@")
            if not resolved_drama_id:
                self._send_json(404, {"ok": False, "error": "没有识别到这集所属的短剧，可能不是短剧库作品"})
                return
            list_url = "/drama-link?" + urllib.parse.urlencode({
                "uid": resolved_uid,
                "drama_id": resolved_drama_id,
                "target": "list",
                "redirect": "1",
            })
            if redirect:
                self.send_response(302)
                self.send_header("Location", list_url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(200, {
                "ok": True,
                "target": "series",
                "uid": resolved_uid,
                "drama_id": resolved_drama_id,
                "drama_title": reference.get("drama_title") or "",
                "episode_count": _to_int(reference.get("episode_count")),
                "source": reference.get("source") or "",
                "url": list_url,
            })
            return
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
        source = {}
        if video_id:
            if prefer_play:
                # Resolve once before returning a signed relay URL.  Keeping the
                # compatibility wrapper here also gives callers/tests a single
                # stable resolver entry point while /drama-media refreshes an
                # expired upstream CDN URL again at download time.
                source = _get_video_play_source(video_id, uid=uid)
                if source.get("url"):
                    link = _video_media_ticket_url(uid, video_id)
            else:
                link = _build_tiktok_video_url(uid, video_id)
        if not link and drama_id:
            link = _get_drama_episode_link(drama_id, uid, target=target)
        if not link:
            error_code = source.get("error_code") if prefer_play and isinstance(source, dict) else ""
            if error_code == "tiktok_login_required":
                status = 409
            elif error_code == "video_removed_or_unavailable":
                status = 410
            else:
                status = 502 if prefer_play else 404
            payload = {
                "ok": False,
                "error_code": error_code or ("play_source_unavailable" if prefer_play else "drama_link_not_found"),
                "error": (
                    source.get("error") if prefer_play and isinstance(source, dict) else ""
                ) or ("play source unavailable" if prefer_play else "drama link not found"),
            }
            if video_id:
                payload["video_id"] = _clean_drama_id(video_id)
                payload["work_url"] = _build_tiktok_video_url(uid, video_id)
            self._send_json(status, payload)
            return
        if redirect:
            self.send_response(302)
            self.send_header("Location", link)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, {"ok": True, "url": link, "target": target})

    def _public_drama_search_endpoint(self, qs):
        if self.command != "GET":
            self._send_json(405, {"ok": False, "error": "只支持 GET 请求"})
            return
        forwarded = str(self.headers.get("X-Forwarded-For", "") or "").split(",", 1)[0].strip()
        client_key = forwarded or (self.client_address[0] if self.client_address else "unknown")
        if not _public_drama_search_rate_allowed(client_key):
            self._send_json(429, {"ok": False, "error": "搜索太频繁，请稍后再试"})
            return
        try:
            query = qs.get("q", qs.get("query", [""]))[0]
            limit = _to_int(qs.get("limit", [20])[0]) or 20
            self._send_json(200, _public_drama_search_payload(query, limit))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception:
            self._send_json(502, {"ok": False, "error": "TikTok 短剧搜索暂时不可用，请稍后再试"})

    def _discover_accounts_endpoint(self, qs):
        if not self._require_schedule_secret(qs):
            return
        mode = str(qs.get("mode", ["accounts"])[0] or "accounts").strip().lower()
        if mode in ("works", "videos"):
            if self.command != "GET":
                self._send_json(405, {"ok": False, "error": "discover works only supports GET"})
                return
            should_run = str(qs.get("run", ["0"])[0]).lower() in ("1", "true", "yes")
            if not should_run:
                self._send_json(200, _read_discovered_works())
                return
            try:
                payload = _discover_works(
                    qs.get("queries", qs.get("keywords", [""]))[0],
                    _to_int(qs.get("limit", [DISCOVERY_MAX_CANDIDATES])[0]) or DISCOVERY_MAX_CANDIDATES,
                    _to_int(qs.get("max_videos", [DISCOVERY_MAX_VIDEOS_PER_KEYWORD])[0]) or DISCOVERY_MAX_VIDEOS_PER_KEYWORD,
                )
                self._send_json(200, payload)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if self.command == "GET":
            should_run = str(qs.get("run", ["0"])[0]).lower() in ("1", "true", "yes")
            if not should_run:
                payload = _read_discovered_accounts()
                configured_accounts, _source = _configured_schedule_accounts()
                monitored = {item.lower() for item in configured_accounts}
                accounts = payload.get("accounts", [])
                if isinstance(accounts, list):
                    for item in accounts:
                        if isinstance(item, dict):
                            item["already_monitored"] = str(item.get("account", "")).lower() in monitored
                self._send_json(200, payload)
                return
            try:
                payload = _discover_accounts(
                    qs.get("keywords", [""])[0],
                    _to_int(qs.get("limit", [DISCOVERY_MAX_CANDIDATES])[0]) or DISCOVERY_MAX_CANDIDATES,
                    _to_int(qs.get("min_followers", [DISCOVERY_MIN_FOLLOWERS])[0]),
                    _to_int(qs.get("min_dramas", [DISCOVERY_MIN_DRAMAS])[0]),
                    _to_int(qs.get("max_videos", [DISCOVERY_MAX_VIDEOS_PER_KEYWORD])[0]) or DISCOVERY_MAX_VIDEOS_PER_KEYWORD,
                )
                self._send_json(200, payload)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(ln) or b"{}")
            accounts = payload.get("accounts", payload.get("account", ""))
            if isinstance(accounts, str):
                accounts = _parse_accounts(accounts)
            elif isinstance(accounts, list):
                accounts = _parse_accounts("\n".join(str(item) for item in accounts))
            else:
                accounts = []
            result = _append_schedule_accounts(accounts)
            saved = result["saved"]
            self._send_json(200, {
                "ok": True,
                "added": result["added"],
                "added_count": len(result["added"]),
                "accounts": saved.get("accounts", []),
                "count": len(saved.get("accounts", [])),
                "source": "backend_pool",
                "supabase": result.get("supabase"),
                "updated_at": saved.get("updated_at", ""),
                "runtime_file": "reports/schedule_accounts.json",
            })
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _schedule_accounts_endpoint(self, qs):
        if not self._require_schedule_secret(qs):
            return
        if self.command == "GET":
            pool = _schedule_account_pool()
            accounts, source = _configured_schedule_accounts()
            updated_at = pool.get("updated_at", "") if "backend_pool" in source else ""
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
            mode = str(payload.get("mode", "replace") or "replace").strip().lower()
            if mode not in ("append", "replace"):
                self._send_json(400, {"ok": False, "error": "mode must be append or replace"})
                return
            accounts = payload.get("accounts", payload.get("text", ""))
            if isinstance(accounts, str):
                accounts = _parse_accounts(accounts)
            elif isinstance(accounts, list):
                accounts = _parse_accounts("\n".join(str(item) for item in accounts))
            else:
                accounts = []
            if mode == "append":
                result = _append_schedule_accounts(accounts)
                saved = result["saved"]
                added = result["added"]
                supabase = result.get("supabase")
            else:
                saved = _write_schedule_account_pool(accounts)
                added = []
                supabase = _store_schedule_accounts_in_supabase(saved["accounts"])
            self._send_json(200, {
                "ok": True,
                "mode": mode,
                "added": added,
                "added_count": len(added),
                "accounts": saved["accounts"],
                "count": len(saved["accounts"]),
                "source": "backend_pool",
                "supabase": supabase,
                "updated_at": saved["updated_at"],
                "runtime_file": "reports/schedule_accounts.json",
            })
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _admin_catalog_endpoint(self, qs):
        if not self._require_schedule_secret(qs):
            return
        if self.command == "GET":
            try:
                catalog, storage = _load_admin_catalog(force=True)
                report, sources, _source_map, accounts = _admin_catalog_context()
                self._send_json(200, {
                    "ok": True,
                    "catalog": catalog,
                    "storage": storage,
                    "generated_at": report.get("generated_at") or "",
                    "sources": sources,
                    "source_count": len(sources),
                    "accounts": accounts,
                    "account_count": len(accounts),
                    "curated": _curated_catalog_payload(include_offline=True),
                })
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > 8 * 1024 * 1024:
                self._send_json(413, {"ok": False, "error": "catalog payload is too large"})
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
            catalog = payload.get("catalog") if isinstance(payload, dict) else None
            if not isinstance(catalog, dict):
                self._send_json(400, {"ok": False, "error": "catalog must be an object"})
                return
            expected_revision = payload.get("expected_revision")
            saved, storage = _persist_admin_catalog(catalog, expected_revision=expected_revision)
            self._send_json(200, {
                "ok": True,
                "catalog": saved,
                "storage": storage,
                "curated": _curated_catalog_payload(include_offline=True),
            })
        except AdminCatalogConflict as exc:
            current, storage = _load_admin_catalog(force=True)
            self._send_json(409, {
                "ok": False,
                "error": str(exc),
                "catalog": current,
                "storage": storage,
            })
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _admin_access_endpoint(self, qs):
        if not self._require_schedule_secret(qs):
            return
        if self.command != "GET":
            self._send_json(405, {"ok": False, "error": "method not allowed"})
            return
        permissions = [
            {"id": permission_id, "label": label, "granted": True}
            for permission_id, label in ADMIN_PERMISSION_DEFINITIONS
        ]
        self._send_json(200, {
            "ok": True,
            "role": "super_admin",
            "role_label": "超级管理员",
            "permissions": permissions,
            "permission_count": len(permissions),
            "services": {
                "schedule_secret": True,
                "tikhub_api": bool(SERVER_API_KEY),
                "supabase": bool(SUPABASE_ENABLED and _supabase_configured()),
            },
        })

    def _curated_catalog_endpoint(self, qs):
        if self.command != "GET":
            self._send_json(405, {"ok": False, "error": "method not allowed"})
            return
        try:
            self._send_json(200, _curated_catalog_payload(include_offline=False))
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
                result = _execute_scheduled_job(accounts, trigger="api")
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
                _execute_scheduled_job(accounts, trigger="api")
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

    def _supabase_latest_report_endpoint(self, qs):
        if not self._allow_report_read(qs):
            return
        try:
            self._send_json(200, _supabase_latest_report_payload())
        except Exception as exc:
            self._send_json(404, {"ok": False, "error": str(exc), "source": "supabase"})

    def _supabase_reports_endpoint(self, qs):
        if not self._allow_report_read(qs):
            return
        try:
            limit = _to_int(qs.get("limit", [SUPABASE_REPORT_HISTORY_LIMIT])[0])
            self._send_json(200, {
                "ok": True,
                "reports": _supabase_report_history(limit),
                "source": "supabase",
            })
        except Exception as exc:
            self._send_json(404, {"ok": False, "error": str(exc), "reports": [], "source": "supabase"})

    def _supabase_report_endpoint(self, qs):
        if not self._allow_report_read(qs):
            return
        try:
            payload = _supabase_report_payload_by_id(qs.get("id", [""])[0])
            compact = str(qs.get("compact", ["0"])[0]).lower() in ("1", "true", "yes")
            self._send_json(200, _compact_report_payload(payload) if compact else payload)
        except Exception as exc:
            self._send_json(404, {"ok": False, "error": str(exc), "source": "supabase"})

    def _serve_report(self, path, qs):
        if not self._allow_report_read(qs):
            return
        relative = posixpath.normpath(urllib.parse.unquote(path[len("/reports/"):]).replace("\\", "/")).lstrip("/")
        if relative in ("", ".", "..") or relative.startswith("../"):
            self._send_json(404, {"ok": False, "error": "report not found"})
            return
        name = os.path.basename(relative)
        full = os.path.normpath(os.path.join(REPORTS_DIR, *relative.split("/")))
        try:
            runtime_safe = os.path.commonpath((os.path.normpath(REPORTS_DIR), full)) == os.path.normpath(REPORTS_DIR)
        except ValueError:
            runtime_safe = False
        if not runtime_safe or not os.path.isfile(full):
            public_full = os.path.normpath(os.path.join(PUBLIC_REPORTS_DIR, *relative.split("/")))
            try:
                public_safe = os.path.commonpath((os.path.normpath(PUBLIC_REPORTS_DIR), public_full)) == os.path.normpath(PUBLIC_REPORTS_DIR)
            except ValueError:
                public_safe = False
            if public_safe and os.path.isfile(public_full):
                full = public_full
            else:
                self._send_json(404, {"ok": False, "error": "report not found"})
                return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        content_length = os.path.getsize(full)
        self.send_response(200)
        self._cors()
        cache_control = "no-store" if relative == "latest_report.json" or runtime_safe else "public, max-age=120, stale-while-revalidate=600"
        self.send_header("Cache-Control", cache_control)
        if cache_control == "no-store":
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", "attachment; filename=%s" % name)
        self.send_header("Content-Length", str(content_length))
        self.end_headers()
        with open(full, "rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

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
        issue_admin_session = path in ("/admin", "/admin/", "/admin.html")
        if path in ("/admin", "/admin/"):
            path = "/admin.html"
        if path in ("/catalog", "/catalog/"):
            path = "/catalog.html"
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
        extra_headers = {}
        if issue_admin_session:
            token = _admin_session_token()
            if token:
                secure = "; Secure" if os.environ.get("RENDER") else ""
                extra_headers["Set-Cookie"] = (
                    f"{ADMIN_SESSION_COOKIE_NAME}={token}; Path=/; Max-Age={ADMIN_SESSION_MAX_AGE}; "
                    f"HttpOnly; SameSite=Strict{secure}"
                )
        self._send_bytes(
            200, data, ctype, no_cache=path.endswith(".html"),
            cache_control=cache_control, extra_headers=extra_headers,
        )

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

    def _send_bytes(self, code, data, ctype, no_cache=False, cache_control=None, extra_headers=None):
        extra_headers = dict(extra_headers or {})
        accept_encoding = str(getattr(self, "headers", {}).get("Accept-Encoding", "")).lower()
        already_encoded = any(str(name).lower() == "content-encoding" for name in extra_headers)
        if (
            len(data) >= 16 * 1024
            and "application/json" in str(ctype).lower()
            and "gzip" in accept_encoding
            and not already_encoded
        ):
            compressed = gzip.compress(data, compresslevel=5, mtime=0)
            if len(compressed) < len(data):
                data = compressed
                extra_headers["Content-Encoding"] = "gzip"
                extra_headers["Vary"] = "Accept-Encoding"
        self.send_response(code)
        self._cors()
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        elif no_cache:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("Content-Type", ctype)
        for name, value in extra_headers.items():
            self.send_header(str(name), str(value))
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
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        _start_internal_scheduler()
        server.serve_forever()
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

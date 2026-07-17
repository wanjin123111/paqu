import datetime
import gzip
import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import tikhub_proxy as proxy


class PrivateAccessTests(unittest.TestCase):
    def make_handler(self, client="203.0.113.8", headers=None):
        handler = object.__new__(proxy.Handler)
        handler.client_address = (client, 12345)
        handler.headers = headers or {}
        handler.responses = []
        handler._send_json = lambda code, payload: handler.responses.append((code, payload))
        return handler

    def test_remote_request_requires_secret(self):
        handler = self.make_handler(headers={"Host": "localhost"})
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"):
            self.assertFalse(handler._require_private_access({}))
        self.assertEqual(handler.responses[0][0], 403)

    def test_remote_header_secret_is_accepted(self):
        handler = self.make_handler(headers={"X-Schedule-Secret": "expected-secret"})
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"):
            self.assertTrue(handler._require_private_access({}))
        self.assertEqual(handler.responses, [])

    def test_admin_auto_session_cookie_is_accepted(self):
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"):
            token = proxy._admin_session_token()
            handler = self.make_handler(headers={
                "Cookie": f"{proxy.ADMIN_SESSION_COOKIE_NAME}={token}",
            })
            self.assertTrue(handler._require_private_access({}))
        self.assertEqual(handler.responses, [])

    def test_admin_page_issues_http_only_session_without_plaintext_secret(self):
        handler = self.make_handler()
        handler._send_bytes = mock.Mock()
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.dict(proxy.os.environ, {"RENDER": "true"}):
            handler._serve_static("/admin")

        _, kwargs = handler._send_bytes.call_args
        cookie = kwargs["extra_headers"]["Set-Cookie"]
        self.assertIn(f"{proxy.ADMIN_SESSION_COOKIE_NAME}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("expected-secret", cookie)

    def test_query_parameter_secret_is_rejected(self):
        handler = self.make_handler()
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"):
            self.assertFalse(handler._require_private_access({"secret": ["expected-secret"]}))
        self.assertEqual(handler.responses[0][0], 403)

    def test_loopback_request_stays_available_without_secret(self):
        handler = self.make_handler(client="127.0.0.1")
        with mock.patch.object(proxy, "SCHEDULE_SECRET", ""), \
                mock.patch.object(proxy, "ALLOW_LOOPBACK_PRIVATE_ACCESS", True):
            self.assertTrue(handler._require_private_access({}))
        self.assertEqual(handler.responses, [])

    def test_render_can_disable_loopback_bypass(self):
        handler = self.make_handler(client="127.0.0.1")
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "ALLOW_LOOPBACK_PRIVATE_ACCESS", False):
            self.assertFalse(handler._require_private_access({}))
        self.assertEqual(handler.responses[0][0], 403)

    def test_public_proxy_route_stops_before_forwarding(self):
        handler = self.make_handler()
        handler.path = "/?url=https%3A%2F%2Fapi.tikhub.io%2Ftest"
        handler._require_private_access = mock.Mock(return_value=False)
        handler._proxy = mock.Mock()
        handler._serve_static = mock.Mock()
        handler.do_GET()
        handler._require_private_access.assert_called_once()
        handler._proxy.assert_not_called()

    def test_public_video_source_stops_before_tikhub_lookup(self):
        handler = self.make_handler()
        handler._require_private_access = mock.Mock(return_value=False)
        with mock.patch.object(proxy, "_get_video_play_url") as get_play_url:
            handler._resolve_drama_link({
                "uid": ["demo"],
                "video_id": ["123"],
                "target": ["play"],
            })
        handler._require_private_access.assert_called_once()
        get_play_url.assert_not_called()

    def test_header_authenticated_video_source_returns_url(self):
        handler = self.make_handler(headers={"X-Schedule-Secret": "expected-secret"})
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "_get_video_play_source", return_value={
                    "url": "https://media.example/video.mp4",
                    "cookie": "sessionid=must-not-leak",
                }):
            handler._resolve_drama_link({
                "uid": ["demo"],
                "video_id": ["123"],
                "target": ["play"],
                "redirect": ["0"],
            })

        self.assertEqual(len(handler.responses), 1)
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "play")
        parsed = urllib.parse.urlparse(payload["url"])
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/drama-media")
        self.assertEqual(params["uid"], ["demo"])
        self.assertEqual(params["video_id"], ["123"])
        self.assertIn("expires", params)
        self.assertIn("sig", params)
        self.assertNotIn("must-not-leak", json.dumps(payload))

    def test_failed_video_source_does_not_masquerade_as_tiktok_work_page(self):
        handler = self.make_handler(headers={"X-Schedule-Secret": "expected-secret"})
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "_get_video_play_source", return_value={
                    "error_code": "play_source_unavailable",
                    "error": "play source unavailable",
                }):
            handler._resolve_drama_link({
                "uid": ["demo"],
                "video_id": ["123"],
                "target": ["play"],
                "redirect": ["1"],
            })

        code, payload = handler.responses[0]
        self.assertEqual(code, 502)
        self.assertEqual(payload["error"], "play source unavailable")
        self.assertEqual(payload["work_url"], "https://www.tiktok.com/@demo/video/123")

    def test_zip_download_requires_and_accepts_header_secret(self):
        denied = self.make_handler()
        denied._send_drama_episode_zip = mock.Mock()
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "_get_drama_episode_items") as get_items:
            denied._resolve_drama_link({
                "uid": ["demo"],
                "drama_id": ["drama-1"],
                "target": ["zip"],
            })
        self.assertEqual(denied.responses[0][0], 403)
        get_items.assert_not_called()
        denied._send_drama_episode_zip.assert_not_called()

        allowed = self.make_handler(headers={"X-Schedule-Secret": "expected-secret"})
        allowed._send_drama_episode_zip = mock.Mock()
        episode_items = [{"aweme_id": "123"}]
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "_get_drama_episode_items", return_value=episode_items):
            allowed._resolve_drama_link({
                "uid": ["demo"],
                "drama_id": ["drama-1"],
                "target": ["zip"],
            })
        allowed._send_drama_episode_zip.assert_called_once_with("demo", "drama-1", episode_items)


class EpisodePageSecurityTests(unittest.TestCase):
    def test_episode_page_uses_header_authenticated_actions(self):
        episodes = [{
            "index": 1,
            "episode_label": "第1集",
            "video_id": "123",
            "title": "Demo episode",
            "publish_time": "2026-07-13 12:00:00",
            "views": 100,
            "views_text": "100",
            "video_url": "https://www.tiktok.com/@demo/video/123",
            "play_url": "/drama-link?uid=demo&video_id=123&target=play&redirect=1",
        }]

        with mock.patch.object(proxy, "_collect_episode_growth_and_record", return_value={}):
            page = proxy._render_drama_episode_list_page("demo", "drama-1", episodes).decode("utf-8")

        self.assertIn('id="backendSecretInput"', page)
        self.assertIn('data-private-action="open-json"', page)
        self.assertIn('data-private-action="download"', page)
        self.assertIn('headers:{"X-Schedule-Secret":secret}', page)
        self.assertIn('localStorage.getItem(storageKey)', page)
        self.assertIn("target=zip", page)
        self.assertIn("target=local_script", page)
        self.assertNotIn("?secret=", page)
        self.assertNotIn("&secret=", page)


class PlaySourceResolutionTests(unittest.TestCase):
    def setUp(self):
        with proxy.VIDEO_PLAY_URL_CACHE_LOCK:
            proxy.VIDEO_PLAY_URL_CACHE.clear()

    def tearDown(self):
        with proxy.VIDEO_PLAY_URL_CACHE_LOCK:
            proxy.VIDEO_PLAY_URL_CACHE.clear()

    def test_play_url_parser_supports_new_response_shapes(self):
        self.assertEqual(
            proxy._video_play_url_from_tree({"data": {"play_url": "https://cdn.example/direct.mp4?a=1&amp;b=2"}}),
            "https://cdn.example/direct.mp4?a=1&b=2",
        )
        self.assertEqual(
            proxy._video_play_url_from_tree({
                "aweme_detail": {"video": {"playAddr": {"urlList": ["https://cdn.example/camel.mp4"]}}}
            }),
            "https://cdn.example/camel.mp4",
        )
        self.assertEqual(
            proxy._video_play_url_from_tree({
                "data": {
                    "itemInfo": {
                        "itemStruct": {
                            "music": {"playUrl": "https://cdn.example/audio.mp3"},
                            "video": {
                                "PlayAddrStruct": {
                                    "UrlList": ["https://cdn.example/new-web-video.mp4"],
                                },
                            },
                        },
                    },
                },
            }),
            "https://cdn.example/new-web-video.mp4",
        )
        self.assertEqual(
            proxy._video_play_url_from_tree({
                "video": {
                    "playAddr": "https://v16-webapp-prime.us.tiktok.com/video/legacy-blocked",
                    "PlayAddrStruct": {
                        "UrlList": [
                            "https://v16-webapp-prime.us.tiktok.com/video/blocked",
                            "https://v19-webapp-prime.us.tiktok.com/video/blocked",
                            "https://www.tiktok.com/aweme/v1/play/?video_id=123",
                        ],
                    },
                },
            }),
            "https://www.tiktok.com/aweme/v1/play/?video_id=123",
        )

    def test_id_resolvers_fall_back_to_share_link_resolver(self):
        calls = []

        def fake_get(endpoint, params, _label, **_kwargs):
            calls.append((endpoint, params))
            if endpoint.endswith("fetch_one_video_by_share_url_v2"):
                return {"data": {"aweme_detail": {"video": {
                    "play_addr": {"url_list": ["https://cdn.example/fallback.mp4"]},
                }}}}
            return {"data": {"aweme_detail": {"video": {}}}}

        with mock.patch.object(proxy, "_send_tikhub_get", side_effect=fake_get):
            url = proxy._get_video_play_url("7638351465085455629", uid="aidramalabs_anime2")

        self.assertEqual(url, "https://cdn.example/fallback.mp4")
        share_calls = [call for call in calls if call[0].endswith("fetch_one_video_by_share_url_v2")]
        self.assertEqual(len(share_calls), 1)
        self.assertEqual(
            share_calls[0][1]["share_url"],
            "https://www.tiktok.com/@aidramalabs_anime2/video/7638351465085455629",
        )
        self.assertTrue(calls[0][0].endswith("fetch_post_detail_v2"))

    def test_web_play_source_preserves_chain_token(self):
        payload = {
            "data": {
                "tt_chain_token": "token-value",
                "aweme_detail": {
                    "video": {
                        "play_addr": {"url_list": ["https://www.tiktok.com/aweme/v1/play/?video_id=123"]},
                    },
                },
            },
        }
        with mock.patch.object(proxy, "_send_tikhub_get", return_value=payload):
            source = proxy._get_video_play_source("123", uid="demo")

        self.assertEqual(source["url"], "https://www.tiktok.com/aweme/v1/play/?video_id=123")
        self.assertEqual(source["tt_chain_token"], "token-value")
        self.assertTrue(source["endpoint"].endswith("fetch_post_detail_v2"))

    def test_tiktok_work_page_uses_server_session_without_exposing_it(self):
        payload = {
            "__DEFAULT_SCOPE__": {
                "webapp.video-detail": {
                    "itemInfo": {
                        "itemStruct": {
                            "id": "123",
                            "video": {"playAddr": "https://cdn.example/session-video.mp4"},
                        },
                    },
                },
            },
        }
        page = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        ).encode("utf-8")

        class Headers:
            def get_all(self, _name):
                return ["tt_chain_token=fresh-token; Path=/; Secure"]

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit=-1):
                return page

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["cookie"] = request.headers.get("Cookie", "")
            captured["timeout"] = timeout
            return Response()

        with mock.patch.object(proxy, "TIKTOK_SESSION_COOKIE", "sessionid=private-session"), \
                mock.patch.object(proxy.TIKTOK_RATE_LIMITER, "wait"), \
                mock.patch.object(proxy.urllib.request, "urlopen", side_effect=fake_urlopen):
            source = proxy._fetch_tiktok_work_page_source("demo", "123")

        self.assertEqual(source["url"], "https://cdn.example/session-video.mp4")
        self.assertIn("sessionid=private-session", captured["cookie"])
        self.assertIn("tt_chain_token=fresh-token", source["cookie"])

    def test_short_drama_login_gate_is_reported_precisely(self):
        page = b'<div data-e2e="short-drama-login-gated-surface">login</div>'

        class Headers:
            def get_all(self, _name):
                return []

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit=-1):
                return page

        with mock.patch.object(proxy, "TIKTOK_SESSION_COOKIE", ""), \
                mock.patch.object(proxy.TIKTOK_RATE_LIMITER, "wait"), \
                mock.patch.object(proxy.urllib.request, "urlopen", return_value=Response()):
            source = proxy._fetch_tiktok_work_page_source("demo", "123")

        self.assertEqual(source["error_code"], "tiktok_login_required")

    def test_negative_cache_retains_actionable_failure_reason(self):
        failure = {
            "error_code": "tiktok_login_required",
            "error": "login required",
            "endpoint": "tiktok-work-page",
        }
        proxy._video_play_cache_store("123", failure, now=time.time())
        with mock.patch.object(proxy, "_send_tikhub_get") as tikhub_get:
            source = proxy._get_video_play_source("123", uid="demo")

        self.assertEqual(source["error_code"], "tiktok_login_required")
        tikhub_get.assert_not_called()

    def test_signed_media_ticket_expires_and_cannot_be_changed(self):
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"):
            url = proxy._video_media_ticket_url("demo", "123", expires=2000, origin="https://example.test")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            signature = params["sig"][0]
            self.assertTrue(proxy._video_media_ticket_valid("demo", "123", "2000", signature, now=1500))
            self.assertFalse(proxy._video_media_ticket_valid("demo", "124", "2000", signature, now=1500))
            self.assertFalse(proxy._video_media_ticket_valid("demo", "123", "2000", signature, now=2001))


class LocalDownloaderScriptTests(unittest.TestCase):
    def test_job_collection_stays_an_array_after_finished_jobs_are_removed(self):
        summary = {
            "video_id": "123",
            "episode_no": 1,
            "title": "Episode 1",
        }
        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "_drama_episode_summary", return_value=summary):
            script_bytes, count, errors = proxy._build_drama_local_downloader_script(
                "demo", "drama-1", [{"aweme_id": "123"}], origin="https://example.test"
            )

        script = script_bytes.decode("utf-8-sig")
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])
        self.assertIn("$jobs = @(Receive-FinishedJobs $jobs)", script)
        self.assertIn("$job = Start-Job", script)
        self.assertIn("$jobs = @($jobs) + @($job)", script)
        self.assertIn('$folderPrefix + "-*"', script)
        self.assertIn("$existingDir.FullName", script)
        self.assertNotIn("$jobs += Start-Job", script)
        self.assertIn("https://example.test/drama-media?", script)
        self.assertIn('"work_url": "https://www.tiktok.com/@demo/video/123"', script)
        self.assertNotIn("--cookies-from-browser chrome", script)
        self.assertIn("--cookies-from-browser firefox", script)
        self.assertIn("Get cookies.txt LOCALLY", script)
        self.assertIn("应用绑定加密", script)
        self.assertIn("TIKHUB_TIKTOK_COOKIE_FILE", script)
        self.assertIn("SHA2-256SUMS", script)
        self.assertIn('"tiktok_login_required"', script)
        self.assertIn("$maxJobs = if ($useSessionCookies) { 2 } else { 4 }", script)


class AdminCatalogTests(unittest.TestCase):
    def setUp(self):
        self.original_cache = dict(proxy.ADMIN_CATALOG_CACHE)

    def tearDown(self):
        proxy.ADMIN_CATALOG_CACHE.clear()
        proxy.ADMIN_CATALOG_CACHE.update(self.original_cache)

    def sample_catalog(self):
        return {
            "version": 2,
            "revision": 0,
            "dramas": {
                "drama-company-1": {
                    "id": "drama-company-1",
                    "chinese_title": "公司短剧",
                    "english_title": "Company Drama",
                    "writer": "编剧甲",
                    "producer": "制片乙",
                    "director": "导演丙",
                    "cast": "演员丁",
                    "aliases": ["旧剧名", "旧剧名"],
                    "notes": "内部备注",
                    "online": True,
                    "order": 1,
                },
                "drama-offline": {
                    "id": "drama-offline",
                    "chinese_title": "未上架短剧",
                    "english_title": "Offline Drama",
                    "writer": "编剧",
                    "producer": "制片",
                    "online": False,
                    "order": 2,
                },
            },
            "sources": {
                "account-a|100": {"status": "owned", "drama_id": "drama-company-1"},
                "account-b|200": {"status": "owned", "drama_id": "drama-company-1"},
                "account-c|300": {"status": "owned", "drama_id": "drama-offline"},
                "account-d|400": {"status": "owned", "drama_id": "missing-drama"},
            },
        }

    def test_sanitizer_deduplicates_aliases_and_repairs_dangling_sources(self):
        catalog = proxy._sanitize_admin_catalog(self.sample_catalog())

        self.assertEqual(catalog["dramas"]["drama-company-1"]["aliases"], ["旧剧名"])
        self.assertEqual(catalog["sources"]["account-d|400"]["status"], "pending")
        self.assertEqual(catalog["sources"]["account-d|400"]["drama_id"], "")

    def test_runtime_file_persistence_increments_revision_and_rejects_stale_save(self):
        with tempfile.TemporaryDirectory() as folder:
            target = pathlib.Path(folder) / "admin_catalog.json"
            proxy.ADMIN_CATALOG_CACHE.update({"catalog": None, "storage": "", "expires_at": 0})
            with mock.patch.object(proxy, "ADMIN_CATALOG_FILE", str(target)), \
                    mock.patch.object(proxy, "SUPABASE_ENABLED", False):
                saved, storage = proxy._persist_admin_catalog(self.sample_catalog(), expected_revision=0)
                self.assertEqual(storage, "runtime_file")
                self.assertEqual(saved["revision"], 1)
                self.assertTrue(target.is_file())
                loaded, loaded_storage = proxy._load_admin_catalog(force=True)
                self.assertEqual(loaded_storage, "runtime_file")
                self.assertEqual(loaded["revision"], 1)
                with self.assertRaises(proxy.AdminCatalogConflict):
                    proxy._persist_admin_catalog(self.sample_catalog(), expected_revision=0)

    def test_public_catalog_merges_accounts_and_hides_offline_and_internal_notes(self):
        catalog = proxy._sanitize_admin_catalog(self.sample_catalog())
        sources = [
            {"key": "account-a|100", "account": "account-a", "nickname": "A", "episodes": 60, "views": 1200, "publish_time": "2026-07-10 10:00:00"},
            {"key": "account-b|200", "account": "account-b", "nickname": "B", "episodes": 58, "views": 800, "publish_time": "2026-07-11 10:00:00"},
            {"key": "account-c|300", "account": "account-c", "nickname": "C", "episodes": 40, "views": 500, "publish_time": "2026-07-12 10:00:00"},
        ]
        context = ({"generated_at": "2026-07-14 10:00:00"}, sources, {row["key"]: row for row in sources}, [])

        with mock.patch.object(proxy, "_load_admin_catalog", return_value=(catalog, "runtime_file")), \
                mock.patch.object(proxy, "_admin_catalog_context", return_value=context):
            payload = proxy._curated_catalog_payload(include_offline=False)

        self.assertEqual(payload["count"], 1)
        drama = payload["dramas"][0]
        self.assertEqual(drama["total_views"], 2000)
        self.assertEqual(drama["episodes"], 60)
        self.assertEqual(drama["accounts"], ["account-a", "account-b"])
        self.assertEqual(drama["source_count"], 2)
        self.assertEqual(drama["notes"], "")

    def test_admin_catalog_endpoint_stops_before_read_without_secret(self):
        handler = object.__new__(proxy.Handler)
        handler.command = "GET"
        handler.headers = {}
        handler.responses = []
        handler._send_json = lambda code, payload: handler.responses.append((code, payload))

        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "_load_admin_catalog") as load_catalog:
            handler._admin_catalog_endpoint({})

        self.assertEqual(handler.responses[0][0], 403)
        load_catalog.assert_not_called()

    def test_large_json_response_is_gzip_compressed_when_browser_accepts_it(self):
        handler = object.__new__(proxy.Handler)
        handler.headers = {"Accept-Encoding": "gzip, deflate"}
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._cors = mock.Mock()
        handler.wfile = io.BytesIO()

        payload = {"summary": [{"account": "demo", "title": "短剧" * 100}] * 300}
        handler._send_json(200, payload)

        response_headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertEqual(response_headers.get("Content-Encoding"), "gzip")
        self.assertEqual(response_headers.get("Vary"), "Accept-Encoding")
        decoded = json.loads(gzip.decompress(handler.wfile.getvalue()).decode("utf-8"))
        self.assertEqual(decoded, payload)

    def test_schedule_account_parser_accepts_handles_and_tiktok_profile_links(self):
        accounts = proxy._parse_accounts(
            "demo.one\n@demo_two\nhttps://www.tiktok.com/@demo.three/?lang=en\n"
            "https://m.tiktok.com/@demo_four/video/7653132293346692365\n"
            "https://example.com/@not-tiktok"
        )

        self.assertEqual(accounts, ["demo.one", "demo_two", "demo.three", "demo_four"])

    def test_schedule_accounts_append_mode_uses_authoritative_pool_append(self):
        body = json.dumps({
            "accounts": "https://www.tiktok.com/@demo.author",
            "mode": "append",
        }).encode("utf-8")
        handler = object.__new__(proxy.Handler)
        handler.command = "POST"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._require_schedule_secret = mock.Mock(return_value=True)
        handler.responses = []
        handler._send_json = lambda code, payload: handler.responses.append((code, payload))
        appended = {
            "saved": {"accounts": ["demo.author"], "updated_at": "2026-07-15T15:00:00+08:00"},
            "added": ["demo.author"],
            "supabase": {"ok": True},
        }

        with mock.patch.object(proxy, "_append_schedule_accounts", return_value=appended) as append:
            handler._schedule_accounts_endpoint({})

        append.assert_called_once_with(["demo.author"])
        code, payload = handler.responses[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload["mode"], "append")
        self.assertEqual(payload["added_count"], 1)
        self.assertEqual(payload["accounts"], ["demo.author"])

    def test_admin_access_grants_the_complete_super_admin_permission_set(self):
        handler = object.__new__(proxy.Handler)
        handler.command = "GET"
        handler.headers = {"X-Schedule-Secret": "expected-secret"}
        handler.responses = []
        handler._send_json = lambda code, payload: handler.responses.append((code, payload))

        with mock.patch.object(proxy, "SCHEDULE_SECRET", "expected-secret"), \
                mock.patch.object(proxy, "SERVER_API_KEY", "configured"), \
                mock.patch.object(proxy, "SUPABASE_ENABLED", True), \
                mock.patch.object(proxy, "_supabase_configured", return_value=True):
            handler._admin_access_endpoint({})

        code, payload = handler.responses[0]
        permission_ids = {item["id"] for item in payload["permissions"]}
        self.assertEqual(code, 200)
        self.assertEqual(payload["role"], "super_admin")
        self.assertEqual(payload["role_label"], "超级管理员")
        self.assertEqual(payload["permission_count"], len(proxy.ADMIN_PERMISSION_DEFINITIONS))
        self.assertTrue(all(item["granted"] for item in payload["permissions"]))
        self.assertTrue({
            "catalog.read", "catalog.review", "catalog.merge", "dramas.manage",
            "dramas.publish", "accounts.manage", "schedule.run", "media.private",
            "reports.export",
        }.issubset(permission_ids))
        self.assertEqual(payload["services"], {
            "schedule_secret": True,
            "tikhub_api": True,
            "supabase": True,
        })

    def test_latest_report_query_excludes_admin_catalog_storage_row(self):
        paths = []

        def request(method, path, **_kwargs):
            paths.append(path)
            return [{
                "id": 9,
                "generated_at": "2026-07-14T10:00:00+08:00",
                "accounts_count": 1,
                "dramas_count": 1,
                "raw": {"summary": [], "dramas_detail": []},
            }]

        proxy.SUPABASE_REPORT_CACHE.update({"latest": None, "latest_expires_at": 0, "by_id": {}})
        with mock.patch.object(proxy, "_supabase_report_read_enabled", return_value=True), \
                mock.patch.object(proxy, "_supabase_request", side_effect=request):
            proxy._supabase_latest_report_payload()

        self.assertIn("source=neq.admin_catalog", paths[0])

    def test_admin_and_public_catalog_frontends_use_the_expected_security_boundary(self):
        root = pathlib.Path(proxy.ROOT)
        admin_js = (root / "admin.js").read_text(encoding="utf-8")
        catalog_js = (root / "catalog.js").read_text(encoding="utf-8")
        admin_html = (root / "admin.html").read_text(encoding="utf-8")

        self.assertNotIn("X-Schedule-Secret", admin_js)
        self.assertIn('/admin/access?t=${Date.now()}', admin_js)
        self.assertIn("超级管理员", admin_js)
        self.assertIn('credentials: "same-origin"', admin_js)
        self.assertNotIn("sessionStorage", admin_js)
        self.assertNotIn("localStorage", admin_js)
        self.assertNotIn("?secret=", admin_js)
        self.assertIn("/curated-catalog", catalog_js)
        self.assertNotIn("X-Schedule-Secret", catalog_js)
        self.assertIn('id="rankingTableBody"', (root / "catalog.html").read_text(encoding="utf-8"))
        catalog_html = (root / "catalog.html").read_text(encoding="utf-8")
        self.assertIn('id="rankingSection"', catalog_html)
        self.assertIn('class="hero-main"', catalog_html)
        self.assertIn('max-height:min(52vh,560px)', catalog_html)
        self.assertIn('<div class="sidebar-foot"><a class="front-link" href="/" target="_blank" rel="noopener noreferrer">打开前端页面 ↗</a></div>', admin_html)
        self.assertNotIn('查看公司短剧前台', admin_html)
        self.assertNotIn('返回原始数据看板', admin_html)
        self.assertEqual(admin_html.count('class="front-link"'), 1)
        self.assertIn('id="permissionList"', admin_html)
        self.assertIn('id="settingPermissionState"', admin_html)
        self.assertIn('id="adminRoleBadge"', admin_html)
        self.assertNotIn('id="secretInput"', admin_html)
        self.assertNotIn('id="verifyBtn"', admin_html)
        self.assertEqual(admin_html.count('data-close-dialog="editDialog"'), 2)
        self.assertEqual(admin_html.count('data-close-dialog="accountDialog"'), 2)
        self.assertRegex(admin_html, r'id="ignoreFromEdit"[^>]+type="button"')
        self.assertRegex(admin_html, r'id="saveDramaBtn"[^>]+type="button"')
        self.assertIn('id="adminLoading"', admin_html)
        self.assertIn('data-view="claimed"', admin_html)
        self.assertIn('id="view-claimed"', admin_html)
        self.assertIn('id="claimedBody"', admin_html)
        self.assertIn('function renderClaimed()', admin_js)
        self.assertNotIn('>二次编辑</button>', admin_js)
        self.assertIn('data-edit-source="${escapeHtml(row.key)}">调整归属</button>', admin_js)
        self.assertIn('data-edit-drama="${escapeHtml(drama.id)}">编辑</button>', admin_js)
        self.assertIn('$("editForm").addEventListener("submit", (event) => event.preventDefault());', admin_js)
        self.assertIn('$("accountForm").addEventListener("submit", (event) => event.preventDefault());', admin_js)
        self.assertIn('api("/schedule-accounts", {', admin_js)
        self.assertIn('JSON.stringify({ accounts: raw, mode: "append" })', admin_js)
        self.assertIn('loadingMessage: "正在确认账号已进入监控池"', admin_js)
        self.assertIn('const missing = (payload.added || []).filter', admin_js)
        admin_js = (root / "admin.js").read_text(encoding="utf-8")
        self.assertIn('report = await api(`/supabase/latest?t=${Date.now()}`', admin_js)
        self.assertIn('report = await api(`/public_reports/latest_report.json?t=${Date.now()}`', admin_js)
        self.assertLess(admin_js.index('/supabase/latest?t='), admin_js.index('/public_reports/latest_report.json?t='))
        self.assertIn('来源：${reportSource} ${normalized.accounts.length} 个账号', admin_js)
        for name in ("index.html", "tikhub-report-frontend.html"):
            public_html = (root / name).read_text(encoding="utf-8")
            self.assertIn('id="pageLoadingStatus"', public_html)
            self.assertIn('function beginPageLoading(message)', public_html)
            self.assertIn('id="catalogPage"', public_html)
            self.assertIn('id="catalogPageBtn"', public_html)
            self.assertIn('data-src="/catalog?embed=1"', public_html)
            self.assertNotIn('<a class="mini-btn" href="/catalog"', public_html)


class DiscoveryWorksTests(unittest.TestCase):
    def sample_video(self):
        return {
            "aweme_id": "7653132293346692365",
            "desc": "Demo short drama episode",
            "create_time": 1783900800,
            "author": {
                "unique_id": "demo.author",
                "nickname": "Demo Author",
            },
            "dramaInfo": {
                "dramaID": "7661844447575266321",
                "dramaName": "Demo full series",
                "numVideos": 31,
                "DramaVideoData": {"episodeNumber": 1},
            },
            "statistics": {
                "play_count": 12000,
                "digg_count": 800,
                "comment_count": 40,
                "share_count": 12,
            },
        }

    def test_direct_video_and_profile_links_are_classified_separately(self):
        video_url = "https://www.tiktok.com/@demo.author/video/7653132293346692365?lang=en"
        profile_url = "https://www.tiktok.com/@demo.author?lang=en"
        self.assertEqual(proxy._discovery_video_id(video_url), "7653132293346692365")
        self.assertEqual(proxy._discovery_account(video_url), "")
        self.assertEqual(proxy._discovery_account(profile_url), "demo.author")

    def test_discover_works_accepts_direct_video_link(self):
        video_url = "https://www.tiktok.com/@demo.author/video/7653132293346692365"
        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch.object(proxy, "DISCOVERED_WORKS_FILE", str(pathlib.Path(temp_dir) / "works.json")), \
                mock.patch.object(proxy, "_configured_schedule_accounts", return_value=(["demo.author"], "test")), \
                mock.patch.object(proxy, "_fetch_discovery_video_by_id", return_value=(self.sample_video(), "single-video")) as fetch:
            payload = proxy._discover_works(video_url, 10, 10)

        fetch.assert_called_once_with("7653132293346692365", mock.ANY)
        self.assertEqual(payload["mode"], "works")
        self.assertEqual(payload["count"], 1)
        work = payload["works"][0]
        self.assertEqual(work["video_id"], "7653132293346692365")
        self.assertEqual(work["account"], "demo.author")
        self.assertEqual(work["views"], 12000)
        self.assertEqual(work["likes"], 800)
        self.assertTrue(work["already_monitored"])
        self.assertEqual(work["drama_id"], "7661844447575266321")
        self.assertEqual(work["episode_count"], 31)

    def test_discover_works_accepts_tiktok_share_short_link(self):
        share_url = "https://www.tiktok.com/t/ZTFNEj8Hk/"
        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch.object(proxy, "DISCOVERED_WORKS_FILE", str(pathlib.Path(temp_dir) / "works.json")), \
                mock.patch.object(proxy, "_configured_schedule_accounts", return_value=([], "test")), \
                mock.patch.object(proxy, "_fetch_discovery_video_by_url", return_value=(self.sample_video(), "share-video")) as fetch:
            payload = proxy._discover_works(share_url, 10, 10)

        fetch.assert_called_once_with(share_url, mock.ANY)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["works"][0]["source_endpoint"], "share-video")

    def test_discover_works_accepts_profile_link(self):
        profile_url = "https://m.tiktok.com/@demo.author"
        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch.object(proxy, "DISCOVERED_WORKS_FILE", str(pathlib.Path(temp_dir) / "works.json")), \
                mock.patch.object(proxy, "_configured_schedule_accounts", return_value=([], "test")), \
                mock.patch.object(proxy, "_fetch_discovery_account_videos", return_value=([self.sample_video()], "account-posts")) as fetch:
            payload = proxy._discover_works(profile_url, 10, 10)

        fetch.assert_called_once_with("demo.author", 10, mock.ANY)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["works"][0]["source_endpoint"], "account-posts")

    def test_both_frontends_expose_account_and_work_modes(self):
        root = pathlib.Path(proxy.ROOT)
        for name in ("index.html", "tikhub-report-frontend.html"):
            page = (root / name).read_text(encoding="utf-8")
            self.assertIn('id="discoverModeSelect"', page)
            self.assertIn('<option value="works">作品</option>', page)
            self.assertIn('mode:discoveryMode', page)
            self.assertIn('粘贴 TikTok 作品链接、账号主页链接或输入关键词', page)
            self.assertNotIn('id="discoverSecretInput"', page)
            self.assertNotIn('id="discoverSecretSaveBtn"', page)
            self.assertNotIn('id="discoverLimitInput"', page)
            self.assertIn('id="backendSecretInline"', page)
            self.assertIn('粘贴 TikTok 账号主页链接或输入 @账号', page)
            self.assertIn('openDiscoveredSeries', page)
            self.assertIn('整剧列表/下载', page)
            self.assertNotIn('throw new Error("missing SCHEDULE_SECRET")', page)
        self.assertEqual(
            (root / "index.html").read_bytes(),
            (root / "tikhub-report-frontend.html").read_bytes(),
        )

    def test_public_dashboard_reads_latest_without_triggering_scrape(self):
        root = pathlib.Path(proxy.ROOT)
        for name in ("index.html", "tikhub-report-frontend.html"):
            page = (root / name).read_text(encoding="utf-8")
            self.assertIn("const PUBLIC_REPORT_REFRESH_MS=60*1000;", page)
            self.assertIn("const DASHBOARD_HISTORY_WORKERS=4;", page)
            self.assertIn('const DASHBOARD_CACHE_STORAGE="tikhub-dashboard-cache-v3";', page)
            self.assertIn('const DASHBOARD_VALIDATION_CACHE_KEY="thr_dashboard_validation_v1";', page)
            self.assertIn("const cachedStatePromise=readCachedDashboardStateAsync();", page)
            self.assertIn("const latestMetaPromise=loadPublicLatestMeta().catch(()=>null);", page)
            self.assertIn("const cacheTrusted=!!(cachedState&&dashboardValidationIsFresh(cachedMs));", page)
            self.assertIn("if(cacheTrusted)applyCachedDashboardState(cachedState);", page)
            self.assertIn("if(!cacheTrusted)applyCachedDashboardState(cachedState);", page)
            self.assertIn('const DASHBOARD_PREVIEW_CACHE_KEY="thr_dashboard_preview_v1";', page)
            self.assertIn("let previewReady=applyCachedDashboardPreview(cachedPreview);", page)
            self.assertIn("applyCachedDashboardPreview(cachedPreview,latestMetaMs)", page)
            self.assertIn("cacheDashboardPreview();", page)
            self.assertIn("cachedMs>=latestMetaMs-1000", page)
            self.assertIn("await dashboardCacheSet(DASHBOARD_LATEST_CACHE_KEY,latestPayload);", page)
            self.assertIn("function deferPublicHistoryRefresh(latestChanged)", page)
            self.assertIn("deferPublicHistoryRefresh(latestChanged);", page)
            self.assertIn("setTimeout(()=>{void refreshPublicHistoryInBackground(latestChanged);},1200);", page)
            self.assertIn("if(!isPublicMode()||!supabase.length)", page)
            self.assertIn('document.addEventListener("visibilitychange"', page)
            self.assertIn('window.addEventListener("focus",refreshPublicDashboardQuiet)', page)
            self.assertIn('id="publicPageNav"', page)
            self.assertIn('.public-page-nav .mini-btn[hidden]{display:none!important}', page)
            self.assertIn('id="publicDramaSearchBtn"', page)
            self.assertIn('id="publicDramaSearchForm"', page)
            self.assertIn('id="publicDramaSearchInput"', page)
            self.assertIn('id="publicPageHomeBtn"', page)
            self.assertIn('id="publicPagePrevBtn"', page)
            self.assertIn('id="publicPageNextBtn"', page)
            self.assertIn("function openPublicDramaSearch()", page)
            self.assertIn('detailSearchText=query;', page)
            self.assertIn('publicDramaSearchForm.addEventListener("submit"', page)
            self.assertIn('setPublicPageView("report");', page)
            self.assertIn('resultView="detail";', page)
            self.assertIn('publicPageHome.addEventListener("click",()=>setPublicPageView("dashboard"))', page)
            self.assertNotIn('class="page-switch', page)
            self.assertNotIn("setTimeout(loadPublicDashboard,600)", page)
            self.assertIn("loadPublicDashboard();", page)
            self.assertIn('await fetch(backendUrl(path,params)', page)
            self.assertNotIn('backendFetchJson("/run-scheduled",{wait:"1"}', page)
            self.assertNotIn("function tickScheduler()", page)
            self.assertNotIn("setInterval(tickScheduler", page)
            self.assertIn('backendFetchJson("/run-scheduled",{}, {requireSecret:true})', page)

    def test_dashboard_growth_uses_first_matching_history_for_new_accounts(self):
        root = pathlib.Path(proxy.ROOT)
        for name in ("index.html", "tikhub-report-frontend.html"):
            page = (root / name).read_text(encoding="utf-8")
            self.assertIn('"日增播放","周增播放"', page)
            self.assertIn("function dashboardDramaBaselinePayload", page)
            self.assertIn("dashboardDramaRowInPayload(payload,targetKeys)", page)
            self.assertIn("detailWeeklyGrowthOf", page)
            self.assertIn("首次监控", page)

    def test_discovery_endpoint_reads_separate_work_results(self):
        handler = object.__new__(proxy.Handler)
        handler.command = "GET"
        handler._require_schedule_secret = mock.Mock(return_value=True)
        handler.responses = []
        handler._send_json = lambda code, payload: handler.responses.append((code, payload))
        stored = {"ok": True, "mode": "works", "works": [{"video_id": "1"}]}
        with mock.patch.object(proxy, "_read_discovered_works", return_value=stored):
            handler._discover_accounts_endpoint({"mode": ["works"]})
        self.assertEqual(handler.responses, [(200, stored)])

    def test_series_target_resolves_work_to_full_drama_list(self):
        handler = object.__new__(proxy.Handler)
        handler.responses = []
        handler._require_private_access = mock.Mock(return_value=True)
        handler._send_json = lambda code, payload: handler.responses.append((code, payload))
        reference = {
            "account": "demo.author",
            "drama_id": "7661844447575266321",
            "drama_title": "Demo full series",
            "episode_count": 31,
            "source": "video-drama-info",
        }
        with mock.patch.object(proxy, "_resolve_drama_reference_for_video", return_value=reference) as resolve:
            handler._resolve_drama_link({
                "uid": ["demo.author"],
                "video_id": ["7653132293346692365"],
                "target": ["series"],
                "redirect": ["0"],
            })

        resolve.assert_called_once_with("demo.author", "7653132293346692365")
        handler._require_private_access.assert_called_once()
        self.assertEqual(handler.responses[0][0], 200)
        payload = handler.responses[0][1]
        self.assertEqual(payload["target"], "series")
        self.assertEqual(payload["episode_count"], 31)
        self.assertIn("target=list", payload["url"])
        self.assertIn("drama_id=7661844447575266321", payload["url"])


class RetentionTests(unittest.TestCase):
    def test_missing_latest_report_read_does_not_start_scrape(self):
        handler = object.__new__(proxy.Handler)
        handler._allow_report_read = mock.Mock(return_value=True)
        handler._send_json = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            reports = root / "reports"
            public = root / "public"
            reports.mkdir()
            public.mkdir()
            with mock.patch.object(proxy, "REPORTS_DIR", str(reports)), \
                    mock.patch.object(proxy, "PUBLIC_REPORTS_DIR", str(public)), \
                    mock.patch.object(proxy, "_execute_scheduled_job") as execute:
                handler._serve_report("/reports/latest_report.json", {})
        execute.assert_not_called()
        handler._send_json.assert_called_once_with(404, {"ok": False, "error": "report not found"})

    def test_report_route_serves_nested_episode_history_shard(self):
        handler = object.__new__(proxy.Handler)
        handler._allow_report_read = mock.Mock(return_value=True)
        handler._cors = mock.Mock()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._send_json = mock.Mock()
        handler.wfile = io.BytesIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            reports = root / "reports"
            public = root / "public"
            shard = reports / "episode_history" / "alpha.json"
            shard.parent.mkdir(parents=True)
            public.mkdir()
            shard.write_bytes(b'{"version":2}')
            with mock.patch.object(proxy, "REPORTS_DIR", str(reports)), \
                    mock.patch.object(proxy, "PUBLIC_REPORTS_DIR", str(public)):
                handler._serve_report("/reports/episode_history/alpha.json", {})
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), b'{"version":2}')
        handler._send_json.assert_not_called()

    def test_episode_history_reads_and_writes_only_one_account_shard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            runtime_dir = root / "runtime"
            public_dir = root / "public"
            public_dir.mkdir()
            (public_dir / "alpha.json").write_text(json.dumps({
                "version": 2,
                "account": "alpha",
                "items": {"alpha|1|10": {"uid": "alpha", "points": [{"ms": 1, "views": 2}]}},
            }), encoding="utf-8")
            (public_dir / "beta.json").write_text(json.dumps({
                "version": 2,
                "account": "beta",
                "items": {"beta|2|20": {"uid": "beta", "points": [{"ms": 1, "views": 3}]}},
            }), encoding="utf-8")
            with mock.patch.object(proxy, "DRAMA_EPISODE_HISTORY_DIR", str(runtime_dir)), \
                    mock.patch.object(proxy, "PUBLIC_DRAMA_EPISODE_HISTORY_DIR", str(public_dir)):
                history = proxy._read_drama_episode_history("@Alpha")
                self.assertEqual(set(history["items"]), {"alpha|1|10"})
                history["items"]["alpha|1|11"] = {"uid": "alpha", "points": []}
                proxy._write_drama_episode_history(history, "Alpha")
                stored = json.loads((runtime_dir / "alpha.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["account"], "alpha")
            self.assertEqual(set(stored["items"]), {"alpha|1|10", "alpha|1|11"})
            self.assertNotIn("beta|2|20", stored["items"])

    def test_runtime_cleanup_only_removes_archives_older_than_30_days(self):
        now = datetime.datetime(2026, 7, 10, 12, 0, tzinfo=proxy.BEIJING_TZ)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            old_names = {
                "scheduled_report_20260609-115959.json",
                "scheduled_report_20260609-115959.csv",
                "scheduled_dramas_20260609-115959.csv",
            }
            keep_names = {
                "scheduled_report_20260610-120000.json",
                "scheduled_dramas_20260709-120000.csv",
                "latest_report.json",
                "drama_episode_history.json",
            }
            for name in old_names | keep_names:
                (root / name).write_text("test", encoding="utf-8")
            with mock.patch.object(proxy, "REPORTS_DIR", temp_dir):
                status = proxy._cleanup_runtime_report_files(now)
            self.assertTrue(status["ok"])
            self.assertEqual(set(status["deleted"]), old_names)
            self.assertTrue(all(not (root / name).exists() for name in old_names))
            self.assertTrue(all((root / name).exists() for name in keep_names))

    def test_supabase_cleanup_deletes_children_before_old_runs(self):
        calls = []

        def fake_request(method, path, payload=None, prefer="", timeout=45):
            calls.append((method, path))
            if method == "GET":
                return [{"id": 11}, {"id": 12}]
            return None

        with mock.patch.object(proxy, "SUPABASE_ENABLED", True), \
                mock.patch.object(proxy, "SUPABASE_URL", "https://example.supabase.co"), \
                mock.patch.object(proxy, "SUPABASE_SERVICE_KEY", "test-key"), \
                mock.patch.object(proxy, "_supabase_request", side_effect=fake_request):
            status = proxy._cleanup_supabase_report_history(
                datetime.datetime(2026, 7, 10, 12, 0, tzinfo=proxy.BEIJING_TZ)
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["deleted_runs"], 2)
        self.assertEqual([method for method, _ in calls], ["GET", "DELETE", "DELETE", "DELETE"])
        self.assertIn("/account_snapshots?run_id=in.(11,12)", calls[1][1])
        self.assertIn("/drama_snapshots?run_id=in.(11,12)", calls[2][1])
        self.assertIn("/report_runs?id=in.(11,12)", calls[3][1])

    def test_episode_history_prunes_entries_without_recent_points(self):
        now_ms = 1_800_000_000_000
        day_ms = 86_400_000
        history = {
            "items": {
                "old": {"points": [{"ms": now_ms - 31 * day_ms, "views": 10}]},
                "mixed": {"points": [
                    {"ms": now_ms - 31 * day_ms, "views": 10},
                    {"ms": now_ms - 2 * day_ms, "views": 20},
                ]},
            }
        }
        deleted = proxy._prune_episode_history(history, now_ms)
        self.assertEqual(deleted, 1)
        self.assertNotIn("old", history["items"])
        self.assertEqual(len(history["items"]["mixed"]["points"]), 1)

    def test_dashboard_payload_is_compact_and_keeps_growth_fields(self):
        payload = {
            "generated_at": "2026-07-10T12:00:00+08:00",
            "summary": [{"账号": "demo", "昵称": "Demo", "短剧数": 2, "总集数": 20, "累计观看": 123}],
            "dramas_detail": [{
                "Account / 账号": "demo",
                "Drama ID / 短剧ID": "42",
                "English Title / 英文剧名": "Title",
                "Chinese Title / 中文剧名": "剧名",
                "Views / 观看数": 123,
                "English Description Preview / 英文简介预览": "x" * 1000,
            }],
        }
        compact = proxy._compact_report_payload(payload)
        self.assertEqual(compact["summary"][0]["a"], "demo")
        self.assertEqual(compact["dramas_detail"][0]["id"], "42")
        self.assertNotIn("English Description Preview / 英文简介预览", compact["dramas_detail"][0])
        self.assertLess(len(json.dumps(compact, ensure_ascii=False)), len(json.dumps(payload, ensure_ascii=False)))


class ScheduledConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.original_job = dict(proxy.LAST_JOB)

    def tearDown(self):
        proxy.LAST_JOB.clear()
        proxy.LAST_JOB.update(self.original_job)

    def test_even_rate_limiter_spaces_requests_without_bursts(self):
        now = [0.0]
        sleeps = []

        def clock():
            return now[0]

        def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        limiter = proxy._EvenRateLimiter(2, clock=clock, sleeper=sleeper)
        limiter.wait()
        limiter.wait()
        limiter.wait()

        self.assertEqual(len(sleeps), 2)
        self.assertAlmostEqual(sleeps[0], 0.5)
        self.assertAlmostEqual(sleeps[1], 0.5)

    def test_account_workers_are_bounded_and_preserve_configured_order(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def scrape(uid):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.015)
                if uid == "account-3":
                    raise RuntimeError("expected failure")
                return {"account": uid}, [{"drama": uid}]
            finally:
                with lock:
                    active -= 1

        accounts = ["account-%s" % index for index in range(8)]
        with mock.patch.object(proxy, "SCHEDULE_ACCOUNT_WORKERS", 3):
            rows, dramas, errors = proxy._scrape_scheduled_accounts(accounts, scrape=scrape)

        self.assertLessEqual(max_active, 3)
        self.assertGreaterEqual(max_active, 2)
        self.assertEqual([row["account"] for row in rows], [
            "account-0", "account-1", "account-2", "account-4",
            "account-5", "account-6", "account-7",
        ])
        self.assertEqual([row["drama"] for row in dramas], [
            "account-0", "account-1", "account-2", "account-4",
            "account-5", "account-6", "account-7",
        ])
        self.assertEqual(errors, [{"account": "account-3", "error": "expected failure"}])
        self.assertEqual(proxy.LAST_JOB["accounts_completed"], 8)
        self.assertEqual(proxy.LAST_JOB["accounts_succeeded"], 7)
        self.assertEqual(proxy.LAST_JOB["accounts_failed"], 1)

    def test_account_worker_pipeline_does_not_truncate_500_accounts(self):
        accounts = ["account-%03d" % index for index in range(500)]

        def scrape(uid):
            return {"account": uid}, []

        with mock.patch.object(proxy, "SCHEDULE_ACCOUNT_WORKERS", 4):
            rows, dramas, errors = proxy._scrape_scheduled_accounts(accounts, scrape=scrape)

        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]["account"], "account-000")
        self.assertEqual(rows[-1]["account"], "account-499")
        self.assertEqual(dramas, [])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

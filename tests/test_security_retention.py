import datetime
import json
import pathlib
import tempfile
import unittest
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
                mock.patch.object(proxy, "_get_video_play_url", return_value="https://media.example/video.mp4"):
            handler._resolve_drama_link({
                "uid": ["demo"],
                "video_id": ["123"],
                "target": ["play"],
                "redirect": ["0"],
            })

        self.assertEqual(handler.responses, [(200, {
            "ok": True,
            "url": "https://media.example/video.mp4",
            "target": "play",
        })])

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
        self.assertNotIn("?secret=", page)
        self.assertNotIn("&secret=", page)


class RetentionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

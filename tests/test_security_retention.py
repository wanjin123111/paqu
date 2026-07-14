import datetime
import io
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
        self.assertIn("target=zip", page)
        self.assertIn("target=local_script", page)
        self.assertNotIn("?secret=", page)
        self.assertNotIn("&secret=", page)


class LocalDownloaderScriptTests(unittest.TestCase):
    def test_job_collection_stays_an_array_after_finished_jobs_are_removed(self):
        summary = {
            "video_id": "123",
            "episode_no": 1,
            "title": "Episode 1",
        }
        with mock.patch.object(proxy, "_drama_episode_summary", return_value=summary), \
                mock.patch.object(proxy, "_episode_direct_play_url", return_value="https://media.example/1.mp4"):
            script_bytes, count, errors = proxy._build_drama_local_downloader_script(
                "demo", "drama-1", [{"aweme_id": "123"}]
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
            self.assertIn('作品链接或分享短链接', page)
            self.assertIn('id="discoverSecretInput"', page)
            self.assertIn('id="discoverSecretSaveBtn"', page)
            self.assertIn('openDiscoveredSeries', page)
            self.assertIn('整剧列表/下载', page)
            self.assertNotIn('throw new Error("missing SCHEDULE_SECRET")', page)
        self.assertEqual(
            (root / "index.html").read_bytes(),
            (root / "tikhub-report-frontend.html").read_bytes(),
        )

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


if __name__ == "__main__":
    unittest.main()

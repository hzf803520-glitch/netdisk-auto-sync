from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.executor_v2.browser_login import _search_refresh_token
from app.executor_v2.providers import _episode_info, _minimal_transfer_roots
from app.executor_v2.scheduler import ResourceScheduler
from app.executor_v2.store import ExecutorStore, normalize_settings


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "EXECUTOR_MASTER_KEY": "m" * 48,
                "EXECUTOR_DATA_DIR": self.temp_dir.name,
            },
        )
        self.env.start()
        self.store = ExecutorStore()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_secret_is_encrypted_at_rest(self) -> None:
        self.store.save_account(
            "baidu",
            {"cookie": "BDUSS=secret-value; STOKEN=another-secret"},
        )
        account = self.store.get_account("baidu", include_secret=True)
        self.assertEqual(
            account["secret"]["cookie"],
            "BDUSS=secret-value; STOKEN=another-secret",
        )
        with sqlite3.connect(self.store.path) as database:
            encrypted = database.execute(
                "SELECT encrypted_secret FROM accounts WHERE provider='baidu'"
            ).fetchone()[0]
        self.assertNotIn("secret-value", encrypted)

    def test_registration_is_idempotent_and_initially_due(self) -> None:
        resource = {
            "resourceKey": "resource-1",
            "provider": "quark",
            "title": "千香",
            "sourceUrl": "https://pan.quark.cn/s/example",
            "targetFolder": "自动转存/千香",
            "monitorEnabled": True,
        }
        self.assertEqual(
            self.store.register_resources([resource], interval_hours=3),
            (1, 0),
        )
        resource["title"] = "千香（更新）"
        self.assertEqual(
            self.store.register_resources([resource], interval_hours=3),
            (1, 0),
        )
        rows = self.store.due_resources()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "千香（更新）")
        self.assertEqual(rows[0]["pending_action"], "transfer")

    def test_manual_queue_runs_when_monitor_is_disabled(self) -> None:
        self.store.register_resources(
            [
                {
                    "resourceKey": "resource-2",
                    "provider": "uc",
                    "title": "测试",
                    "sourceUrl": "https://drive.uc.cn/s/example",
                    "targetFolder": "自动转存/测试",
                    "monitorEnabled": False,
                }
            ],
            interval_hours=3,
        )
        self.assertTrue(self.store.enqueue_resource("resource-2", "check"))
        due = self.store.due_resources()
        self.assertEqual(due[0]["pending_action"], "check")


class HelperTests(unittest.TestCase):
    def test_minimal_transfer_roots_do_not_duplicate_folder_children(self) -> None:
        files = [
            {
                "relPath": "Season 1",
                "parentPath": "",
                "dir": True,
            },
            {
                "relPath": "Season 1/E01.mkv",
                "parentPath": "Season 1",
                "dir": False,
            },
            {
                "relPath": "poster.jpg",
                "parentPath": "",
                "dir": False,
            },
        ]
        roots = _minimal_transfer_roots(files)
        self.assertEqual(
            [item["relPath"] for item in roots],
            ["Season 1", "poster.jpg"],
        )

    def test_episode_detection(self) -> None:
        files = [
            {"name": "千香.E01.1080p.mkv", "dir": False},
            {"name": "千香 第12集.mp4", "dir": False},
            {"name": "海报.jpg", "dir": False},
        ]
        self.assertEqual(_episode_info(files), "1–12 集")

    def test_nested_refresh_token_detection(self) -> None:
        token = "r" * 64
        payload = {"auth": '{"tokens":{"refresh_token":"' + token + '"}}'}
        self.assertEqual(_search_refresh_token(payload), token)

    def test_settings_floor_is_one_hour(self) -> None:
        self.assertEqual(
            normalize_settings({"checkIntervalHours": 0})["checkIntervalHours"],
            3,
        )
        self.assertEqual(
            normalize_settings({"checkIntervalHours": 1})["checkIntervalHours"],
            1,
        )


class SchedulerTests(unittest.TestCase):
    def test_success_clears_pending_action(self) -> None:
        with (
            tempfile.TemporaryDirectory() as data_dir,
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_MASTER_KEY": "k" * 48,
                    "EXECUTOR_DATA_DIR": data_dir,
                },
            ),
        ):
            store = ExecutorStore()
            store.register_resources(
                [
                    {
                        "resourceKey": "resource-3",
                        "provider": "baidu",
                        "title": "测试",
                        "sourceUrl": "https://pan.baidu.com/s/example",
                        "targetFolder": "自动转存/测试",
                    }
                ],
                interval_hours=3,
            )

            class FakeProviders:
                @staticmethod
                def sync_resource(resource, *, force_transfer=False):
                    return {
                        "status": "已同步",
                        "share_url": "https://pan.baidu.com/s/new",
                        "message": "完成",
                    }

            scheduler = ResourceScheduler(store, FakeProviders(), max_workers=1)
            resource = store.get_resource("resource-3")
            scheduler._run_resource(resource, "transfer")
            updated = store.get_resource("resource-3")
            self.assertEqual(updated["status"], "已同步")
            self.assertEqual(updated["pending_action"], "")
            self.assertGreater(updated["next_run_at"], 0)
            scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()

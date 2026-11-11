"""
Test nhanh cho logic cốt lõi của bot (không cần Discord/MongoDB thật).

Chạy:  python -m unittest discover -s tests -v

discord / flask / pymongo được thay bằng module giả (MagicMock) để test chạy
được ở mọi nơi (kể cả CI) mà không cần token hay kết nối mạng. Chỉ kiểm tra
LOGIC (điều kiện, thứ tự gọi, giá trị trả về) — không thay thế việc chạy thử bot
thật trên Discord.
"""
import asyncio
import os
import re
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

for _name in ["discord", "discord.ext", "discord.ext.commands", "discord.ext.tasks",
              "discord.app_commands", "discord.ui", "keep_alive", "flask",
              "pymongo", "pymongo.errors"]:
    sys.modules[_name] = MagicMock()
sys.modules["pymongo.errors"].DuplicateKeyError = type("DuplicateKeyError", (Exception,), {})
try:
    import requests  # noqa: F401
except Exception:  # pragma: no cover
    sys.modules["requests"] = MagicMock()

os.environ.setdefault("ALLOWED_CHANNEL_IDS", "1")
os.environ.setdefault("ALLOWED_ROLE_IDS", "")

import bot as botmod  # noqa: E402
import crawl_job  # noqa: E402
import db  # noqa: E402


class FakeLoop:
    async def run_in_executor(self, _executor, fn, *args):
        return fn(*args)


botmod.bot = SimpleNamespace(loop=FakeLoop())


def run(coro):
    return asyncio.run(coro)


class CheckAccessTests(unittest.TestCase):
    def setUp(self):
        botmod.ALLOWED_CHANNEL_IDS = {1}
        botmod.ALLOWED_ROLE_IDS = set()
        self.cfgs = {
            10: {"allowed_channel_ids": [], "allowed_role_ids": [], "configured": False},
            20: {"allowed_channel_ids": [], "allowed_role_ids": [], "configured": True},
            30: {"allowed_channel_ids": [5], "allowed_role_ids": [], "configured": True},
        }
        botmod.db = SimpleNamespace(get_guild_config=lambda gid: self.cfgs[gid])
        botmod._guild_config_cache.clear()

    def test_unconfigured_guild_uses_env_default(self):
        self.assertNotEqual(run(botmod.check_access(111, 10, 2, [])), "")
        self.assertEqual(run(botmod.check_access(111, 10, 1, [])), "")

    def test_cleared_guild_does_not_fall_back_to_env(self):
        self.assertEqual(run(botmod.check_access(111, 20, 2, [])), "")

    def test_configured_guild_replaces_env(self):
        self.assertEqual(run(botmod.check_access(111, 30, 5, [])), "")
        self.assertNotEqual(run(botmod.check_access(111, 30, 1, [])), "")


class CategoryCacheTests(unittest.TestCase):
    def test_failure_is_not_cached_and_stale_cache_is_used(self):
        state = {"fail": True}

        def fake_get_custom():
            if state["fail"]:
                raise RuntimeError("mongo down")
            return {"meo": {"label": "Mèo", "keywords": ["cats"], "nsfw": False}}

        botmod.db = SimpleNamespace(get_custom_categories=fake_get_custom)
        botmod._categories_cache.update({"data": None, "at": 0.0})
        self.assertEqual(run(botmod.get_all_categories_async()), {})
        self.assertIsNone(botmod._categories_cache["data"])

        state["fail"] = False
        self.assertIn("meo", run(botmod.get_all_categories_async()))

        botmod._categories_cache["at"] = time.monotonic() - 999
        state["fail"] = True
        self.assertIn("meo", run(botmod.get_all_categories_async()))


class CleanupTests(unittest.TestCase):
    def _check(self, **kw):
        def head(url, **_):
            if "exc" in kw:
                raise kw["exc"]
            return SimpleNamespace(status_code=kw["code"])
        with patch.object(botmod.requests, "head", head):
            return botmod._check_broken("u")

    def test_only_404_410_are_broken(self):
        self.assertEqual(self._check(code=404), "u")
        self.assertEqual(self._check(code=410), "u")
        for code in (200, 403, 405, 429, 500):
            self.assertIsNone(self._check(code=code), code)
        self.assertIsNone(self._check(exc=TimeoutError("x")))


class ClaimTests(unittest.TestCase):
    class Coll:
        def __init__(self, samples, lose_ids=()):
            self.samples, self.lose_ids, self.claims = list(samples), set(lose_ids), []

        def aggregate(self, _pipeline):
            return iter(self.samples.pop(0) if self.samples else [])

        def find_one_and_update(self, flt, upd, return_document=None):
            self.claims.append((flt, upd))
            return None if flt["_id"] in self.lose_ids else {"_id": flt["_id"], "image_url": "b"}

    def _with(self, coll):
        return patch.object(db, "get_db", lambda: {db.COLLECTION_NAME: coll})

    def test_lost_race_retries_with_another_image(self):
        c = self.Coll([[{"_id": "A"}], [{"_id": "B"}]], lose_ids={"A"})
        with self._with(c):
            doc = db.get_next_image("meo", [])
        self.assertEqual(doc["_id"], "B")
        self.assertEqual(len(c.claims), 2)
        self.assertTrue(all("$or" in f and "_id" in f for f, _ in c.claims))

    def test_gives_up_after_max_attempts(self):
        class Always(self.Coll):
            n = 0

            def aggregate(self, _p):
                Always.n += 1
                return iter([{"_id": f"X{Always.n}"}])
        c = Always([], lose_ids=set(f"X{i}" for i in range(1, 50)))
        with self._with(c):
            self.assertIsNone(db.get_random_image([], ["meo"]))
        self.assertEqual(len(c.claims), db.CLAIM_MAX_ATTEMPTS)

    def test_empty_stock_returns_none_without_claiming(self):
        c = self.Coll([])
        with self._with(c):
            self.assertIsNone(db.get_random_image([], ["meo"]))
        self.assertFalse(c.claims)

    def test_reserve_does_not_count_as_sent_but_normal_claim_does(self):
        c = self.Coll([[{"_id": "A"}], [{"_id": "B"}]])
        with self._with(c):
            db.get_next_image("meo", [], reserve=True)
            db.get_next_image("meo", [], reserve=False)
        reserve_update, normal_update = c.claims[0][1], c.claims[1][1]
        self.assertNotIn("$inc", reserve_update)
        self.assertTrue(reserve_update["$set"]["reserved"])
        self.assertEqual(normal_update["$inc"], {"sent_count": 1})
        # giữ chỗ phải tự khả dụng lại sau RESERVE_MINUTES (không phải COOLDOWN_HOURS)
        hold = reserve_update["$set"]["last_sent_at"]
        expires_in = hold + db.timedelta(hours=db.COOLDOWN_HOURS) - db.now_utc()
        self.assertAlmostEqual(expires_in.total_seconds(), db.RESERVE_MINUTES * 60, delta=5)

    def test_confirm_image_sent_only_touches_reserved(self):
        calls = []

        class Coll:
            def update_one(self, flt, upd):
                calls.append((flt, upd))
                return SimpleNamespace(modified_count=1)
        with patch.object(db, "get_db", lambda: {db.COLLECTION_NAME: Coll()}):
            self.assertTrue(db.confirm_image_sent("u"))
        self.assertEqual(calls[0][0], {"image_url": "u", "reserved": True})
        self.assertEqual(calls[0][1]["$inc"], {"sent_count": 1})


class ReportAndStreakTests(unittest.TestCase):
    def test_report_counts_distinct_reporters(self):
        class Coll:
            def find_one_and_update(self, flt, upd, return_document=None):
                assert "$addToSet" in upd
                return {"reporters": [1, 2]}
        with patch.object(db, "get_db", lambda: {db.COLLECTION_NAME: Coll()}):
            self.assertEqual(db.report_image("u", 2), 2)

    def test_report_missing_image_returns_zero(self):
        class Coll:
            def find_one_and_update(self, *a, **k):
                return None
        with patch.object(db, "get_db", lambda: {db.COLLECTION_NAME: Coll()}):
            self.assertEqual(db.report_image("u", 2), 0)

    def test_failure_streak(self):
        deleted, inc = [], {"n": 0}

        class Meta:
            def delete_one(self, flt):
                deleted.append(flt)

            def find_one_and_update(self, *a, **k):
                inc["n"] += 1
                return {"count": inc["n"]}
        with patch.object(db, "get_db", lambda: {db.META_COLLECTION: Meta()}):
            self.assertEqual(db.update_failure_streak("meo", True), 1)
            self.assertEqual(db.update_failure_streak("meo", True), 2)
            self.assertEqual(db.update_failure_streak("meo", False), 0)
        self.assertEqual(deleted, [{"_id": "fail_streak_meo"}])


class ReportButtonTests(unittest.TestCase):
    def _run(self, session, broken, reports=0, next_url="http://x/new.jpg"):
        calls = {"deleted": [], "removed": [], "index": [], "edits": [], "followups": [], "confirmed": []}

        async def defer(*a, **k):
            return True

        async def edit(embed=None, view=None):
            calls["edits"].append(embed)

        async def followup(msg, ephemeral=False):
            calls["followups"].append(msg)

        async def fetch_next(cat, exclude, reserve=False):
            return next_url

        interaction = SimpleNamespace(
            message=SimpleNamespace(id=42), user=SimpleNamespace(id=7),
            data={"custom_id": "paginator:report"},
            edit_original_response=edit, followup=SimpleNamespace(send=followup),
        )
        botmod.db = SimpleNamespace(
            get_paginator_session=lambda mid: session,
            delete_images_by_url=lambda urls: calls["deleted"].extend(urls),
            report_image=lambda url, uid: reports,
            remove_url_from_session=lambda mid, url: calls["removed"].append(url),
            update_paginator_index=lambda mid, i: calls["index"].append(i),
            confirm_image_sent=lambda url: calls["confirmed"].append(url),
            append_image_and_set_index=lambda mid, url, i: calls["index"].append(("append", url, i)),
        )
        with patch.object(botmod, "_timed_defer", defer), \
                patch.object(botmod, "_check_broken", lambda u: u if broken else None), \
                patch.object(botmod, "_fetch_next_image_url", fetch_next), \
                patch.object(botmod, "_build_image_embed", lambda label, url: url):
            run(botmod._do_report_image(interaction))
        return calls

    def _session(self, images, index):
        return {"author_id": 7, "images": images, "index": index, "label": "Mèo", "category_key": "meo"}

    def test_dead_image_is_deleted_immediately_and_next_shown(self):
        c = self._run(self._session(["a", "b", "c"], 1), broken=True)
        self.assertEqual(c["deleted"], ["b"])
        self.assertEqual(c["removed"], ["b"])
        self.assertEqual(c["edits"], ["c"])
        self.assertEqual(c["confirmed"], ["c"])
        self.assertIn("Đã xoá", c["followups"][0])

    def test_live_image_only_deleted_at_threshold(self):
        c = self._run(self._session(["a", "b"], 0), broken=False, reports=1)
        self.assertEqual(c["deleted"], [])
        self.assertEqual(c["removed"], ["a"])
        c = self._run(self._session(["a", "b"], 0), broken=False, reports=botmod.REPORT_DELETE_THRESHOLD)
        self.assertEqual(c["deleted"], ["a"])

    def test_last_image_in_session_fetches_a_new_one(self):
        c = self._run(self._session(["a"], 0), broken=True)
        self.assertEqual(c["edits"], ["http://x/new.jpg"])
        self.assertIn(("append", "http://x/new.jpg", 0), c["index"])

    def test_other_users_cannot_report(self):
        s = self._session(["a"], 0)
        s["author_id"] = 999
        c = self._run(s, broken=True)
        self.assertEqual(c["deleted"], [])
        self.assertIn("không thể", c["followups"][0])


class GuildConfigTests(unittest.TestCase):
    def _cfg(self, doc):
        coll = SimpleNamespace(find_one=lambda q: doc)
        with patch.object(db, "get_db", lambda: {db.GUILD_CONFIGS_COLLECTION: coll}):
            return db.get_guild_config(1)

    def test_configured_flag(self):
        self.assertFalse(self._cfg(None)["configured"])
        self.assertFalse(self._cfg({"_id": 1, "setup_category_id": 9})["configured"])
        self.assertTrue(self._cfg({"_id": 1, "allowed_channel_ids": []})["configured"])


class CrawlJobTests(unittest.TestCase):
    def test_db_error_raises(self):
        crawl_job.get_custom_categories = lambda: (_ for _ in ()).throw(RuntimeError("mongo down"))
        with self.assertRaises(RuntimeError):
            crawl_job.main()

    def test_no_categories_alerts_and_skips_last_crawl(self):
        calls = {"set_last": 0, "alert": 0}
        crawl_job.get_custom_categories = lambda: {}
        crawl_job.set_last_crawl_time = lambda: calls.__setitem__("set_last", calls["set_last"] + 1)
        crawl_job.send_discord_alert = lambda m: calls.__setitem__("alert", calls["alert"] + 1)
        crawl_job.CATEGORIES = {}
        crawl_job.main()
        self.assertEqual(calls, {"set_last": 0, "alert": 1})

    def test_polite_pause(self):
        sleeps = []
        crawl_job._last_request_done_at = None
        with patch.object(crawl_job.time, "sleep", lambda s: sleeps.append(s)):
            crawl_job._polite_pause()
            self.assertEqual(sleeps, [])
            crawl_job._mark_request_done()
            crawl_job._polite_pause()
            self.assertEqual(len(sleeps), 1)
            self.assertLessEqual(sleeps[0], crawl_job.REQUEST_DELAY_MAX_SECONDS)
            crawl_job._last_request_done_at = time.monotonic() - 10
            crawl_job._polite_pause()
            self.assertEqual(len(sleeps), 1)

    def test_workflow_has_no_push_trigger(self):
        with open(os.path.join(REPO, ".github", "workflows", "main.yml"), encoding="utf-8") as f:
            wf = f.read()
        self.assertIsNone(re.search(r"^\s*push:", wf, re.M))
        self.assertIn("schedule:", wf)
        self.assertIn("workflow_dispatch:", wf)


if __name__ == "__main__":
    unittest.main()

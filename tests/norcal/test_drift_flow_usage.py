import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "norcal/health/scripts/drift_flow.py"

class TestDriftFlowUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("drift_flow_usage", SOURCE)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.STATE_DIR = self.root / "state"
        self.mod.STATE = self.mod.STATE_DIR / "state.json"
        self.mod.LEDGER = self.mod.STATE_DIR / "ledger.jsonl"
        stub = self.root / "executor"
        stub.write_text("#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\nPath("+repr(str(self.root/"argv.json"))+").write_text(json.dumps(sys.argv[1:]))\nprint(json.dumps({'id':'t_test'}))\n")
        stub.chmod(0o755)
        self.mod.HERMES = str(stub)
        self.status = {}
        self.mod.task_status = lambda tid: self.status.get(tid)
        self.clock = dt.datetime(2026,10,14,20,tzinfo=dt.timezone.utc)
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("DRIFT_FLOW_NO_HANDOFF", None)
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def process(self, lines, seconds=0):
        buf = io.StringIO()
        with patch.object(self.mod, "now", return_value=self.clock+dt.timedelta(seconds=seconds)), contextlib.redirect_stdout(buf):
            self.mod.process("test", "true", lines)
        return buf.getvalue()
    def dirty(self, subject="services/life-wiki/vault"):
        return ["F\tdirty\t"+subject+"\t2 modified notes\n", "C\t"+subject+"\n"]
    def test_transient_vault_write_settles_without_executor_or_alert(self):
        self.assertEqual(self.process(self.dirty()), "")
        self.assertEqual(self.process(["C\tservices/life-wiki/vault\n"], 60), "")
        self.assertEqual(self.mod.load(), {})
        self.assertFalse((self.root/"argv.json").exists())
    def test_repeated_checks_inside_sync_grace_do_not_spend_executor(self):
        self.process(self.dirty())
        self.assertEqual(self.process(self.dirty(), 15*60), "")
        self.assertFalse((self.root/"argv.json").exists())
    def test_persistent_vault_write_hands_off_once_to_normal_worker(self):
        self.process(self.dirty())
        self.assertIn("t_test", self.process(self.dirty(), 20*60))
        args=json.loads((self.root/"argv.json").read_text())
        self.assertEqual(args[args.index("--assignee")+1], "default")
        self.assertNotIn("--executor-lane", args)
        self.assertEqual(self.process(self.dirty(), 21*60), "")
        self.assertEqual(self.mod.load()["dirty:services/life-wiki/vault"]["first_seen"], self.mod.iso(self.clock))
    def test_other_checkout_dirty_remains_immediate_on_existing_lane(self):
        self.assertIn("t_test",self.process(self.dirty("company/checkout")))
        args=json.loads((self.root/"argv.json").read_text())
        self.assertEqual(args[args.index("--executor-lane")+1], "claude")
    def test_permission_failure_does_not_report_pending_dirty_as_resolved(self):
        self.process(self.dirty())
        out=self.process(["F\tcheck_failed\tservices/life-wiki/vault\tpermission denied\n", "C\tservices/life-wiki/vault\n"],60)
        self.assertNotIn("Drift repaired",out)
        self.assertTrue(self.mod.load()["dirty:services/life-wiki/vault"]["pending"])
    def test_observed_clean_repository_still_closes_alerted_finding(self):
        self.process(self.dirty())
        self.process(self.dirty(),20*60)
        self.assertIn("Drift repaired", self.process(["C\tservices/life-wiki/vault\n"],21*60))
        self.assertEqual(self.mod.load(),{})

    # --- #alerts drift rules (t_38093000, Christopher 2026-10-16) ---
    def behind(self, subject="hermes/repos/acme"):
        return ["F\tbehind\t"+subject+"\t3 commit(s) behind origin/main\n", "C\t"+subject+"\n"]
    def clean(self, subject="hermes/repos/acme"):
        return ["C\t"+subject+"\n"]
    def argv(self):
        return json.loads((self.root/"argv.json").read_text())
    def test_drift_detected_then_repaired_posts_exactly_two_messages(self):
        first = self.process(self.behind())
        self.assertIn("Drift found: acme", first)
        self.assertIn("Repair started (task t_test).", first)
        self.assertNotIn("Needs you", first)
        # Same drift on later ticks, including past the old 24h reminder: silent.
        for secs in (60, 3600, 25*3600, 49*3600):
            self.assertEqual(self.process(self.behind(), secs), "")
        done = self.process(self.clean(), 50*3600)
        self.assertIn("Drift repaired: acme now matches.", done)
        self.assertEqual(self.mod.load(), {})
        self.assertEqual(self.process(self.clean(), 51*3600), "")
    def test_direction_change_of_same_drift_is_not_reposted(self):
        self.process(self.behind())
        self.assertEqual(self.process(["F\tdiverged\thermes/repos/acme\t1 ahead 3 behind\n",
                                       "C\thermes/repos/acme\n"], 60), "")
        rec = self.mod.load()["sync:hermes/repos/acme"]
        self.assertEqual((rec["kind"], rec["task"]), ("diverged", "t_test"))
    def test_failed_repair_starts_new_repair_silently_no_cap(self):
        self.process(self.behind())
        first_key = self.argv()[self.argv().index("--idempotency-key")+1]
        # Repair card is still working: no new card.
        self.status["t_test"] = "running"
        (self.root/"argv.json").unlink()
        self.assertEqual(self.process(self.behind(), 60), "")
        self.assertFalse((self.root/"argv.json").exists())
        keys = {first_key}
        for n, secs in ((2, 120), (3, 180), (4, 240)):
            # The repair card ended but the drift is still there.
            self.status["t_test"] = "done"
            self.assertEqual(self.process(self.behind(), secs), "")
            args = self.argv()
            body = args[args.index("--body")+1]
            self.assertIn(f"REPAIR ATTEMPT {n}", body)
            self.assertIn("different approach", body)
            keys.add(args[args.index("--idempotency-key")+1])
            self.assertEqual(self.mod.load()["sync:hermes/repos/acme"]["attempt"], n)
        self.assertEqual(len(keys), 4)
        self.assertIn("Drift repaired: acme now matches.", self.process(self.clean(), 300))
    def test_repair_that_cannot_start_posts_one_needs_you(self):
        os.environ["DRIFT_FLOW_NO_HANDOFF"] = "1"
        out = self.process(self.behind())
        self.assertIn("Repair could NOT start", out)
        self.assertIn("Needs you.", out)
        self.assertEqual(self.process(self.behind(), 60), "")
        # Once a repair card can be opened, say so once.
        os.environ.pop("DRIFT_FLOW_NO_HANDOFF")
        self.assertIn("Drift repair started: acme", self.process(self.behind(), 120))
        self.assertEqual(self.process(self.behind(), 180), "")
    def test_repair_body_no_longer_offers_giving_up(self):
        self.process(self.behind())
        body = self.argv()[self.argv().index("--body")+1]
        self.assertNotIn("If you cannot resolve it", body)
        self.assertIn("no attempt cap", body)
    def mark(self, fp, field, text, seconds):
        buf = io.StringIO()
        with patch.object(self.mod, "now", return_value=self.clock+dt.timedelta(seconds=seconds)), contextlib.redirect_stdout(buf):
            self.mod.mark(fp, field, text)
        return buf.getvalue()
    def test_note_after_resolution_records_without_reopening(self):
        subject = "company/checkout"
        fp = f"sync:{subject}"
        self.assertIn("t_test", self.process([f"F\tahead\t{subject}\tdrift detail\n", f"C\t{subject}\n"]))
        self.assertIn("Drift repaired", self.process([f"C\t{subject}\n"], 60))
        self.assertEqual(self.mod.load(), {})
        out = self.mark(fp, "note", "repair complete, see evidence", 90)
        self.assertIn(f"recorded note for {fp}", out)
        self.assertEqual(self.mod.load(), {})
        rows = [json.loads(l) for l in self.mod.LEDGER.read_text().splitlines()]
        self.assertEqual(rows[-1]["event"], "note")
        self.assertTrue(rows[-1].get("post_resolution"))
        self.assertEqual(rows[-1]["fp"], fp)

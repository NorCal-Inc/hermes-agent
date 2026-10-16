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
        self.assertNotIn("Drift resolved",out)
        self.assertTrue(self.mod.load()["dirty:services/life-wiki/vault"]["pending"])
    def test_observed_clean_repository_still_closes_alerted_finding(self):
        self.process(self.dirty())
        self.process(self.dirty(),20*60)
        self.assertIn("Drift resolved", self.process(["C\tservices/life-wiki/vault\n"],21*60))
        self.assertEqual(self.mod.load(),{})
    def mark(self, fp, field, text, seconds):
        buf = io.StringIO()
        with patch.object(self.mod, "now", return_value=self.clock+dt.timedelta(seconds=seconds)), contextlib.redirect_stdout(buf):
            self.mod.mark(fp, field, text)
        return buf.getvalue()
    def test_note_after_resolution_records_without_reopening(self):
        subject = "company/checkout"
        fp = f"sync:{subject}"
        self.assertIn("t_test", self.process([f"F\tahead\t{subject}\tdrift detail\n", f"C\t{subject}\n"]))
        self.assertIn("Drift resolved", self.process([f"C\t{subject}\n"], 60))
        self.assertEqual(self.mod.load(), {})
        out = self.mark(fp, "note", "repair complete, see evidence", 90)
        self.assertIn(f"recorded note for {fp}", out)
        self.assertEqual(self.mod.load(), {})
        rows = [json.loads(l) for l in self.mod.LEDGER.read_text().splitlines()]
        self.assertEqual(rows[-1]["event"], "note")
        self.assertTrue(rows[-1].get("post_resolution"))
        self.assertEqual(rows[-1]["fp"], fp)

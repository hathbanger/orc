"""In hidden mode the fixtures overwrite their paths for every check, so a
worker's edit to such a path is inert: it neither tampers with nor stands in
for the grade. Before this, an ordinary added test in the same file read as
`tampered` through the workflow's check-input tracking."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_gym as gym

F2P = ["python3", "-m", "unittest", "test.handoff_test.T.test_new"]
P2P = ["python3", "-m", "unittest", "test.handoff_test.T.test_old"]
TASK = {"id": "pr-1", "checks": {"fail_to_pass": [F2P], "pass_to_pass": [P2P]},
        "hidden": {"fixtures": [{"path": "test/handoff_test.py"}]}}


def outcome(check_inputs_changed, f2p="passed", p2p="passed"):
    receipts = [{"argv": F2P, "status": f2p, "before": {"status": "failed"}},
                {"argv": P2P, "status": p2p, "before": {"status": "passed"}}]
    return {"status": "success", "nodes": [{"result": {"run_id": "r1", "acceptance_checks": receipts,
                                                       "check_inputs_changed": check_inputs_changed}}]}


class HiddenFixtureEditTest(unittest.TestCase):
    def summarize(self, changed, inputs, mode="hidden", edits=None, **checks):
        return gym.summarize(TASK, "lane", {"agent": "opencode"}, outcome(inputs, **checks), changed, 1000, mode,
                             {"test/handoff_test.py": {"deleted": 0, "defines": []}} if edits is None else edits)

    def test_adding_to_the_hidden_test_file_is_not_tampering(self):
        row = self.summarize(["fix.py", "test/handoff_test.py"], ["test/handoff_test.py"])
        self.assertEqual(row["verdict"], "solved")
        self.assertEqual((row["tampered"], row["check_inputs_changed"]), ([], []))
        self.assertEqual(row["touched_fixtures"], ["test/handoff_test.py"])

    def test_rewriting_existing_hidden_tests_still_tampers(self):
        for edits in ({"test/handoff_test.py": {"deleted": 3, "defines": []}},
                      {"test/handoff_test.py": {"deleted": 0, "defines": ["test_new"]}},   # forged stand-in
                      {"test/handoff_test.py": None}, {}):
            with self.subTest(edits=edits):
                row = self.summarize(["fix.py", "test/handoff_test.py"], ["test/handoff_test.py"], edits=edits)
                self.assertEqual(row["verdict"], "tampered")

    def test_file_edit_reads_deletions_and_forged_names(self):
        patch = ("diff --git a/test/handoff_test.py b/test/handoff_test.py\n--- a/test/handoff_test.py\n"
                 "+++ b/test/handoff_test.py\n@@ -1 +1,5 @@\n def test_old(self): pass\n"
                 "+    def test_mine(self):\n+        pass\n+    def test_new(self):\n+        pass\n")
        self.assertEqual(gym.check_test_names(TASK), {"test_new", "test_old"})
        self.assertEqual(gym._file_edit(patch, "test/handoff_test.py", {"test_new"}), {"deleted": 0, "defines": ["test_new"]})
        self.assertEqual(gym._file_edit(patch, "test/handoff_test.py", set()), {"deleted": 0, "defines": []})
        self.assertIsNone(gym._file_edit(patch, "other.py", {"test_new"}))

    def test_other_check_inputs_still_tamper_in_hidden_mode(self):
        row = self.summarize(["fix.py", "conftest.py", "test/handoff_test.py"], ["conftest.py", "test/handoff_test.py"])
        self.assertEqual(row["verdict"], "tampered")
        self.assertEqual(row["tampered"], ["conftest.py"])

    def test_visible_mode_is_unchanged(self):
        row = self.summarize(["fix.py", "test/handoff_test.py"], ["test/handoff_test.py"], mode="visible")
        self.assertEqual(row["verdict"], "tampered")

    def test_a_regression_is_still_a_regression(self):
        row = self.summarize(["fix.py", "test/handoff_test.py"], ["test/handoff_test.py"], p2p="failed")
        self.assertEqual(row["verdict"], "regressed")

    def test_recorded_rows_are_regraded_on_read(self):
        legacy = {"event": "finished", "kind": "fix", "mode": "hidden", "key": "pr-1:lane:hidden", "lane": "lane",
                  "task": "pr-1", "completed": True, "baseline_ok": True, "verdict": "tampered",
                  "f2p_passed": True, "p2p_regressed": False, "tampered": ["test/handoff_test.py"],
                  "check_inputs_changed": ["test/handoff_test.py"], "touched_fixtures": ["test/handoff_test.py"]}
        genuine = {**legacy, "key": "pr-2:lane:hidden", "task": "pr-2",
                   "tampered": ["conftest.py", "test/handoff_test.py"],
                   "check_inputs_changed": ["conftest.py", "test/handoff_test.py"]}
        added = ("diff --git a/test/handoff_test.py b/test/handoff_test.py\n--- a/test/handoff_test.py\n"
                 "+++ b/test/handoff_test.py\n@@ -1,2 +1,4 @@\n def test_old(self): pass\n+def test_mine(self):\n+    pass\n")
        rewritten = added.replace(" def test_old(self): pass\n", "-def test_old(self): pass\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "added.patch").write_text(added)
            (root / "rewritten.patch").write_text(rewritten)
            forger = {**legacy, "key": "pr-3:lane:hidden", "task": "pr-3", "patch": str(root / "rewritten.patch")}
            unsaved = {**legacy, "key": "pr-4:lane:hidden", "task": "pr-4"}
            legacy["patch"] = str(root / "added.patch")
            (root / "results.jsonl").write_text("\n".join(json.dumps(r) for r in (legacy, genuine, forger, unsaved)) + "\n")
            rows = {row["key"]: row for row in gym.read_results(directory)}
        self.assertEqual(rows["pr-1:lane:hidden"]["verdict"], "solved")
        self.assertEqual(rows["pr-1:lane:hidden"]["regraded"], "hidden_fixture_edit")
        for key in ("pr-2:lane:hidden", "pr-3:lane:hidden", "pr-4:lane:hidden"):
            self.assertEqual(rows[key]["verdict"], "tampered", key)
            self.assertNotIn("regraded", rows[key])


if __name__ == "__main__":
    unittest.main()

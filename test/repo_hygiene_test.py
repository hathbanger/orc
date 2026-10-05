"""The public repository carries ORC only: no archives, and no private projects' handoff documents."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = re.compile(r"(^|/)(OASIS|TENET)_[A-Z0-9_]*\d{4}-\d{2}-\d{2}\.(md|zip)$|(^|/)ORC_TENET_REVIEW_")


class RepoHygieneTest(unittest.TestCase):
    def tracked(self):
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        return listing.stdout.splitlines()

    def test_no_archives_are_committed(self):
        self.assertEqual([path for path in self.tracked() if path.lower().endswith((".zip", ".tar", ".tgz", ".tar.gz", ".7z"))], [])

    def test_no_private_handoff_documents_are_committed(self):
        self.assertEqual([path for path in self.tracked() if HANDOFF.search(path)], [])
        self.assertTrue(HANDOFF.search("OASIS_DOGFOOD_RUN_2026-09-23.md"))
        self.assertTrue(HANDOFF.search("TENET_VNEXT_ANALYSIS_2026-09-23.md"))
        self.assertFalse(HANDOFF.search("docs/routing.md"))


if __name__ == "__main__":
    unittest.main()

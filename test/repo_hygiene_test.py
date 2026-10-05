"""The public repository carries ORC only: no archives, and no private projects' handoff documents."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
# A dated, all-caps handoff document at the repository root: another project's
# hand-over notes, not ORC documentation (which lives under docs/).
HANDOFF = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*_\d{4}-\d{2}-\d{2}\.(md|zip)$")


class RepoHygieneTest(unittest.TestCase):
    def tracked(self):
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        return listing.stdout.splitlines()

    def test_no_archives_are_committed(self):
        self.assertEqual([path for path in self.tracked() if path.lower().endswith((".zip", ".tar", ".tgz", ".tar.gz", ".7z"))], [])

    def test_no_submodule_gitlinks_are_committed(self):
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s"], capture_output=True, text=True)
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        self.assertEqual([line.split("\t", 1)[-1] for line in listing.stdout.splitlines() if line.startswith("160000 ")], [])
        self.assertIn(".claude/worktrees/", (ROOT / ".gitignore").read_text().splitlines())

    def test_no_private_handoff_documents_are_committed(self):
        self.assertEqual([path for path in self.tracked() if HANDOFF.search(path)], [])
        self.assertTrue(HANDOFF.search("PROJECT_HANDOFF_2026-09-23.md"))
        self.assertTrue(HANDOFF.search("PRODUCT_REVIEW_2026-09-23.zip"))
        self.assertFalse(HANDOFF.search("docs/routing.md"))
        self.assertFalse(HANDOFF.search("README.md"))


if __name__ == "__main__":
    unittest.main()

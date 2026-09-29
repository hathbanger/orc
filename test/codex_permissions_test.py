"""Git permissions for writers; sandbox probes run commands, never model calls."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core


class CodexPermissionsTest(unittest.TestCase):
    def test_readers_cannot_inherit_writer_permissions(self):
        task = {"agent": "codex", "workspace": "/tmp/fusion-reader", "write": False}
        config = core.deep_merge(core.DEFAULTS, {"codex": {"sandbox": "danger-full-access", "approval": "on-request"}})
        for session in (None, "saved-session"):
            argv, _, _ = core.agent_command(config, task, session)
            self.assertEqual(argv[argv.index("-s") + 1], "read-only")
            self.assertEqual(argv[argv.index("-a") + 1], "never")
            self.assertFalse(any("fusion_git_write" in arg for arg in argv))

    def test_fresh_and_resumed_writers_use_the_same_scoped_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            task = {"agent": "codex", "workspace": directory, "write": True}
            for session in (None, "saved-session"):
                argv, _, _ = core.agent_command(core.DEFAULTS, task, session)
                self.assertIn('default_permissions="fusion_git_write"', argv)
                self.assertNotIn("danger-full-access", argv)
                self.assertNotIn("-s", argv)
                self.assertEqual("resume" in argv, bool(session))
            config = core.deep_merge(core.DEFAULTS, {"codex": {"git_write": False}})
            argv, _, _ = core.agent_command(config, task, None)
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")

    def test_network_table_reaches_the_writer_profile_and_limited_mode_starts_the_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            network = {"enabled": True, "allow_local_binding": True, "mode": "limited",
                       "domains": {"pypi.org": "allow", "*.npmjs.org": "allow"}}
            args = core.codex_permission_args(Path(directory), {**core.DEFAULTS["codex"], "network": network}, True)
            profile = next(arg for arg in args if arg.startswith("permissions.fusion_git_write="))
            self.assertIn(',network={"enabled"=true,"allow_local_binding"=true,"mode"="limited",'
                          '"domains"={"pypi.org"="allow","*.npmjs.org"="allow"}}}', profile)
            self.assertEqual(args[args.index("features.network_proxy=true") - 1], "-c")
            unlimited = core.codex_permission_args(Path(directory), {**core.DEFAULTS["codex"], "network": {"enabled": True}}, True)
            self.assertNotIn("features.network_proxy=true", unlimited)
            reader = core.codex_permission_args(Path(directory), {**core.DEFAULTS["codex"], "network": network}, False)
            self.assertEqual(reader, ["-s", "read-only"])
            with self.assertRaisesRegex(ValueError, "codex.network"):
                core.codex_permission_args(Path(directory), {**core.DEFAULTS["codex"], "network": "on"}, True)

    def test_default_writer_profile_has_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            args = core.codex_permission_args(Path(directory), core.DEFAULTS["codex"], True)
            self.assertFalse(any("network" in arg for arg in args))

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("codex"), "requires the macOS Codex sandbox")
    def test_real_sandbox_allows_git_in_repos_and_worktrees_but_protects_other_paths(self):
        help_text = subprocess.run(["codex", "sandbox", "--help"], capture_output=True, text=True, timeout=10).stdout
        if "--permission-profile" not in help_text:
            self.skipTest("installed Codex does not support named permission profiles")
        # Outside system temp so the sibling is not covered by the temp grant.
        with tempfile.TemporaryDirectory(prefix=".orc-sandbox-test-", dir=Path.home()) as directory:
            root = Path(directory)
            repo = root / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            git = ["git", "-C", str(repo), "-c", "user.name=ORC test", "-c", "user.email=test@example.invalid"]
            subprocess.run([*git, "commit", "--allow-empty", "-qm", "fixture"], check=True)
            worktree = root / "worktree"
            subprocess.run([*git, "worktree", "add", "-qb", "fixture-worktree", str(worktree)], check=True)
            script = '''
import pathlib, subprocess, sys
repo, outside, branch = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
git = ['git', '-C', str(repo), '-c', 'user.name=ORC test', '-c', 'user.email=test@example.invalid']
subprocess.run([*git, 'switch', '-qc', branch], check=True)
(repo / 'change.txt').write_text('verified edit')
subprocess.run([*git, 'add', 'change.txt'], check=True)
subprocess.run([*git, 'commit', '-qm', 'verified Git write'], check=True)
for protected in (repo / '.codex/config.toml', outside):
    try:
        protected.write_text('must be denied')
    except PermissionError:
        continue
    raise AssertionError(f'Unexpected write access: {protected}')
print('branch, stage, commit succeeded; protected writes denied')
'''
            for workspace in (repo, worktree):
                (workspace / ".codex").mkdir()
                (workspace / ".codex/config.toml").write_text("")
                args = core.codex_permission_args(workspace, core.DEFAULTS["codex"], True)
                args.remove("--strict-config")  # Supported by exec, not the sandbox probe command.
                result = subprocess.run(
                    ["codex", *args, "sandbox", "-P", "fusion_git_write", "-C", str(workspace), "--",
                     sys.executable, "-c", script, str(workspace), str(root / "outside.txt"), "check-" + workspace.name],
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("protected writes denied", result.stdout)
                denied = subprocess.run(
                    ["codex", "sandbox", "-P", ":read-only", "-C", str(workspace), "--", "git", "branch", "reader-write"],
                    capture_output=True, text=True, timeout=20,
                )
                self.assertNotEqual(denied.returncode, 0)


if __name__ == "__main__":
    unittest.main()

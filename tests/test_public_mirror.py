import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.sync_public_mirror import PUBLIC_MIRROR_BRANCH, PublicMirrorGitError, sync_snapshot


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class PublicMirrorTests(unittest.TestCase):
    def test_workflow_runs_on_private_master_push_and_has_required_safety_gates(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish-public.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("\n  push:\n    branches:\n      - master", workflow)
        self.assertNotIn("\n  pull_request:", workflow)
        self.assertIn("kkytmkmmk/personal-os", workflow)
        self.assertIn("PUBLIC_REPO_TOKEN", workflow)
        self.assertIn("master", workflow)
        self.assertIn("GIT_ASKPASS", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertNotIn("x-access-token:${PUBLIC_REPO_TOKEN}", workflow)
        for command in ("check_secrets.py", "check_public_safety.py", "check_tracked_private_files.py", "run_memory_quality_benchmark.py"):
            self.assertIn(command, workflow)

    def _snapshot(self, root: Path, readme: str, extra: dict[str, str] | None = None) -> Path:
        snapshot = root / "snapshot"
        snapshot.mkdir()
        (snapshot / "README.md").write_text(readme, encoding="utf-8")
        for name, content in (extra or {}).items():
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return snapshot

    def test_initial_publish_sync_and_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "public.git"
            git("init", "--bare", str(remote))
            snapshot = self._snapshot(root, "first", {"removed.txt": "old"})

            initial = sync_snapshot(snapshot, str(remote), root / "work")
            self.assertEqual(initial.mode, "PUBLISHED")
            self.assertEqual(git("--git-dir", str(remote), "show", f"{PUBLIC_MIRROR_BRANCH}:README.md").strip(), "first")

            unchanged = sync_snapshot(snapshot, str(remote), root / "work")
            self.assertEqual(unchanged.mode, "NO CHANGES")

            (snapshot / "README.md").write_text("second", encoding="utf-8")
            (snapshot / "removed.txt").unlink()
            (snapshot / "added.txt").write_text("new", encoding="utf-8")
            updated = sync_snapshot(snapshot, str(remote), root / "work")
            self.assertEqual(updated.mode, "PUBLISHED")
            self.assertEqual(git("--git-dir", str(remote), "show", f"{PUBLIC_MIRROR_BRANCH}:README.md").strip(), "second")
            self.assertEqual(git("--git-dir", str(remote), "show", f"{PUBLIC_MIRROR_BRANCH}:added.txt").strip(), "new")
            missing = subprocess.run(
                ["git", "--git-dir", str(remote), "cat-file", "-e", f"{PUBLIC_MIRROR_BRANCH}:removed.txt"], capture_output=True
            )
            self.assertNotEqual(missing.returncode, 0)

    def test_dry_run_does_not_create_a_public_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "public.git"
            git("init", "--bare", str(remote))
            snapshot = self._snapshot(root, "safe")
            result = sync_snapshot(snapshot, str(remote), root / "work", dry_run=True)
            self.assertEqual(result.mode, "DRY RUN")
            self.assertNotEqual(
                subprocess.run(["git", "--git-dir", str(remote), "rev-parse", PUBLIC_MIRROR_BRANCH], capture_output=True).returncode,
                0,
            )

    def test_authentication_error_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._snapshot(root, "safe")
            secret = "very-private-token-value"
            with self.assertRaises(PublicMirrorGitError) as caught:
                sync_snapshot(snapshot, f"https://x-access-token:{secret}@example.invalid/public.git", root / "work")
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn("x-access-token:", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseSafetyTests(unittest.TestCase):
    def test_local_operational_scripts_are_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("scripts/provision-fn.ps1", ignored)
        self.assertIn("scripts/verify_gbrain_lifecycle.py", ignored)

    def test_source_checkout_can_run_admin_tools_without_installed_entrypoints(self) -> None:
        for name, command in (
            ("start-admin-console.ps1", "agent_memory_gateway.admin_console"),
            ("check-admin-health.ps1", "agent_memory_gateway.admin_check"),
        ):
            script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("PYTHONPATH", script)
            self.assertIn(command, script)
            self.assertIn("MEMORY_GATEWAY_TOKEN", script)

    def test_ci_runs_release_gates_without_parent_commit_assumption(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", workflow)
        self.assertIn("python -m compileall -q src tests", workflow)
        self.assertIn("公开文件是否包含敏感信息", workflow)
        self.assertIn("git diff-tree --check -r HEAD", workflow)
        self.assertIn("$_ -ne 'tests/fixtures/security_cases.json'", workflow)
        self.assertNotIn("HEAD^ HEAD", workflow)

    def test_security_fixture_is_explicitly_nonworking_test_data(self) -> None:
        fixture = (ROOT / "tests" / "fixtures" / "security_cases.json").read_text(encoding="utf-8")
        self.assertIn("non-working-test-material", fixture)
        self.assertIn("non-working-password", fixture)
        self.assertIn("sk-" + "aaaaaaaaaaaaaaaaaaaaaaaa", fixture)
        self.assertIn("ghp_" + "aaaaaaaaaaaaaaaaaaaaaaaa", fixture)


if __name__ == "__main__":
    unittest.main()

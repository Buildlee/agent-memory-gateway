from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def project_markdown_files() -> list[Path]:
    files = [ROOT / name for name in ("README.md", "README_EN.md", "DESIGN.md", "PRODUCT.md")]
    files.extend((ROOT / "docs").rglob("*.md"))
    files.extend((ROOT / "examples").rglob("*.md"))
    return sorted(path for path in files if path.is_file())


class DocumentationContractTests(unittest.TestCase):
    def test_all_local_markdown_links_resolve(self) -> None:
        broken: list[str] = []
        for document in project_markdown_files():
            text = document.read_text(encoding="utf-8")
            for raw_target in MARKDOWN_LINK.findall(text):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                relative = unquote(target.split("#", 1)[0].split("?", 1)[0])
                resolved = (document.parent / relative).resolve()
                if not resolved.exists():
                    broken.append(
                        f"{document.relative_to(ROOT).as_posix()} -> {target}"
                    )
        self.assertEqual(broken, [], "失效的本地 Markdown 链接：\n" + "\n".join(broken))

    def test_readmes_match_current_cli_and_mcp_names(self) -> None:
        for name in ("README.md", "README_EN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("memory-device install --profile", text)
                self.assertIn("memory_remember", text)
                self.assertNotIn("memory_write", text)
                self.assertNotIn("memory-sidecar-daemon --gateway-url", text)

    def test_cross_platform_installation_is_documented_in_both_languages(self) -> None:
        for relative in (
            "README.md",
            "README_EN.md",
            "docs/deployment.md",
            "docs/en/deployment.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(document=relative):
                self.assertIn("Linux", text)
                self.assertIn("macOS", text)
                self.assertIn("memory-device install", text)

    def test_openclaw_mcp_example_matches_the_runtime_shape(self) -> None:
        value = __import__("json").loads(
            (ROOT / "examples" / "openclaw-mcp.json").read_text(encoding="utf-8")
        )

        self.assertIn("shared-memory", value["mcp"]["servers"])
        self.assertNotIn("mcp_servers", value)

    def test_import_docs_cover_scan_apply_resume_and_rollback(self) -> None:
        for relative in (
            "docs/importing-existing-memory.md",
            "docs/en/importing-existing-memory.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(document=relative):
                for contract in (
                    "memory-import scan",
                    "memory-import apply",
                    "--resume",
                    "memory-import rollback",
                    "--confirmed-by-user",
                ):
                    self.assertIn(contract, text)


if __name__ == "__main__":
    unittest.main()

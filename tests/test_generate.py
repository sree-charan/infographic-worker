"""Offline tests: no NotebookLM, no network. Run with `python -m unittest`."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate  # noqa: E402


class SlugTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(generate.slugify("Exams start Monday!"), "exams-start-monday")

    def test_empty_falls_back(self):
        self.assertEqual(generate.slugify("   "), "infographic")

    def test_truncates(self):
        self.assertLessEqual(len(generate.slugify("a" * 200)), 40)


class UrlTests(unittest.TestCase):
    def test_compose(self):
        url = generate.compose_raw_url("me/repo", "main", "generated", "x.png")
        self.assertEqual(
            url, "https://raw.githubusercontent.com/me/repo/main/generated/x.png"
        )

    def test_compose_strips_slashes(self):
        url = generate.compose_raw_url("me/repo", "main", "/generated/", "x.png")
        self.assertEqual(
            url, "https://raw.githubusercontent.com/me/repo/main/generated/x.png"
        )


class NotebookIdTests(unittest.TestCase):
    def test_active_id(self):
        self.assertEqual(
            generate._extract_notebook_id('{"active_notebook_id": "abc"}'), "abc"
        )

    def test_nested_notebook(self):
        self.assertEqual(
            generate._extract_notebook_id('{"notebook": {"id": "xyz"}}'), "xyz"
        )

    def test_missing_raises(self):
        with self.assertRaises(ValueError):
            generate._extract_notebook_id('{"nope": true}')


class DryRunPipelineTests(unittest.TestCase):
    def test_dry_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rc = generate.main([
                "--dry-run",
                "--text", "Test announcement",
                "--title", "Test Notice",
                "--out", str(d / "work.png"),
                "--host-dir", str(d / "generated"),
                "--manifest", str(d / "generated" / "latest.json"),
                "--repo", "me/repo",
                "--ref", "main",
            ])
            self.assertEqual(rc, 0)

            manifest = json.loads((d / "generated" / "latest.json").read_text())
            self.assertTrue(manifest["url"].startswith(
                "https://raw.githubusercontent.com/me/repo/main/"))
            self.assertTrue(manifest["filename"].endswith(".png"))
            self.assertEqual(manifest["orientation"], "portrait")

            staged = Path(manifest["file"])
            self.assertTrue(staged.exists())
            # PNG signature
            self.assertEqual(staged.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()

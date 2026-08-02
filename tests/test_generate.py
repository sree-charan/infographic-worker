"""Offline tests: no NotebookLM, no network. Run with `python -m unittest`."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate  # noqa: E402
from PIL import Image  # noqa: E402


class SlugTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(generate.slugify("Exams start Monday!"), "exams-start-monday")

    def test_empty_falls_back(self):
        self.assertEqual(generate.slugify("   "), "infographic")

    def test_truncates(self):
        self.assertLessEqual(len(generate.slugify("a" * 200)), 40)


class UrlTests(unittest.TestCase):
    def test_compose(self):
        self.assertEqual(
            generate.compose_raw_url("me/repo", "main", "generated", "x.png"),
            "https://raw.githubusercontent.com/me/repo/main/generated/x.png")

    def test_compose_strips_slashes(self):
        self.assertEqual(
            generate.compose_raw_url("me/repo", "main", "/generated/", "x.png"),
            "https://raw.githubusercontent.com/me/repo/main/generated/x.png")


class DescriptionTests(unittest.TestCase):
    def test_directive_present(self):
        d = generate.compose_description("Focus on the dates.")
        self.assertIn("plain, natural, human English", d)
        self.assertIn("delve", d)                 # banned-word list is there
        self.assertIn("Focus on the dates.", d)   # user steer appended

    def test_directive_without_user_text(self):
        d = generate.compose_description("")
        self.assertIn("No hype", d)


class NotebookIdTests(unittest.TestCase):
    def test_active_id(self):
        self.assertEqual(generate._extract_notebook_id('{"active_notebook_id":"abc"}'), "abc")

    def test_nested_notebook(self):
        self.assertEqual(generate._extract_notebook_id('{"notebook":{"id":"xyz"}}'), "xyz")

    def test_missing_raises(self):
        with self.assertRaises(ValueError):
            generate._extract_notebook_id('{"nope": true}')


class SourceIdTests(unittest.TestCase):
    def test_list_shape(self):
        self.assertEqual(
            generate._extract_source_ids('[{"id":"s1"},{"source_id":"s2"}]'), ["s1", "s2"])

    def test_wrapped_shape(self):
        self.assertEqual(
            generate._extract_source_ids('{"sources":[{"sourceId":"s3"}]}'), ["s3"])

    def test_empty(self):
        self.assertEqual(generate._extract_source_ids('[]'), [])


class ProcessImageTests(unittest.TestCase):
    def _make(self, d, name="src.png", size=(600, 1000)):
        p = Path(d) / name
        Image.new("RGB", size, (250, 243, 232)).save(p)
        return p

    def test_crop_then_overlay_logo_no_band(self):
        with tempfile.TemporaryDirectory() as d:
            png = self._make(d)
            logo = Path(d) / "logo.png"
            Image.new("RGBA", (1200, 240), (0, 0, 0, 255)).save(logo)

            generate.process_image(png, crop_frac=0.045, crop_px=None,
                                   logo_path=logo, logo_width_frac=0.26,
                                   logo_margin_frac=0.02)
            out = Image.open(png)
            # overlay only: height is the cropped height, NOT grown by a band
            self.assertEqual(out.size, (600, 1000 - round(1000 * 0.045)))
            self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_crop_px_override_and_no_logo(self):
        with tempfile.TemporaryDirectory() as d:
            png = self._make(d)
            generate.process_image(png, crop_frac=0.5, crop_px=120,
                                   logo_path=None, logo_width_frac=0.26,
                                   logo_margin_frac=0.03)
            self.assertEqual(Image.open(png).size, (600, 880))


class DryRunPipelineTests(unittest.TestCase):
    def test_dry_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rc = generate.main([
                "--dry-run", "--text", "Test announcement", "--title", "Test Notice",
                "--out", str(d / "work.png"),
                "--host-dir", str(d / "generated"),
                "--manifest", str(d / "generated" / "latest.json"),
                "--no-logo",
                "--repo", "me/repo", "--ref", "main",
            ])
            self.assertEqual(rc, 0)
            manifest = json.loads((d / "generated" / "latest.json").read_text())
            self.assertTrue(manifest["url"].startswith(
                "https://raw.githubusercontent.com/me/repo/main/"))
            self.assertEqual(manifest["orientation"], "portrait")
            self.assertEqual(manifest["notebook_id"], "dry-run")
            staged = Path(manifest["file"])
            self.assertEqual(staged.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()

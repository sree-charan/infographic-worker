#!/usr/bin/env python3
"""
Infographic worker.

Turns a piece of text (an announcement / topic) into a portrait infographic PNG
using the unofficial `notebooklm` CLI, then:
  - post-processes the image (crops the NotebookLM footer, pastes a portal logo),
  - stages it for hosting and writes a JSON manifest with the public URL.

Auth is NOT handled here. The CLI reads the session from the environment
(NOTEBOOKLM_AUTH_JSON) or the seeded on-disk profile. The GitHub Actions workflow
injects the secret; locally you run `notebooklm login` once.

Usage:
    python generate.py --text "Exams start Monday..." --title "Exam notice"
    python generate.py --text-file notice.txt --notebook-id abc123      # reuse a notebook
    python generate.py --dry-run --text "x" --repo owner/repo --ref main # no network
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageStat

CLI = os.environ.get("NOTEBOOKLM_BIN", "notebooklm")

# Style directive prepended to every generation to keep the text human and
# strip the usual LLM filler. NotebookLM has final say, so this reduces slop
# strongly but cannot guarantee it 100%.
VOICE_DIRECTIVE = (
    "Voice: write like a real person addressing college students — plain, warm, "
    "and direct. Use short declarative sentences in active voice. Lead with the "
    "concrete facts taken straight from the source (dates, times, venues, names, "
    "numbers); never invent, infer, or pad with generic filler. Headings must be "
    "specific and scannable, not clever wordplay or puns. "
    "Ban this AI/marketing filler outright: delve, leverage, robust, seamless, "
    "unlock, empower, elevate, foster, harness, game-changer, cutting-edge, "
    "testament to, dive in, embark, journey, revolutionize, unleash, supercharge, "
    "tapestry, realm, in today's fast-paced world, when it comes to, "
    "it's important to note, navigate the landscape. "
    "No hype, no cliches, no rhetorical questions, no exclamation spam, no emoji, "
    "and no empty adjectives like 'amazing' or 'exciting'. "
    "Gut check: if a line would look right on a printed campus notice, keep it; "
    "if it reads like an ad or a blog intro, rewrite it."
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:max_len].strip("-")) or "infographic"


def compose_description(user_instructions: str | None) -> str:
    """Combine the voice directive with any caller-supplied steer."""
    parts = [VOICE_DIRECTIVE]
    if user_instructions and user_instructions.strip():
        parts.append(user_instructions.strip())
    return "\n\n".join(parts)


def compose_raw_url(repo: str, ref: str, host_dir: str, filename: str) -> str:
    """Public raw.githubusercontent.com URL (only resolvable when repo is PUBLIC)."""
    host_dir = host_dir.strip("/")
    parts = [p for p in (host_dir, filename) if p]
    return f"https://raw.githubusercontent.com/{repo}/{ref}/" + "/".join(parts)


def run_cli(args: list[str], *, timeout: int | None = None) -> str:
    cmd = [CLI, *args]
    print(f"$ {' '.join(cmd[:3])} ...", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.stdout.strip():
        print(proc.stdout, file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd[:3])} ...` failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _extract_notebook_id(cli_json_stdout: str) -> str:
    data = json.loads(cli_json_stdout)
    for key in ("active_notebook_id", "notebook_id", "id"):
        if isinstance(data.get(key), str) and data[key]:
            return data[key]
    nb = data.get("notebook")
    if isinstance(nb, dict) and isinstance(nb.get("id"), str):
        return nb["id"]
    raise ValueError(f"Could not find a notebook id in: {cli_json_stdout!r}")


def _extract_source_ids(cli_json_stdout: str) -> list[str]:
    """Pull source ids out of `source list --json`, tolerant of shape drift."""
    data = json.loads(cli_json_stdout)
    items = data if isinstance(data, list) else (
        data.get("sources") or data.get("items") or [])
    ids: list[str] = []
    for it in items:
        if isinstance(it, dict):
            sid = it.get("id") or it.get("source_id") or it.get("sourceId")
            if sid:
                ids.append(sid)
        elif isinstance(it, str):
            ids.append(it)
    return ids


# --------------------------------------------------------------------------- #
# notebooklm steps
# --------------------------------------------------------------------------- #
def ensure_notebook(title: str, notebook_id: str | None) -> tuple[str, bool]:
    """Return (notebook_id, pinned). pinned=True means we reuse an existing one."""
    if notebook_id:
        return notebook_id, True
    out = run_cli(["create", title, "--json"], timeout=120)
    return _extract_notebook_id(out), False


def reset_notebook_sources(notebook_id: str) -> None:
    """Delete every existing source so the notebook holds only the new announcement.

    Reused notebooks otherwise accumulate sources, and NotebookLM would build the
    infographic from ALL of them mixed together.
    """
    try:
        out = run_cli(["source", "list", "--json", "-n", notebook_id], timeout=60)
        ids = _extract_source_ids(out)
    except Exception as e:  # noqa: BLE001 - best effort
        print(f"WARN: could not list sources to reset: {e}", file=sys.stderr)
        return
    for sid in ids:
        try:
            run_cli(["source", "delete", sid, "-y", "-n", notebook_id], timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete source {sid}: {e}", file=sys.stderr)


def add_text_source(notebook_id: str, text: str) -> None:
    run_cli(["source", "add", text, "--type", "text", "-n", notebook_id], timeout=180)


def generate_infographic(notebook_id: str, *, description: str, orientation: str,
                         detail: str, style: str | None, timeout: int) -> None:
    args = ["generate", "infographic", description, "-n", notebook_id,
            "--orientation", orientation, "--detail", detail,
            "--wait", "--timeout", str(timeout), "--retry", "2", "--json"]
    if style:
        args += ["--style", style]
    run_cli(args, timeout=timeout + 120)


def download_infographic(notebook_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_cli(["download", "infographic", str(out_path), "-n", notebook_id, "--latest"],
            timeout=180)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"Download produced no file at {out_path}")


def write_placeholder(out_path: Path, size: tuple[int, int] = (600, 1000)) -> None:
    """A cream portrait image, used by --dry-run so post-processing is testable."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (250, 243, 232)).save(out_path)


# --------------------------------------------------------------------------- #
# post-processing: crop NotebookLM footer + paste portal logo
# --------------------------------------------------------------------------- #
def _dominant_color(im: Image.Image) -> tuple[int, int, int, int]:
    """Most-used colour in the image (the background, for an infographic)."""
    rgb = im.convert("RGB")
    w, h = rgb.size
    small = rgb.resize((128, max(1, round(128 * h / w))))
    colors = small.getcolors(maxcolors=small.width * small.height) or [(1, (255, 255, 255))]
    r, g, b = max(colors, key=lambda c: c[0])[1]
    return (r, g, b, 255)


def _bottom_empty_height(im: Image.Image, bg: tuple[int, int, int, int], tol: int = 24) -> int:
    """How many px of near-uniform background the image already has at the bottom.

    Scans rows upward from the bottom; a row counts as empty until >2% of its
    pixels differ from the background colour. Returned in original-image px.
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    sw = 120
    sh = min(h, 600)
    small = rgb.resize((sw, sh))
    px = small.load()
    br, bgc, bb = bg[0], bg[1], bg[2]
    limit = max(1, int(sw * 0.02))
    empty = 0
    for y in range(sh - 1, -1, -1):
        diff = 0
        for x in range(sw):
            r, g, b = px[x, y]
            if abs(r - br) + abs(g - bgc) + abs(b - bb) > tol:
                diff += 1
                if diff > limit:
                    break
        if diff > limit:
            break
        empty += 1
    return round(empty * h / sh)


def _white_logo(logo: Image.Image) -> Image.Image:
    """The logo in solid white, alpha preserved, for dark backgrounds."""
    white = Image.new("RGBA", logo.size, (255, 255, 255, 0))
    white.putalpha(logo.getchannel("A"))
    return white


def _busyness(im: Image.Image, box: tuple[int, int, int, int]) -> float:
    """How much detail is in a region: mean edge energy, 0 for flat colour.

    Used to choose where the logo can sit without landing on a face or a caption.
    """
    crop = im.convert("L").crop(box)
    if crop.width < 2 or crop.height < 2:
        return 1e9
    edges = crop.filter(ImageFilter.FIND_EDGES)
    return ImageStat.Stat(edges).mean[0]


def _cover_wordmark(im: Image.Image, *, w_frac: float = 0.20,
                    h_frac: float = 0.020,
                    feather: int = 10) -> tuple[int, int, int, int] | None:
    """Hide the generator's wordmark by cloning the pixels directly above it.

    Measured on an unprocessed output: the mark is 192x20px on a 1536x2752 canvas,
    sitting 11px from the bottom and 11px from the right. So a small corner
    rectangle covers it - the previous approach cropped 3% of the height off the
    whole width, 82px where 31 were needed, which sliced through body text and cut
    illustrations flat.

    Deliberately not matched by text or template. The mark read "NotebookLM" and now
    reads "Gemini Notebook", so anything keyed to the wording breaks at the next
    rename, and there is no reference image for the new one. Its geometry has not
    moved, and a clone of the region above is invisible on flat background and
    plausible on artwork.
    """
    w, h = im.size
    bw = max(1, round(w * w_frac))
    bh = max(1, round(h * h_frac))
    # Flush to the right and bottom edges. An inset left the mark's last letters
    # sitting in the feather zone, where the patch is only part opaque, so "LM"
    # showed through at 682px of surviving ink.
    x1, y1 = w, h
    x0, y0 = max(0, x1 - bw), max(0, y1 - bh)

    # Flat fill in the local background colour. Two cleverer fills failed here in
    # the same way, each assuming the pixels near the mark are background:
    # cloning the strip above duplicated a card border 55px lower, and averaging
    # that strip per column turned the body text into a faint barcode of vertical
    # stripes. The mode of a ring around the rectangle is the one estimate that
    # ignores whatever content is passing through it.
    ring_h = max(bh * 3, 90)
    ring = im.crop((max(0, x0 - bw // 2), max(0, y0 - ring_h), x1, y0)).convert("RGB")
    small = ring.resize((min(ring.width, 160), min(ring.height, 90)), Image.BOX)
    quant = small.quantize(colors=8, method=Image.Quantize.FASTOCTREE)
    counts = sorted(quant.getcolors() or [], reverse=True)
    if counts:
        idx = counts[0][1]
        pal = quant.getpalette()[idx * 3:idx * 3 + 3]
        fill = (pal[0], pal[1], pal[2], 255)
    else:
        fill = _dominant_color(im)
    patch = Image.new("RGBA", (x1 - x0, y1 - y0), fill)

    # Feather only the two interior edges - top and left - where the patch meets
    # the picture. The other two are the image border, and must stay hard so the
    # mark is fully covered.
    mask = Image.new("L", patch.size, 255)
    px = mask.load()
    for i in range(min(feather, patch.width)):
        a = int(255 * (i + 1) / (feather + 1))
        for yy in range(patch.height):
            px[i, yy] = min(px[i, yy], a)
    for j in range(min(feather, patch.height)):
        a = int(255 * (j + 1) / (feather + 1))
        for xx in range(patch.width):
            px[xx, j] = min(px[xx, j], a)
    im.paste(patch, (x0, y0), mask)
    return (x0, y0, x1, y1)


def process_image(png_path: Path, *, crop_frac: float, crop_px: int | None,
                  logo_path: Path | None, logo_width_frac: float,
                  logo_margin_frac: float, grow: bool = False) -> None:
    """Remove the generator's wordmark and place the portal logo.

    The canvas is left exactly as it is. The previous version appended a band and
    filled it by stretching the image's own bottom row downward, which turns
    whatever is on that row into vertical streaks - on a full-bleed illustration
    the figures and the flag smeared into 73px of stripes. That branch only ran
    when content reached the bottom edge, which is precisely when the stretched row
    is guaranteed to contain content, so it was self-defeating.

    Instead the logo is overlaid on the quietest horizontal band in the bottom
    third, in white or in brand colour depending on how dark that band is. Pass
    grow=True for the old behaviour of appending space, now filled with a flat
    colour rather than a stretched row.
    """
    im = Image.open(png_path).convert("RGBA")

    # An explicit crop is still honoured, for a one-off, but nothing is cropped by
    # default: the wordmark is covered in place instead.
    crop = crop_px if crop_px is not None else round(im.height * crop_frac)
    crop = max(0, min(crop, im.height - 1))
    if crop:
        im = im.crop((0, 0, im.width, im.height - crop))
    else:
        _cover_wordmark(im)

    w, h = im.size
    if not (logo_path and Path(logo_path).exists()):
        im.convert("RGB").save(png_path)
        return

    logo = Image.open(logo_path).convert("RGBA")
    target_w = max(1, round(w * logo_width_frac))
    target_h = max(1, round(target_w * logo.height / logo.width))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    margin = round(h * logo_margin_frac)
    x = (w - target_w) // 2
    need = target_h + 2 * margin

    bg = _dominant_color(im)
    empty = _bottom_empty_height(im, bg)

    # One margin, not two. Requiring space for a margin above AND below meant a
    # poster with 130px of clear space at the foot failed the test and fell through
    # to the band search, which floated the logo 376px up into the middle of the
    # design.
    if empty >= target_h + margin:
        y = h - margin - target_h
    elif grow:
        add = need - empty
        band = Image.new("RGBA", (w, add), bg)
        canvas = Image.new("RGBA", (w, h + add), bg)
        canvas.alpha_composite(im, (0, 0))
        canvas.alpha_composite(band, (0, h))
        im = canvas
        h += add
        y = h - margin - target_h
    else:
        # Content runs to the edge. Sit in the quietest band near the bottom rather
        # than damaging the composition to make room. Biased downward: searching the
        # whole bottom third put the logo 280px up on a full-bleed illustration,
        # where it floated mid-picture instead of reading as a footer.
        best_y, best_score = h - margin - target_h, None
        lo = max(0, h - max(need, round(h * 0.18)))
        hi = h - target_h - margin // 2
        step = max(4, target_h // 4)
        for cand in range(lo, hi + 1, step):
            busy = _busyness(im, (x, cand, x + target_w, cand + target_h))
            # Distance penalty in the same units as edge energy, calibrated so a
            # quieter band is only worth taking if it is not far from the bottom.
            penalty = 25.0 * (hi - cand) / max(h, 1)
            score = busy + penalty
            if best_score is None or score < best_score:
                best_y, best_score = cand, score
        y = best_y

    # White reads on a dark or saturated band; the brand colours read on a light
    # one. Same choice the video pipeline makes, for the same reason.
    region = im.convert("RGB").crop((x, y, x + target_w, y + target_h))
    if ImageStat.Stat(region.convert("L")).mean[0] < 140:
        logo = _white_logo(logo)

    im.alpha_composite(logo, (x, y))
    im.convert("RGB").save(png_path)


# --------------------------------------------------------------------------- #
# hosting / manifest
# --------------------------------------------------------------------------- #
def stage_and_manifest(png_path: Path, *, title: str, orientation: str, detail: str,
                       notebook_id: str, repo: str | None, ref: str | None,
                       host_dir: str, manifest_path: Path) -> dict:
    host = Path(host_dir)
    host.mkdir(parents=True, exist_ok=True)
    filename = f"{_now_stamp()}-{slugify(title)}.png"
    final = host / filename
    final.write_bytes(png_path.read_bytes())

    url = compose_raw_url(repo, ref, host_dir, filename) if repo and ref else None
    manifest = {
        "url": url,
        "file": str(final).replace("\\", "/"),
        "filename": filename,
        "title": title,
        "orientation": orientation,
        "detail": detail,
        "notebook_id": notebook_id,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate a portrait infographic from text.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--text-file")

    p.add_argument("--title", default="Announcement")
    p.add_argument("--orientation", default="portrait",
                   choices=["portrait", "landscape", "square"])
    p.add_argument("--detail", default="detailed",
                   choices=["concise", "standard", "detailed"])
    p.add_argument("--style", default=os.environ.get("INFOGRAPHIC_STYLE", ""))
    p.add_argument("--instructions", default="",
                   help="Extra steer, appended to the built-in human-voice directive.")
    p.add_argument("--notebook-id", default=os.environ.get("NOTEBOOKLM_NOTEBOOK", ""),
                   help="Reuse an existing notebook (its sources are wiped each run).")
    p.add_argument("--timeout", type=int, default=900)

    # post-processing
    p.add_argument("--crop-frac", type=float, default=0.0,
                   help="Fraction of height to trim off the bottom. Default 0: the "
                        "generator wordmark is covered in place instead, because a "
                        "full-width crop deep enough to reach it also cut body text "
                        "and sliced illustrations flat.")
    p.add_argument("--crop-px", type=int, default=None,
                   help="Absolute bottom crop in px (overrides --crop-frac).")
    p.add_argument("--logo", default="assets/logo.png", help="Logo PNG to paste.")
    p.add_argument("--no-logo", action="store_true")
    p.add_argument("--logo-width-frac", type=float, default=0.26)
    p.add_argument("--logo-margin-frac", type=float, default=0.02)
    p.add_argument("--grow", action="store_true",
                   help="Append space for the logo when content reaches the bottom "
                        "edge, instead of overlaying it on the quietest band. The "
                        "appended band is a flat colour; it used to be the image's "
                        "own bottom row stretched downward, which smeared whatever "
                        "was on that row into vertical streaks.")

    # hosting
    p.add_argument("--out", default="")
    p.add_argument("--host-dir", default="generated")
    p.add_argument("--manifest", default="generated/latest.json")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--ref", default=os.environ.get("GITHUB_REF_NAME", ""))

    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def resolve_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    return Path(args.text_file).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    text = resolve_text(args)
    if not text.strip():
        print("ERROR: text is empty.", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else Path("out") / f"{_now_stamp()}.png"
    notebook_id = args.notebook_id or ""

    if args.dry_run:
        print("[dry-run] skipping NotebookLM; writing placeholder PNG.", file=sys.stderr)
        write_placeholder(out_path)
        notebook_id = notebook_id or "dry-run"
    else:
        notebook_id, pinned = ensure_notebook(args.title, args.notebook_id or None)
        print(f"notebook: {notebook_id} (pinned={pinned})", file=sys.stderr)
        if pinned:
            reset_notebook_sources(notebook_id)
        add_text_source(notebook_id, text)
        time.sleep(3)  # let the text source register
        generate_infographic(
            notebook_id,
            description=compose_description(args.instructions),
            orientation=args.orientation,
            detail=args.detail,
            style=args.style or None,
            timeout=args.timeout,
        )
        download_infographic(notebook_id, out_path)

    process_image(
        out_path,
        crop_frac=args.crop_frac,
        crop_px=args.crop_px,
        logo_path=None if args.no_logo else Path(args.logo),
        logo_width_frac=args.logo_width_frac,
        logo_margin_frac=args.logo_margin_frac,
        grow=args.grow,
    )

    manifest = stage_and_manifest(
        out_path,
        title=args.title,
        orientation=args.orientation,
        detail=args.detail,
        notebook_id=notebook_id,
        repo=args.repo or None,
        ref=args.ref or None,
        host_dir=args.host_dir,
        manifest_path=Path(args.manifest),
    )

    print(json.dumps(manifest))
    print(f"NOTEBOOK_ID={notebook_id}", file=sys.stderr)
    if manifest.get("url"):
        print(f"IMAGE_URL={manifest['url']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

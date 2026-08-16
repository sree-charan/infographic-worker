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

from PIL import Image

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


# For a poster, VOICE_DIRECTIVE is actively harmful: it demands facts, dates,
# venues and scannable headings, so asking for a text-free image while sending it
# produces a notice with a headline. This replaces it rather than arguing with it.
POSTER_DIRECTIVE = (
    "This is a decorative POSTER, not an infographic and not a notice. It carries "
    "no information: no facts, no dates, no times, no venues, no names, no numbers, "
    "no headings, no subheadings, no body copy, no bullet points, no labels, no "
    "captions, no credits and no watermarks.\n\n"
    "Render exactly one piece of text, exactly once: \"{line}\". Nothing else "
    "written anywhere in the image - no second copy of it, no translation, no "
    "decorative lettering that spells other words.\n\n"
    "Everything else is illustration. Fill the composition with imagery and colour "
    "rather than words, and let the single line of text be the focal point.\n\n"
    "Set the text in a display weight large enough to read at a glance, and keep it "
    "clear of the illustration so neither fights the other."
)


def compose_description(user_instructions: str | None, *, poster: bool = False,
                        line: str = "") -> str:
    """Combine the governing directive with any caller-supplied steer."""
    parts = [POSTER_DIRECTIVE.format(line=line) if poster else VOICE_DIRECTIVE]
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
    """Most-used colour in the image - the page background, for an infographic.

    Downsampled with NEAREST, so every counted colour is one that actually appears.
    Smooth resampling invents averages along every edge, and the winner can be a
    colour present nowhere in the picture.
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    small = rgb.resize((160, max(1, round(160 * h / w))), Image.NEAREST)
    colors = small.getcolors(maxcolors=small.width * small.height) or [(1, (255, 255, 255))]
    r, g, b = max(colors, key=lambda c: c[0])[1]
    return (r, g, b, 255)


def _bottom_empty_height(im: Image.Image, bg: tuple[int, int, int, int], tol: int = 24) -> int:
    """How many px of near-uniform background the image already has at the bottom.

    Scans rows upward from the bottom; a row counts as empty until >2% of its
    pixels differ from the reference colour. Returned in original-image px.

    The reference is taken from the bottom rows themselves, not from `bg`. The
    page's dominant colour is often not the footer's: a white card on a cream
    background reports white, no footer row matches it, and the answer comes back 0
    - so a poster with a 342px empty foot looked full to the caller. `bg` is only
    the fallback for when the bottom strip cannot be sampled.

    Call this AFTER the generator's wordmark has been covered. The mark sits in
    that footer, and a scan for uniform rows stops dead at it: measured 9px on a
    poster whose real whitespace was 206px.
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    sw = 120
    sh = min(h, 600)
    small = rgb.resize((sw, sh))
    px = small.load()

    foot = small.crop((0, sh - max(2, sh // 50), sw, sh))
    quant = foot.quantize(colors=4, method=Image.Quantize.FASTOCTREE)
    counts = sorted(quant.getcolors() or [], reverse=True)
    if counts:
        idx = counts[0][1]
        pal = quant.getpalette()[idx * 3:idx * 3 + 3]
        br, bgc, bb = pal[0], pal[1], pal[2]
    else:
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


def _edge_color(im: Image.Image, box: tuple[int, int, int, int],
                band: int = 10) -> tuple[int, int, int, int]:
    """Most common exact colour in a thin ring just outside `box`.

    Two things matter here and both were wrong before.

    The sample must be adjacent. Sampling a large block above and to the left took
    in the poster's decorative frame, so the patch came out 16 levels darker than
    the white it sat on - a visible grey rectangle. Only the colour immediately
    around the mark can match it.

    And it must be an exact colour, counted. Quantizing to a palette and taking the
    most common entry returns an averaged colour that may appear nowhere in the
    picture, which is the same mistake as taking a mean.
    """
    from collections import Counter

    x0, y0, x1, y1 = box
    rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    counts: Counter = Counter()
    for y in range(max(0, y0 - band), min(h, y0)):
        for x in range(max(0, x0 - band), min(w, x1)):
            counts[px[x, y]] += 1
    for y in range(max(0, y0 - band), min(h, y1)):
        for x in range(max(0, x0 - band), min(w, x0)):
            counts[px[x, y]] += 1

    if not counts:
        return _dominant_color(im)
    r, g, b = counts.most_common(1)[0][0]
    return (r, g, b, 255)


def _feathered_fill(size: tuple[int, int], colour: tuple[int, int, int, int],
                    ramps: tuple[int, int, int, int] = (8, 8, 3, 3)
                    ) -> tuple[Image.Image, Image.Image]:
    """A flat patch plus a mask that ramps in from each edge by its own amount.

    `ramps` is (left, top, right, bottom). Per side because the two sides facing
    into the picture have room for a long, invisible ramp, while the two facing the
    canvas edge do not: the mark sits only 11px off the corner, so a long ramp there
    would cross the mark, and a ramp across the mark leaves its letters part
    covered. That is how "NOTEBOOKLM" stayed faintly legible under our logo.
    """
    w, h = size
    patch = Image.new("RGBA", (w, h), colour)
    mask = Image.new("L", (w, h), 0)
    px = mask.load()
    rl, rt, rr, rb = (max(r, 0) for r in ramps)
    for y in range(h):
        fy = 1.0
        if rt and y < rt:
            fy = min(fy, (y + 1) / (rt + 1))
        if rb and h - 1 - y < rb:
            fy = min(fy, (h - y) / (rb + 1))
        for x in range(w):
            fx = 1.0
            if rl and x < rl:
                fx = min(fx, (x + 1) / (rl + 1))
            if rr and w - 1 - x < rr:
                fx = min(fx, (w - x) / (rr + 1))
            px[x, y] = int(255 * min(fx, fy))
    return patch, mask


def process_image(png_path: Path, *, crop_frac: float, crop_px: int | None,
                  logo_path: Path | None, logo_width_frac: float,
                  logo_margin_frac: float, grow: bool = False) -> None:
    """Swap the generator's wordmark for the portal logo, in place.

    The wordmark is always in the same spot - bottom right, measured at 192x20px
    sitting 11px off the edges of a 1536x2752 canvas - so this is a local swap and
    nothing else. The canvas is not resized, nothing is cropped, no band is added
    and nothing is drawn over the artwork.

    Everything else was tried and was worse. Cropping deep enough to reach the mark
    took body text and sliced illustrations flat. Appending a band and filling it by
    stretching the bottom row smeared that row into vertical stripes. Trimming the
    generator's own footer changed the format of the design. Placing the logo on the
    "quietest" band put it on top of the picture.

    A patch of the surrounding background goes down first, then the logo on top, in
    white or in its own colours depending on how dark that background is.
    """
    im = Image.open(png_path).convert("RGBA")

    # An explicit crop is still honoured for a one-off, but nothing is cropped by
    # default.
    crop = crop_px if crop_px is not None else round(im.height * crop_frac)
    crop = max(0, min(crop, im.height - 1))
    if crop:
        im = im.crop((0, 0, im.width, im.height - crop))
    w, h = im.size

    if not (logo_path and Path(logo_path).exists()):
        im.convert("RGB").save(png_path)
        return

    logo = Image.open(logo_path).convert("RGBA")
    target_w = max(1, round(w * logo_width_frac))
    target_h = max(1, round(target_w * logo.height / logo.width))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    # Measured on an unprocessed output: the wordmark is 192x20px sitting 11px off
    # the right and bottom edges of a 1536x2752 canvas. The patch has to reach
    # closer to the edge than that, or the mark's last rows fall outside it.
    edge_x = max(3, round(w * 0.002))
    edge_y = max(3, round(h * 0.0015))
    mark_w = max(target_w, round(w * 0.145))
    mark_h = max(target_h, round(h * 0.013))

    cx1, cy1 = w - edge_x, h - edge_y
    cx0, cy0 = max(0, cx1 - mark_w), max(0, cy1 - mark_h)

    # Long ramps on the two sides facing the picture, short ones towards the canvas
    # edge where there is no room. The patch still stops short of the very corner so
    # a border running along it is not cut in half.
    ramp_in, ramp_out = 10, 3
    px0, py0 = max(0, cx0 - ramp_in), max(0, cy0 - ramp_in)
    px1, py1 = min(w, cx1 + ramp_out), min(h, cy1 + ramp_out)

    # The page's most-used colour, which is what the mark sits on. The local ring is
    # only trusted when it agrees: a ring beside the mark can land on a decorative
    # border or a drop shadow, and then the patch comes out 16 levels darker than
    # the white around it, or navy on white.
    page = _dominant_color(im)
    local = _edge_color(im, (px0, py0, px1, py1))
    bg = local if sum(abs(a - b) for a, b in zip(local[:3], page[:3])) <= 40 else page

    patch, mask = _feathered_fill((px1 - px0, py1 - py0), bg,
                                  ramps=(ramp_in, ramp_in, ramp_out, ramp_out))
    im.paste(patch, (px0, py0), mask)

    # White on a dark background, the brand colours on a light one. No black
    # variant: on the backgrounds this generator produces it never wins.
    luma = (bg[0] * 299 + bg[1] * 587 + bg[2] * 114) // 1000
    if luma < 140:
        logo = _white_logo(logo)

    lx = cx0 + (mark_w - target_w) // 2
    ly = cy0 + (mark_h - target_h) // 2
    im.alpha_composite(logo, (max(0, lx), max(0, ly)))
    im.convert("RGB").save(png_path)


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
    p.add_argument("--poster", action="store_true",
                   help="Decorative poster with one line of text and no other "
                        "writing. Replaces the voice directive, which otherwise "
                        "demands facts and headings and gives you a notice.")
    p.add_argument("--instructions", default="",
                   help="Extra steer, appended to the built-in human-voice directive.")
    p.add_argument("--notebook-id", default=os.environ.get("NOTEBOOKLM_NOTEBOOK", ""),
                   help="Reuse an existing notebook (its sources are wiped each run).")
    p.add_argument("--timeout", type=int, default=900)

    # post-processing
    p.add_argument("--crop-frac", type=float, default=0.0,
                   help="Fraction of height to trim off the bottom. Default 0: the "
                        "wordmark is swapped in place, so no crop is needed, and any "
                        "crop deep enough to reach it also cuts body text.")
    p.add_argument("--crop-px", type=int, default=None,
                   help="Absolute bottom crop in px (overrides --crop-frac).")
    p.add_argument("--logo", default="assets/logo.png", help="Logo PNG to paste.")
    p.add_argument("--no-logo", action="store_true")
    p.add_argument("--logo-width-frac", type=float, default=0.13,
                   help="Logo width as a fraction of image width. Matched to the "
                        "wordmark it replaces, which measures 192px on a 1536px "
                        "canvas - an eighth of the width.")
    p.add_argument("--logo-margin-frac", type=float, default=0.0,
                   help="Space above and below the logo as a fraction of image "
                        "height. Default 0 means auto: proportional to the logo "
                        "instead, since a fraction of a 2752px canvas gave the same "
                        "55px margin whatever size the logo was.")
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
            description=compose_description(args.instructions,
                                            poster=args.poster,
                                            line=(args.text or args.title)),
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

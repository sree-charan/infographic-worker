#!/usr/bin/env python3
"""
Infographic worker.

Takes an announcement/topic text, drives the (unofficial) `notebooklm` CLI to
generate a portrait infographic PNG, then stages the file for hosting and writes
a JSON manifest containing the public raw URL.

This module is deliberately generic: it knows nothing about any specific app.
It just turns text -> infographic.png -> {url, metadata}.

Auth is NOT handled here. The CLI reads the session from the environment
(NOTEBOOKLM_AUTH_JSON) or from the seeded profile on disk. The GitHub Actions
workflow injects the secret; locally you run `notebooklm login` once.

Usage:
    python generate.py --text "Exams start Monday..." --title "Exam notice"
    python generate.py --text-file notice.txt --orientation portrait --detail detailed
    python generate.py --dry-run --text "x" --repo owner/repo --ref main   # no network
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# A 1x1 transparent PNG, used only by --dry-run so the hosting/commit path can be
# exercised end to end without calling NotebookLM.
_PLACEHOLDER_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

CLI = os.environ.get("NOTEBOOKLM_BIN", "notebooklm")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def slugify(text: str, max_len: int = 40) -> str:
    """Filesystem/URL-safe slug from arbitrary text."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:max_len].strip("-")) or "infographic"


def compose_raw_url(repo: str, ref: str, host_dir: str, filename: str) -> str:
    """Public raw.githubusercontent.com URL for a committed file.

    Note: only resolvable without auth when the repo is PUBLIC.
    """
    host_dir = host_dir.strip("/")
    parts = [p for p in (host_dir, filename) if p]
    return f"https://raw.githubusercontent.com/{repo}/{ref}/" + "/".join(parts)


def run_cli(args: list[str], *, stdin_text: str | None = None, timeout: int | None = None) -> str:
    """Run the notebooklm CLI, returning stdout. Raises on non-zero exit."""
    cmd = [CLI, *args]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.stdout.strip():
        print(proc.stdout, file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd)}` failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _extract_notebook_id(cli_json_stdout: str) -> str:
    """Pull a notebook id out of `notebooklm create --json` output, defensively."""
    data = json.loads(cli_json_stdout)
    for key in ("active_notebook_id", "notebook_id", "id"):
        if isinstance(data.get(key), str) and data[key]:
            return data[key]
    nb = data.get("notebook")
    if isinstance(nb, dict) and isinstance(nb.get("id"), str):
        return nb["id"]
    raise ValueError(f"Could not find a notebook id in: {cli_json_stdout!r}")


# --------------------------------------------------------------------------- #
# notebooklm steps
# --------------------------------------------------------------------------- #
def ensure_notebook(title: str, notebook_id: str | None) -> str:
    if notebook_id:
        return notebook_id
    out = run_cli(["create", title, "--json"], timeout=120)
    return _extract_notebook_id(out)


def add_text_source(notebook_id: str, text: str) -> None:
    # `source add -` reads the content from stdin; --type text forces a text source.
    run_cli(["source", "add", "-", "--type", "text", "-n", notebook_id],
            stdin_text=text, timeout=180)


def generate_infographic(notebook_id: str, *, orientation: str, detail: str,
                         style: str | None, instructions: str | None,
                         timeout: int) -> None:
    args = ["generate", "infographic"]
    if instructions:
        args.append(instructions)
    args += ["-n", notebook_id, "--orientation", orientation, "--detail", detail,
             "--wait", "--timeout", str(timeout), "--retry", "2", "--json"]
    if style:
        args += ["--style", style]
    run_cli(args, timeout=timeout + 120)


def download_infographic(notebook_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_cli(["download", "infographic", str(out_path), "-n", notebook_id,
             "--latest", "--force"], timeout=180)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"Download produced no file at {out_path}")


def write_placeholder(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_PLACEHOLDER_PNG)


# --------------------------------------------------------------------------- #
# hosting / manifest
# --------------------------------------------------------------------------- #
def stage_and_manifest(png_path: Path, *, title: str, orientation: str, detail: str,
                       repo: str | None, ref: str | None, host_dir: str,
                       manifest_path: Path) -> dict:
    """Move the PNG into host_dir and write a manifest with the public URL."""
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
    src.add_argument("--text", help="Announcement / topic text.")
    src.add_argument("--text-file", help="Path to a file containing the text.")

    p.add_argument("--title", default="Announcement", help="Title (default: Announcement).")
    p.add_argument("--orientation", default="portrait",
                   choices=["portrait", "landscape", "square"])
    p.add_argument("--detail", default="detailed",
                   choices=["concise", "standard", "detailed"])
    p.add_argument("--style", default=os.environ.get("INFOGRAPHIC_STYLE", ""),
                   help="Optional NotebookLM infographic style (e.g. professional, editorial).")
    p.add_argument("--instructions", default="",
                   help="Optional free-text steer for the generation.")
    p.add_argument("--notebook-id", default=os.environ.get("NOTEBOOKLM_NOTEBOOK", ""),
                   help="Reuse an existing notebook instead of creating one.")
    p.add_argument("--timeout", type=int, default=900,
                   help="Max seconds to wait for generation (default 900).")

    p.add_argument("--out", default="", help="Working PNG path (default under out/).")
    p.add_argument("--host-dir", default="generated", help="Dir to stage the committed PNG.")
    p.add_argument("--manifest", default="generated/latest.json",
                   help="Where to write the JSON manifest.")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                   help="owner/repo, for composing the raw URL.")
    p.add_argument("--ref", default=os.environ.get("GITHUB_REF_NAME", ""),
                   help="Branch/ref, for composing the raw URL.")

    p.add_argument("--dry-run", action="store_true",
                   help="Skip NotebookLM entirely; emit a placeholder PNG (for pipeline tests).")
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

    if args.dry_run:
        print("[dry-run] skipping NotebookLM; writing placeholder PNG.", file=sys.stderr)
        write_placeholder(out_path)
    else:
        nb_id = ensure_notebook(args.title, args.notebook_id or None)
        print(f"notebook: {nb_id}", file=sys.stderr)
        add_text_source(nb_id, text)
        # give the text source a moment to register before generation
        time.sleep(3)
        generate_infographic(
            nb_id,
            orientation=args.orientation,
            detail=args.detail,
            style=args.style or None,
            instructions=args.instructions or None,
            timeout=args.timeout,
        )
        download_infographic(nb_id, out_path)

    manifest = stage_and_manifest(
        out_path,
        title=args.title,
        orientation=args.orientation,
        detail=args.detail,
        repo=args.repo or None,
        ref=args.ref or None,
        host_dir=args.host_dir,
        manifest_path=Path(args.manifest),
    )

    # Machine-readable line on stdout; human logs go to stderr.
    print(json.dumps(manifest))
    if manifest.get("url"):
        print(f"IMAGE_URL={manifest['url']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

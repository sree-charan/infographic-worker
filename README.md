# Infographic Worker

Turn a piece of text (an announcement, a topic) into a **portrait infographic
PNG** using the unofficial [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py)
client, host the image, and return a public URL — all from a GitHub Actions
workflow you trigger on demand.

```
text  ->  notebooklm generate infographic  ->  PNG  ->  committed to /generated  ->  raw URL
```

> ⚠️ `notebooklm-py` uses **undocumented Google endpoints**. It works until
> Google changes something. Treat this as a best-effort tool, not production
> infrastructure.

---

## How it works

1. You trigger the **Generate Infographic** workflow (Actions tab) with your text.
2. The runner installs `notebooklm-py` and runs `generate.py`, which:
   - creates a notebook (or reuses one),
   - adds your text as a source,
   - `generate infographic --orientation portrait --detail detailed`,
   - downloads the PNG.
3. The PNG is committed to `generated/` and a `generated/latest.json` manifest is
   written with the public `url`.
4. The URL is shown in the run's **Summary** and the PNG is also uploaded as a
   build artifact.

The final URL looks like:
`https://raw.githubusercontent.com/<owner>/<repo>/<branch>/generated/<file>.png`

---

## One-time setup

### 1. Auth (the important part)

A CI runner can't do an interactive Google login, so you **seed the session once
locally** and store it as an encrypted GitHub secret.

On your own machine:

```bash
pip install "notebooklm-py[browser]"
notebooklm login              # complete the Google sign-in in the browser
notebooklm auth check --test  # expect status ok
```

Then copy the seeded session JSON:

```bash
cat ~/.notebooklm/profiles/default/storage_state.json
```

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**
- Name: `NOTEBOOKLM_AUTH_JSON`
- Value: the full contents of that file.

The workflow passes it via the `NOTEBOOKLM_AUTH_JSON` env var, which the CLI
reads directly — no browser needed on the runner.

> Sessions expire. When runs start failing auth, re-run `notebooklm login`
> locally and update the secret. For longer-lived unattended auth, see
> `notebooklm login --master-token` in the upstream docs (durable, but a
> **full-account** credential — use a dedicated throwaway Google account and
> share only the notebook you need to it).

### 2. Optional: pin a notebook

By default a fresh notebook is created per run. To reuse one, add a repo
**variable** (not secret) `NOTEBOOKLM_NOTEBOOK` with the notebook id.

---

## Trigger it

Actions tab → **Generate Infographic** → **Run workflow**, fill in:

| Input          | Meaning                                   |
|----------------|-------------------------------------------|
| `text`         | The announcement / topic (required)       |
| `title`        | Used for the filename + notebook name     |
| `orientation`  | `portrait` (default), `landscape`, `square` |
| `detail`       | `detailed` (default), `standard`, `concise` |
| `style`        | Optional NotebookLM infographic style     |
| `instructions` | Optional free-text steer                  |

When it finishes, grab the URL from the run **Summary**.

---

## Hosting note (public vs private repo)

The returned `raw.githubusercontent.com` URL is only fetchable **without auth
when the repo is public**. If you keep the repo private, that URL won't load in a
client app. Options:
- Make this repo **public** (the images are non-sensitive), **or**
- Swap the hosting step for an upload to object storage / Google Drive and return
  that URL instead. `stage_and_manifest()` in `generate.py` is the single place
  to change.

---

## Run locally

```bash
pip install -r requirements.txt

# Real generation (requires `notebooklm login` first):
python generate.py --text "Exams begin Monday. Bring your hall ticket." \
  --title "Exam notice" --repo you/this-repo --ref main

# Dry run (no network, emits a placeholder PNG to exercise the pipeline):
python generate.py --dry-run --text "x" --repo you/this-repo --ref main
```

## Tests

Pure offline tests (no network, no NotebookLM):

```bash
python -m unittest discover -s tests -v
```

---

## Files

| File | Purpose |
|------|---------|
| `generate.py` | The worker: text → infographic PNG → manifest |
| `.github/workflows/generate-infographic.yml` | On-demand workflow |
| `requirements.txt` | Runtime dependency |
| `tests/test_generate.py` | Offline unit tests |
| `generated/` | Committed images + `latest.json` (created on first run) |

# `frappe_import.py` — import an export into Frappe LMS

Recreate a course from a [`thinkific_export.py`](thinkific_export.md) folder inside
a running [Frappe LMS](https://github.com/frappe/lms) instance, over Frappe's REST
API. It creates the `LMS Course`, its `Course Chapter`s and `Course Lesson`s,
uploads the videos / PDFs / audio, and rebuilds quizzes (`LMS Quiz` +
`LMS Question`).

You point it at an export folder and name one course; it builds that course in
Frappe. It reads the export's `manifest.yaml` and per-lesson JSON — it does **not**
talk to Thinkific.

## What makes it safe to re-run

- **Resume / start-from-existing.** Courses, chapters and lessons are matched by
  **title**. Anything that already exists is reused, and only the missing pieces
  are created. So:
  - a run that failed half-way just continues where it stopped, and
  - a course you created **by hand** in Frappe first (matching the export's title)
    is picked up and filled in with its chapters, lessons and media.

  Existing lessons are left untouched unless you pass `--update-existing`.
- **Ordering is rebuilt from the manifest** on every run — the course's
  `Chapter Reference` list and each chapter's `Lesson Reference` list are set to
  the manifest order (extra rows already present are kept, appended after). So
  chapters and lessons end up ordered exactly as on Thinkific.

## Requirements

- A running Frappe LMS site you can reach over HTTP(S), and a **User** on it with
  permission to create courses.
- An **API key + secret** for that user (Frappe Desk → the User → *Settings → API
  Access → Generate Keys*). Auth is the standard Frappe token header
  `Authorization: token <key>:<secret>`.
- Python 3.9+ (this repo uses [`uv`](https://docs.astral.sh/uv/)).

## Install

```bash
uv sync                       # importer needs only requests + pyyaml
uv sync --extra vimeo         # add this only if you'll use --video-mode vimeo
```

Or with plain `pip`: `pip install requests pyyaml` (plus `PyVimeo` for the Vimeo
mode).

## Configure credentials

Set these once in your shell (the importer reads them from the environment; CLI
flags override):

```bash
export FRAPPE_URL="https://lms.example.com"
export FRAPPE_API_KEY="xxxxxxxxxxxxxxx"
export FRAPPE_API_SECRET="yyyyyyyyyyyyyyy"
export FRAPPE_INSTRUCTOR="jane@example.com"   # optional; defaults to the key's own user
```

## Usage

```bash
# Import one course (folder name, slug, numeric id, or exact title)
uv run frappe-import --export-dir ./thinkific_export --course my-course

# Import every course in the export folder
uv run frappe-import --export-dir ./thinkific_export --all

# Preview everything without writing to Frappe
uv run frappe-import --export-dir ./thinkific_export --course my-course --dry-run

# Upload videos to Vimeo (non-public) instead of into the LMS
uv run frappe-import --export-dir ./thinkific_export --course my-course --video-mode vimeo

# Publish the course, and re-process lessons that already exist
uv run frappe-import --export-dir ./thinkific_export --course my-course \
    --publish --update-existing
```

Always try `--dry-run` first: it walks the whole export and prints exactly what it
would create, without touching Frappe or uploading anything.

### Options

| Flag | Description |
|------|-------------|
| `--export-dir DIR` | The export root (the `thinkific-export --work-dir`). **Required.** |
| `--course X` | Which course to import: its folder name, slug, numeric id, or exact title. |
| `--all` | Import **every** course under `--export-dir`. (Give exactly one of `--course` / `--all`.) |
| `--frappe-url URL` | Frappe site URL (or `FRAPPE_URL`). |
| `--api-key` / `--api-secret` | Frappe API credentials (or `FRAPPE_API_KEY` / `FRAPPE_API_SECRET`). |
| `--instructor EMAIL` | User set as course instructor (or `FRAPPE_INSTRUCTOR`; else the API key's own user). |
| `--video-mode {frappe,vimeo,link}` | How to handle videos (see below). Default `frappe`. |
| `--publish` | Mark the course published (default: draft). |
| `--private-files` | Upload assets as **private** Frappe files (default: public). |
| `--update-existing` | Re-process lessons that already exist (default: skip them). |
| `--dry-run` | Print the plan; write nothing. |
| `--vimeo-view {unlisted,disable,nobody,anybody}` | Vimeo privacy (default `unlisted`). |
| `--vimeo-embed {public,whitelist,private}` | Vimeo embed policy (default `public`). |
| `--vimeo-domain HOST` | Domain to whitelist for embedding (default: the Frappe host). |

## Video modes

| `--video-mode` | What happens |
|----------------|--------------|
| `frappe` (default) | The local MP4 is uploaded to the Frappe site and embedded in the lesson with `{{ Video("/files/...") }}`. Simple, but large media lives on the LMS server. |
| `vimeo` | The local MP4 is uploaded to **Vimeo** as a non-public video and the Vimeo player URL is embedded with `{{ Embed("...\|\|\|https://player.vimeo.com/video/ID") }}`. Keeps big files off the LMS. Requires `uv sync --extra vimeo` and `VIMEO_ACCESS_TOKEN`. |
| `link` | Nothing is uploaded; the manifest's source `video_url` is embedded as-is. Fast for a structural import (note: Thinkific S3 URLs are signed and may expire). |

If a video lesson has no local file on disk (e.g. the export ran with
`--no-videos`), the importer records the source URL instead of uploading.

## How Thinkific maps to Frappe LMS

| Thinkific | Frappe LMS |
|-----------|------------|
| Course | `LMS Course` (title, one instructor, draft unless `--publish`) |
| Chapter | `Course Chapter` (linked to the course; ordered via `Chapter Reference`) |
| Lesson | `Course Lesson` (linked to the chapter; ordered via `Lesson Reference`) |
| Text lesson | lesson `body` = the lesson's HTML |
| Video lesson | video embed macro + the HTML caption in `body` |
| Audio lesson | `{{ Audio("...") }}` + caption |
| Pdf lesson | `{{ PDF("...") }}` |
| Quiz lesson | `LMS Quiz` + `LMS Question` docs; the quiz is attached via the lesson's `quiz_id` |
| free preview lesson | `include_in_preview = 1` |
| Other lesson types | a lesson with only title/type (body is a placeholder comment) |

The lesson `body` uses Frappe LMS's Markdown + macro syntax; HTML from Thinkific is
embedded inline (Markdown passes raw HTML through).

### Quizzes

Each Thinkific question becomes an `LMS Question` of type **Choices**: the prompt
is kept as HTML, choices fill `option_1..option_N`, and each correct choice
(`credited: true` in the export) sets the matching `is_correct_N`. Multi-answer
questions set `multiple = 1`. The questions are collected into one `LMS Quiz`
(passing percentage from Thinkific's `passingScore`, default 60), which is linked
to the lesson via `quiz_id` so it renders under the lesson.

## Vimeo mode details

1. Create a Personal Access Token at
   [developer.vimeo.com](https://developer.vimeo.com/) with scopes
   **`public private edit upload`**, and request the **Upload** capability on your
   app. Set `VIMEO_ACCESS_TOKEN` (and optionally `VIMEO_CLIENT_ID` /
   `VIMEO_CLIENT_SECRET`).
2. Privacy: `--vimeo-view unlisted` (default) means *anyone with the link*;
   `--vimeo-view disable` hides the video from vimeo.com entirely (embed-only).
   `--vimeo-embed whitelist` restricts embedding to `--vimeo-domain` (defaults to
   your Frappe host). **`disable` and embed-whitelisting require a paid Vimeo
   plan**; on a free account use `unlisted`.
3. Uploads are resumable (tus). After upload the importer polls until Vimeo
   finishes transcoding, then embeds `https://player.vimeo.com/video/<id>`.

## Notes & limitations

- **Idempotency is by title.** If you rename a lesson in Frappe, the next run will
  see it as missing and create a duplicate. Keep titles matching the export while
  importing.
- **Re-uploads.** Because existing lessons are skipped, media isn't re-uploaded on
  a resume. If you force `--update-existing`, assets referenced by that lesson are
  uploaded again (Frappe does not de-duplicate `File`s by content).
- **Structure-only lesson types** (Survey, Exam, Assignment, Presentation, …) have
  no captured body in the export, so they become title/type-only lessons.
- **Course metadata** (description, image, category, tags) is not present in the
  export; the course gets a minimal placeholder description. If you created the
  course by hand first, its existing fields are left untouched.
- Per-question explanations from Thinkific aren't carried over (Frappe's
  `LMS Question` has per-option, not per-question, explanation fields).

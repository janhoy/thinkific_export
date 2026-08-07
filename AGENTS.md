# AGENTS.md

This file provides guidance to coding agents (Claude Code and others) when working with code in this repository.

## Overview

Two single-file CLIs that form **one `uv`-managed project with two executables**
(`pyproject.toml`, hatchling build, console scripts `thinkific-export` and
`frappe-import`):

- **`thinkific_export.py`** — exports a whole Thinkific course site (structure plus
  every downloadable asset: videos, PDFs, audio) into a self-documenting folder:
  one `manifest.yaml` per course, the media files, and a `FORMAT.md` spec.
- **`frappe_import.py`** — reads such an export folder and recreates a course in a
  **Frappe LMS** instance (https://github.com/frappe/lms) over its REST API:
  courses, chapters, lessons, quizzes, and media (uploaded to Frappe, to a
  non-public Vimeo account, or referenced by source URL).

Each script is self-contained (dataclass `Config`, argparse, lazy optional
imports). There is no test suite; lint is `ruff` (config in `pyproject.toml`).
Detailed user docs live in `docs/thinkific_export.md` and `docs/frappe_import.md`;
`README.md` is a short project overview linking both.

## Setup & Commands

```bash
uv sync                              # venv + deps (requests, pyyaml, playwright)
uv sync --extra vimeo                # add PyVimeo, only for frappe-import --video-mode vimeo
uv run playwright install chromium   # one-time, for the exporter's login browser

# Exporter
uv run thinkific-export --subdomain myschool             # log in if needed, then export everything
uv run thinkific-export --subdomain myschool --login     # force a fresh interactive login, then export
uv run thinkific-export --subdomain myschool --course 12345  # only this course (numeric id)
uv run thinkific-export --subdomain myschool --no-videos # structure + PDFs/audio; record video URLs but skip video files

# Importer (Frappe creds via env: FRAPPE_URL, FRAPPE_API_KEY, FRAPPE_API_SECRET, FRAPPE_INSTRUCTOR)
uv run frappe-import --export-dir ./export --course my-course --dry-run   # preview, writes nothing
uv run frappe-import --export-dir ./export --all                         # every course in the folder
uv run frappe-import --export-dir ./export --course my-course --video-mode vimeo
```

Lint with `uv run ruff check .` (rule set `E,F,W,I,B`; `E501`/pyupgrade off on
purpose — see `pyproject.toml`). CI runs the same on PRs
(`.github/workflows/lint.yml`); Dependanbot (`.github/dependabot.yml`) bumps
Actions + Python deps monthly. Syntax-check either script with
`python -m py_compile <file>`. Playwright (exporter) and `vimeo` (importer) are
imported lazily, so both scripts stay importable/compileable without them.

Exporter config resolution: **CLI flag > env var (`THINKIFIC_SUBDOMAIN`,
`THINKIFIC_WORKDIR`)**. `subdomain` is **required** (no default). `work_dir` falls
back to `./thinkific_export`. Auth is a saved browser session, not a key. The
importer needs Frappe API key/secret (token auth) from env/flags — nothing else is
secret.

## Architecture: why GraphQL, and the query flow

The central design decision: **Thinkific's public REST API is gated behind a paid plan
(Pro + Growth), so this tool does not use it.** Instead it drives the *admin dashboard's own*
GraphQL API at `/api/graphql`, which is authenticated by an ordinary logged-in session — the
same access any admin has in a browser. This is the whole reason the tool exists in its current
form; do not reach for `api.thinkific.com/api/public/v1` (that path needs the plan).

Two pieces make it work:

- **Session auth (`login`)** opens a real Chrome window (headful) via Playwright; you sign in
  manually (SSO/2FA fine — no password touches the script) and the session is saved to
  `_videos`'s sibling `.thinkific_session.json` under `work_dir`. `main()` runs this
  automatically whenever `--login` is passed or no saved session exists, then proceeds to export.

- **In-page GraphQL replay (`export`)** loads that session headless, navigates to an
  authenticated admin page, and runs every query via `page.evaluate` of `_GQL_JS` — a `fetch('/api/graphql')`
  executed *inside the page*, so the session cookie and the page's `<meta name="csrf-token">`
  are applied automatically. Replaying from inside the page is what avoids reconstructing auth/CSRF
  headers by hand. If the landing URL redirects to a sign-in page (or a query returns non-200), the
  session has expired and the tool tells you to re-run with `--login`.

### The three-query walk (see the `_GQL_*` constants)

1. `_GQL_COURSES` — paginated course list.
2. `_GQL_LESSONS` — one course's chapters + lessons. `lessons` is a GraphQL **union**, so common
   fields (`name`, `type`, …) are selected via `... on LessonInterface` while `id` must be selected
   per concrete type (`... on VideoLesson{id}`, etc.) — that split is why the query lists every
   lesson type explicitly.
3. `_GQL_DETAIL` — per-lesson detail via `node(id:)`. Inline fragments cover the asset-bearing types
   (Text/Audio/Pdf/Video) plus Quiz (`questions` → `choices`, where `choice.credited: true` marks the
   correct answer — works for both `radio` single-answer and `checkbox` multi-answer). Remaining types
   (Survey/…) return just `__typename`.

**ID translation gotcha:** `_GQL_LESSONS` returns lesson ids as base64 `VideoLesson-<n>`, but the
`node()` detail query wants base64 `Content-<n>` (same numeric id, different prefix). `_gid_num`
decodes the number; `_content_gid` re-encodes it as `Content-<n>`. `_gid_num` is also how the
manifest's numeric course/chapter/lesson ids are derived from the base64 gids.

### Direct downloads

The detail query returns **directly downloadable URLs**: `video.url` (S3), and `url` for
PDF/audio (Thinkific CDN). Most are fetchable with a plain unauthenticated `GET` (they support
range requests), so `stream_download` pulls them with a bare `requests.Session` — no cookies,
no click-through. Each `Video` lesson is linked to its exact file in one pass (no filename matching).

**Wistia fallback (important):** not every video sits on the public S3 bucket. Some `video.url`s
point at a locked-down `files.cdn.thinkific.com` origin that returns **HTTP 403** — those videos are
hosted on **Wistia**. `wistia_download_url()` handles them: it fetches the lesson's `video.playUrl`
(a videoproxy player page — plain `requests`, no CORS issue), scrapes the Wistia media id from the
HTML, then reads Wistia's public media JSON (`fast.wistia.com/embed/medias/<id>.json`) and returns a
downloadable MP4 asset (preferring the `original` source upload, else the largest MP4 rendition).
`_process_lesson` tries `video.url` first and only falls back to Wistia on failure. In practice a
large fraction of a site's videos can require this path — a naive S3-only download silently misses
the Wistia-hosted ones. `video.playUrl` itself is a player HTML page, never the media.

### Output layout (all under `work_dir`)

```
FORMAT.md                             # self-documenting spec of this dump (written every run from FORMAT_DOC)
<course-slug>/manifest.yaml           # structure + summary + per-lesson file/video links
<course-slug>/content/NNN_*.json           # {lesson, detail} raw GraphQL payload per lesson
<course-slug>/content/NNN_*.pdf|mp3        # downloaded PDF/audio assets
<course-slug>/content/NNN_*_thumbnail.jpg  # video poster thumbnails (fetched even with --no-videos)
_videos/*.mp4                         # downloaded videos (original filenames)
_videos/videos_manifest.yaml          # flat index of every downloaded video
.thinkific_session.json               # saved Playwright session (auth — keep private)
```

Each manifest lesson carries `download_status` ∈ {`downloaded`, `manual_required`, `no_asset`};
video lessons also carry `video_url`, `video_file` (when downloaded), and `thumbnail_url` /
`thumbnail_file` for the poster image. The thumbnail comes from `video.thumbnail` (a Wistia
delivery JPEG) and is downloaded regardless of `--no-videos`, since it's small and useful for
re-import.

## Architecture: `frappe_import.py` (export → Frappe LMS)

Reads an export folder (never touches Thinkific) and drives **Frappe's REST API**
with API key/secret token auth (`Authorization: token <key>:<secret>`). The thin
`Frappe` client does list/get/create/update + `upload_file`; a `dry_run` flag makes
all writes (and the reads `get_list`/`logged_user`) no-ops that return placeholders,
so `--dry-run` exercises the whole walk offline.

**The Frappe LMS data model it targets** (verified against frappe/lms):
- `LMS Course` — `name` is an autogenerated slug; set `title`, `short_introduction`
  (reqd), `description` (reqd, HTML), and at least one `instructors` child row
  (`{instructor: <User email>}`, reqd).
- `Course Chapter` — has a **required `course` Link**, AND the course orders it via
  a `Chapter Reference` child row. Both are needed; the reference row's implicit
  `idx` is the order.
- `Course Lesson` — required `chapter` Link (course auto-fetched); ordered by a
  `Lesson Reference` child row on the chapter. Content goes in `body`
  (Markdown + macros). Quiz attaches via the lesson's `quiz_id`.
- Quizzes: standalone `LMS Question` docs (type `Choices`; options in flat
  `option_1..10` with `is_correct_1..10`; `multiple=1` for multi-answer) collected
  into an `LMS Quiz` (`questions` child rows of `{question, marks}`).

**Lesson `body` macros** (the stable, server-rendered contract — preferred over the
frontend's EditorJS `content` JSON): `{{ Video("/files/x.mp4") }}`,
`{{ Audio(...) }}`, `{{ PDF(...) }}`, `{{ YouTubeVideo("id") }}`,
`{{ Embed("title|||url") }}` (Vimeo/other), `{{ Quiz("quiz-name") }}`. HTML from
Thinkific (`detail.htmlDescription`) is embedded inline (Markdown passes it through).

**Resume/idempotency:** courses/chapters/lessons are matched by **title**; existing
ones are reused (so a hand-created course is filled in, and a failed run resumes).
Existing lessons are skipped unless `--update-existing`. Ordering is rebuilt from
the manifest every run via `_ensure_order` (manifest order first, extra existing
rows appended). Matching by title means renaming in Frappe causes a duplicate.

**Video modes** (`--video-mode`): `frappe` uploads the MP4 to the LMS
(`upload_file` → `{{ Video(file_url) }}`); `vimeo` uploads to Vimeo as non-public
(PyVimeo, lazy import; `view=disable`+`embed=whitelist` needs a paid plan; polls
transcode) → `{{ Embed(...) }}`; `link` embeds the manifest `video_url` as-is.
`--all` imports every `*/manifest.yaml` under the export root.

## Conventions specific to this codebase

- **Video counts won't match the library:** the admin video library (`site.videos`) typically holds
  more videos than the courses reference, because old/unused uploads linger there without being
  attached to any course. A full export downloads only the videos referenced by lessons, so expect
  the exported count to be lower than the library count — that's correct.
- **`safe_name()` sanitizes every filename** written to disk; keep new file writes routed through it.
- **Idempotent downloads:** `stream_download` skips an existing non-empty destination and writes
  via a `.part` temp file first, so re-running resumes rather than redownloads.
- **`manifest.yaml` is the source of truth**, one per course, regenerated on each run.
- To handle a **new lesson type** that carries a file, add an inline fragment to `_GQL_DETAIL`
  and a branch in `_process_lesson` (exporter) — everything else flows from `type`/`url` — and a
  branch in `frappe_import.py`'s `_build_body` (importer) if it should render specially in Frappe.
- **Placeholders only in docs/examples:** never commit a real subdomain, course id, Frappe host, or
  personal name — use `myschool`, `12345`, `lms.example.com`, `Jane Doe`.

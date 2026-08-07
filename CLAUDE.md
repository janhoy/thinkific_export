# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file CLI (`thinkific_export.py`) that exports a whole Thinkific course site —
course structure plus every downloadable asset (videos, PDFs, audio) — writing one
`manifest.yaml` per course and the media files. The project is a single self-sufficient
Python script; there is no config file, package, build step, or test suite.

## Setup & Commands

```bash
pip install requests pyyaml playwright && playwright install chromium

python thinkific_export.py --subdomain myschool             # log in if needed, then export everything
python thinkific_export.py --subdomain myschool --login     # force a fresh interactive login, then export
python thinkific_export.py --subdomain myschool --course 12345  # only this course (numeric id)
python thinkific_export.py --subdomain myschool --no-videos # structure + PDFs/audio; record video URLs but skip video files
python thinkific_export.py --subdomain myschool --work-dir ./out
```

There is no lint or test config. Syntax-check with `python -m py_compile thinkific_export.py`.
Playwright is imported lazily, so the script stays importable/compileable without it installed.

Config resolution: **CLI flag > env var (`THINKIFIC_SUBDOMAIN`, `THINKIFIC_WORKDIR`)**.
`subdomain` is **required** (no default) — the script exits if neither `--subdomain` nor
`THINKIFIC_SUBDOMAIN` is set. `work_dir` falls back to `./thinkific_export`. There is
nothing secret to configure — auth is a saved browser session, not a key.

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
  and a branch in `_process_lesson` — everything else flows from `type`/`url`.

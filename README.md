# thinkific_export

Export an entire [Thinkific](https://www.thinkific.com/) course site — the full
course structure **plus every downloadable asset** (videos, PDFs, audio) — into a
self-contained, self-documenting folder you own. One `manifest.yaml` per course,
the media files on disk, and a `FORMAT.md` spec so the backup can be re-imported
into another LMS by a human or a script.

It's a single Python file. No account API keys, no paid plan, no server.

## Why this exists

Thinkific's official REST API (`api.thinkific.com/api/public/v1`) is gated behind
a paid plan. This tool **does not use it**. Instead it drives the admin
dashboard's *own* GraphQL API (`/api/graphql`) — the same one your browser uses
when you click around the admin UI — authenticated by an ordinary logged-in
session. If you can see your courses in the Thinkific admin dashboard, you can
export them.

You log in once, in a real browser window the tool opens for you (SSO and 2FA
work fine). No password is ever typed into the script; only the resulting session
cookie is saved locally and reused.

## Requirements

- Python 3.9+
- Admin access to the Thinkific site you want to export

## Install

```bash
pip install requests pyyaml playwright
playwright install chromium
```

## Usage

The only required input is your Thinkific **subdomain** — for
`https://myschool.thinkific.com` that's `myschool`.

```bash
# Log in if needed (opens a browser the first time), then export everything
python thinkific_export.py --subdomain myschool

# Force a fresh interactive login first (e.g. after the session expires)
python thinkific_export.py --subdomain myschool --login

# Export a single course by its numeric id
python thinkific_export.py --subdomain myschool --course 12345

# Export structure + PDFs/audio and record video URLs, but skip downloading video files
python thinkific_export.py --subdomain myschool --no-videos

# Choose where to write output (default: ./thinkific_export)
python thinkific_export.py --subdomain myschool --work-dir ./out
```

On the first run a Chrome window opens on the Thinkific sign-in page. Sign in,
then press Enter in the terminal — the session is saved and the export begins
headless. Subsequent runs reuse the saved session until it expires.

### Options

| Flag | Description |
|------|-------------|
| `--subdomain X` | Thinkific subdomain (**required**; or set `THINKIFIC_SUBDOMAIN`). |
| `--work-dir DIR` | Output directory (default `./thinkific_export`; or `THINKIFIC_WORKDIR`). |
| `--login` | Force a fresh interactive login before exporting. |
| `--course ID` | Export only this numeric course id. |
| `--no-videos` | Skip downloading video files (still records their URLs + thumbnails). |

Configuration precedence: **CLI flag > environment variable**. There is no config
file — auth is the saved browser session, not a key.

## Output

Everything is written under `--work-dir`:

```
<work_dir>/
├── FORMAT.md                          # full spec of this export (written every run)
├── <course-slug>/
│   ├── manifest.yaml                  # course tree + per-lesson asset links
│   └── content/
│       ├── NNN_<lesson>.json          # raw GraphQL payload per lesson
│       ├── NNN_<lesson>.pdf|.mp3       # downloaded PDF / audio asset
│       └── NNN_<lesson>_thumbnail.jpg  # video poster image
├── _videos/
│   ├── <original name>.mp4            # every downloaded video (flat, de-duplicated)
│   └── videos_manifest.yaml           # flat index of downloaded videos
└── .thinkific_session.json            # saved login session — keep private, don't commit
```

Each lesson in `manifest.yaml` carries a `download_status` of `downloaded`,
`manual_required`, or `no_asset`, and video lessons additionally record
`video_url`, `video_file`, and thumbnail paths. **`FORMAT.md`, generated into
every export, documents every file and field** — including the lesson types, the
id scheme, and a step-by-step recipe for re-importing into another LMS.

## How it works

1. **Login** opens a headful Chrome via Playwright; you authenticate manually and
   the session is saved to `.thinkific_session.json`.
2. **Export** loads that session headless, opens an authenticated admin page, and
   replays GraphQL queries from *inside* the page (`fetch('/api/graphql')`), so
   the session cookie and CSRF token are applied automatically — no header
   reconstruction.
3. Three queries walk the site: the course list, each course's chapters +
   lessons, and per-lesson detail. The detail query returns **direct download
   URLs** for videos (S3), PDFs, and audio, which are fetched with plain HTTP
   requests.
4. Videos not on the public S3 bucket are hosted on **Wistia**; the tool detects
   the 403, reads the lesson's player page to find the Wistia media id, and pulls
   the downloadable MP4 from Wistia's public media JSON.

Downloads are idempotent — an existing non-empty file is skipped, and downloads
write to a `.part` temp file first, so re-running resumes rather than
re-downloads.

## Notes & limitations

- Only videos **attached to a lesson** are exported. Old/unused uploads sitting in
  the Thinkific video library are intentionally skipped.
- Lesson types that carry a body — Text, Video, Audio, Pdf, Quiz — are captured in
  full. Other types (Survey, Exam, Assignment, Presentation, …) are captured at
  structure level only (title, type, order); see `FORMAT.md` §5.
- Quizzes capture questions, choices, and which choices are correct
  (`credited: true`), for both single- and multiple-answer questions.
- `draft` (unpublished) lessons are included; filter them if you only want
  published content.

## Privacy

`.thinkific_session.json` is your live login session — treat it like a password.
Add it (and your export output) to `.gitignore`; never commit it.

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under the [Apache License 2.0](LICENSE).

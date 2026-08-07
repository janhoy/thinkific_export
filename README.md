# thinkific-tools

Two small command-line tools for moving a [Thinkific](https://www.thinkific.com/)
course site somewhere you control:

1. **[`thinkific-export`](docs/thinkific_export.md)** — export an entire Thinkific
   site (course structure **plus every downloadable asset**: videos, PDFs, audio)
   into a self-contained, self-documenting folder. No paid plan, no account API
   key — just your logged-in browser session.
2. **[`frappe-import`](docs/frappe_import.md)** — take that export and recreate a
   course inside a [Frappe LMS](https://github.com/frappe/lms) instance over its
   REST API: chapters, lessons, quizzes, and media (uploaded to Frappe or to a
   non-public Vimeo account).

Each is a single self-contained Python file; together they form one
[`uv`](https://docs.astral.sh/uv/)-managed project with two executables.

## Setup

Install [`uv`](https://docs.astral.sh/uv/), then from the repo root:

```bash
uv sync                        # create the venv + install dependencies
uv run playwright install chromium   # one-time, for the exporter's login browser
```

Optional extra, only for uploading videos to Vimeo:

```bash
uv sync --extra vimeo
```

Prefer not to use `uv`? Each tool also runs under a plain
`pip install requests pyyaml playwright` — see the per-tool docs.

## Quick start

```bash
# 1. Export your Thinkific site (opens a browser to log in the first time)
uv run thinkific-export --subdomain myschool --work-dir ./export

# 2. Recreate a course in Frappe LMS (credentials via env — see the doc)
export FRAPPE_URL=https://lms.example.com
export FRAPPE_API_KEY=... FRAPPE_API_SECRET=...
uv run frappe-import --export-dir ./export --course my-course --dry-run   # preview
uv run frappe-import --export-dir ./export --all                          # do it
```

## Documentation

| Tool | Doc |
|------|-----|
| `thinkific-export` | [docs/thinkific_export.md](docs/thinkific_export.md) — why the admin GraphQL API, options, output layout, the Wistia video fallback. |
| `frappe-import` | [docs/frappe_import.md](docs/frappe_import.md) — Frappe setup, video modes (Frappe / Vimeo / link), the resume-from-existing behaviour, and how lessons/quizzes map. |

The export folder is **self-documenting**: every run writes a `FORMAT.md` into it
that specifies every file and field, so the backup can be re-imported by any tool
or by hand. Design notes for both tools live in [AGENTS.md](AGENTS.md).

## Privacy

The exporter saves your live login session to `.thinkific_session.json` — treat it
like a password; it is already in `.gitignore`. Frappe/Vimeo credentials are read
from environment variables, never stored. Never commit session files, export
output, or real subdomains / course ids / personal names.

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under the [Apache License 2.0](LICENSE).

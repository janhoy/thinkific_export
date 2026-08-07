# Contributing

Thanks for your interest in improving **thinkific-tools**! Contributions of all
kinds — bug reports, fixes, docs, new lesson-type support — are welcome.

## Getting started

The repo is one [`uv`](https://docs.astral.sh/uv/)-managed project with two
single-file executables (`thinkific_export.py`, `frappe_import.py`). No package
directory, no config file.

```bash
git clone <your-fork-url>
cd thinkific_export
uv sync                              # deps for both tools
uv sync --extra vimeo                # optional: PyVimeo, for the importer's Vimeo mode
uv run playwright install chromium   # for the exporter's login browser
```

Playwright (exporter) and `vimeo` (importer) are imported lazily, so both scripts
stay importable and syntax-checkable even without them installed.

## Before you open a pull request

- **Lint:** `uv run ruff check .` — CI runs the same on every PR
  (`.github/workflows/lint.yml`). The rule set (`E,F,W,I,B`, with `E501` and
  pyupgrade off) lives in `pyproject.toml`.
- **Syntax check:** `python -m py_compile thinkific_export.py frappe_import.py`
- **No private data:** never commit `.thinkific_session.json`, an export output
  folder, real Frappe/Vimeo credentials, or any real subdomain / course id /
  Frappe host / personal name. Use placeholders like `myschool`, `12345`,
  `lms.example.com`.
- **Keep it dependency-light:** the base deps are `requests`, `pyyaml`,
  `playwright`; `PyVimeo` is an optional extra. Please don't add heavy
  dependencies without discussing it in an issue first.
- **Exporter:** route every filename written to disk through `safe_name()`. If you
  add support for a **new lesson type that carries a file**, add an inline fragment
  to `_GQL_DETAIL` and a branch in `_process_lesson`, and update the `FORMAT_DOC`
  spec so the export stays self-documenting.
- **Importer:** if a new lesson type should render specially in Frappe, add a branch
  in `_build_body`; keep the lesson `body` on the documented Markdown-macro contract.
- **Docs:** user-facing docs are `docs/thinkific_export.md` and
  `docs/frappe_import.md` (linked from `README.md`); update them with behaviour
  changes.

## Reporting bugs

Open an issue describing what you ran (command line, minus your subdomain /
credentials), what you expected, and what happened. Include the relevant console
output. Please redact any private URLs, ids, hosts, or session data.

## Design notes

See [AGENTS.md](AGENTS.md) for an in-depth explanation of both tools — why the
exporter uses the admin GraphQL API instead of the paid public REST API, the
three-query walk, the id-translation gotcha, the Wistia fallback, and how the
importer maps the export onto the Frappe LMS data model.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE), the same license as the project.

# Contributing

Thanks for your interest in improving **thinkific_export**! Contributions of all
kinds — bug reports, fixes, docs, and new lesson-type support — are welcome.

## Getting started

```bash
git clone <your-fork-url>
cd thinkific_export
pip install requests pyyaml playwright
playwright install chromium
```

The whole tool is a single file, `thinkific_export.py`. There is no package,
build step, or config file. Playwright is imported lazily, so the script stays
importable and syntax-checkable even without it installed.

## Before you open a pull request

- **Syntax check:** `python -m py_compile thinkific_export.py`
- **No private data:** never commit `.thinkific_session.json`, an export output
  folder, or any real subdomain / course id / personal name. Use placeholders
  like `myschool` and `12345` in docs and examples.
- **Keep it self-contained:** the script is deliberately dependency-light
  (`requests`, `pyyaml`, `playwright`). Please don't add heavy dependencies
  without discussing it in an issue first.
- **Route file writes through `safe_name()`** so every filename stays
  filesystem-safe.
- If you add support for a **new lesson type that carries a file**, add an inline
  fragment to the `_GQL_DETAIL` query and a branch in `_process_lesson`, and
  update the `FORMAT_DOC` spec so the export stays self-documenting.

## Reporting bugs

Open an issue describing what you ran (command line, minus your subdomain),
what you expected, and what happened. Include the relevant console output. Please
redact any private URLs, ids, or session data.

## Design notes

See [CLAUDE.md](CLAUDE.md) for an in-depth explanation of the architecture — why
the tool uses the admin GraphQL API instead of the paid public REST API, the
three-query walk, the id-translation gotcha, and the Wistia fallback for videos
that aren't on the public S3 bucket.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE), the same license as the project.

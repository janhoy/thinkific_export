#!/usr/bin/env python3
"""
thinkific_export.py — export a Thinkific course site
====================================================

Exports course STRUCTURE and every downloadable asset (videos, PDFs, audio) for
all courses on your Thinkific site, writing one ``manifest.yaml`` per course plus
the media files.

How it works
------------
Thinkific's public REST API is gated behind a paid plan (Pro + Growth). This tool
avoids it entirely: it drives the *admin dashboard's* own GraphQL API
(``/api/graphql``), authenticated by your normal login session — the same access
you have in a browser. It replays the dashboard's queries from inside an
authenticated page (so cookies + CSRF are handled automatically), which yields
the course tree AND direct download URLs for videos (S3), PDFs and audio. Those
URLs are fetchable with a plain GET, so each lesson is linked to its exact file
in one pass — no clicking, no filename guessing.

Auth: you log in once in a browser window this tool opens (SSO / 2FA all fine);
no password ever touches the script. The session is saved and reused.

Setup:  pip install requests pyyaml playwright  &&  playwright install chromium
Usage:
  python thinkific_export.py --subdomain myschool             # log in if needed, then export everything
  python thinkific_export.py --subdomain myschool --login     # force a fresh login, then export
  python thinkific_export.py --subdomain myschool --course 12345  # just one course (by numeric id)
  python thinkific_export.py --subdomain myschool --no-videos # structure + PDFs/audio, skip video files
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # keep importable/syntax-checkable without Playwright
    sync_playwright = None


_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")
_EXT_BY_CT = {
    "application/pdf": ".pdf", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "video/mp4": ".mp4", "image/png": ".png", "image/jpeg": ".jpg",
    "application/zip": ".zip",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    subdomain: str
    work_dir: Path = Path("./thinkific_export")

    @property
    def state_path(self) -> Path:
        return self.work_dir / ".thinkific_session.json"

    @staticmethod
    def load(subdomain: Optional[str] = None,
             work_dir: Optional[str] = None) -> "Config":
        sub = subdomain or os.getenv("THINKIFIC_SUBDOMAIN")
        if not sub:
            sys.exit("A Thinkific subdomain is required: pass --subdomain X "
                     "or set THINKIFIC_SUBDOMAIN.")
        wd = (work_dir or os.getenv("THINKIFIC_WORKDIR") or "./thinkific_export")
        return Config(subdomain=sub, work_dir=Path(wd).expanduser())


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #
def safe_name(name: str, maxlen: int = 120) -> str:
    name = html.unescape(name or "").strip()
    name = _SAFE.sub("_", name).strip("._ ")
    name = re.sub(r"\s+", " ", name)
    return (name or "untitled")[:maxlen]


def guess_ext(url: str, resp_ct: Optional[str] = None) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix
    if ext and len(ext) <= 6:
        return ext
    if resp_ct:
        return _EXT_BY_CT.get(resp_ct.split(";")[0].strip(), "")
    return ""


def filename_from_url(url: str) -> str:
    base = url.split("?", 1)[0].rsplit("/", 1)[-1] or "asset"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "asset"
    return base if "." in base else base + ".mp4"


def stream_download(url: str, dest: Path, session: requests.Session) -> Optional[int]:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"    - exists, skipping {dest.name}")
        return dest.stat().st_size
    try:
        with session.get(url, stream=True, timeout=180) as r:
            if r.status_code != 200:
                print(f"    x {r.status_code} for {dest.name}")
                return None
            tmp = dest.with_suffix(dest.suffix + ".part")
            size = 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        fh.write(chunk)
                        size += len(chunk)
            tmp.rename(dest)
        print(f"    v {dest.name}  ({size/1e6:.1f} MB)")
        return size
    except requests.RequestException as exc:
        print(f"    x error {dest.name}: {exc}")
        return None


_WISTIA_ID_RE = re.compile(r"wistia\.com/embed/medias/([A-Za-z0-9]+)")


def wistia_download_url(play_url: str, session: requests.Session) -> Optional[str]:
    """Resolve a Wistia-hosted lesson to a directly downloadable MP4 URL.

    Some videos aren't on the public S3 bucket — their `video.url` points at a
    locked-down CDN origin that 403s. Those are served by Wistia: the lesson's
    `playUrl` (videoproxy) renders a player whose HTML names the Wistia media id,
    and Wistia's public media JSON lists downloadable assets. We prefer the
    `original` source upload, else the largest MP4 rendition.
    """
    try:
        html = session.get(play_url, timeout=60).text
    except requests.RequestException:
        return None
    m = _WISTIA_ID_RE.search(html)
    if not m:
        return None
    try:
        media = session.get(f"https://fast.wistia.com/embed/medias/{m.group(1)}.json",
                            timeout=60).json()
    except (requests.RequestException, ValueError):
        return None
    assets = (media.get("media") or {}).get("assets") or []
    originals = [a for a in assets if a.get("type") == "original"]
    mp4s = [a for a in assets if a.get("ext") == "mp4"]
    pick = (originals[:1] or sorted(
        mp4s, key=lambda a: a.get("size") or a.get("bitrate") or 0, reverse=True)[:1])
    return pick[0].get("url") if pick else None


# --------------------------------------------------------------------------- #
# Admin GraphQL queries (replayed from inside an authenticated page)
# --------------------------------------------------------------------------- #
# The course list (paginated).
_GQL_COURSES = (
    "query($first:Int,$after:String){courses(first:$first,after:$after){"
    "pageInfo{hasNextPage endCursor} edges{node{id slug type "
    "description cardImageUrl defaultCourseCardImageUrl "
    "product{productableName status} instructor{fullName}}}}}"
)
# Chapters + lessons for one course. `lessons` is a union, so common fields come
# from LessonInterface and `id` from each concrete lesson type.
_GQL_LESSONS = (
    "query($id:ID!,$first:Int){course(id:$id){id slug "
    "chapters(first:$first){pageInfo{hasNextPage} edges{node{id name position "
    "lessons{"
    "... on LessonInterface{name type position draft free __typename}"
    "... on TextLesson{id} ... on VideoLesson{id} ... on AudioLesson{id}"
    "... on PdfLesson{id} ... on DownloadLesson{id} ... on MultimediaLesson{id}"
    "... on QuizLesson{id} ... on SurveyLesson{id} ... on ExamLesson{id}"
    "... on AssignmentLesson{id} ... on PresentationLesson{id}"
    "... on DisqusLesson{id} ... on LiveLesson{id} ... on CanvasLesson{id}"
    "}}}}}}"
)
# Per-lesson detail. Inline fragments for types that carry a file (Text/Audio/
# Pdf/Video) plus Quiz (questions + choices, where choice.credited flags the
# correct answer). Other types just return __typename. node() wants the base64
# "Content-<n>" id.
_GQL_DETAIL = (
    "query($id:ID!){node(id:$id){__typename "
    "... on TextLesson{htmlDescription} "
    "... on AudioLesson{url downloadable htmlDescription} "
    "... on PdfLesson{url downloadable} "
    "... on VideoLesson{downloadable htmlDescription video{"
    "url playUrl fileName contentType humanReadableFileSize durationInSeconds thumbnail}} "
    "... on QuizLesson{passingScore quizType showExplanation quizQuestionBank "
    "numberOfSelectedQuizQuestions questions{edges{node{"
    "id name prompt displayType textExplanation position "
    "choices{edges{node{id text credited position}}}}}}}"
    "}}"
)
# JS executed inside the authenticated page: POST GraphQL with the page's CSRF token.
_GQL_JS = """async ([query, variables]) => {
  const m = document.querySelector('meta[name="csrf-token"]');
  const r = await fetch('/api/graphql', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json',
              ...(m ? {'X-CSRF-Token': m.getAttribute('content')} : {})},
    body: JSON.stringify({query, variables})});
  return {status: r.status, body: await r.text()};
}"""


def _gid_num(gid: str) -> str:
    """base64 'VideoLesson-2542033' / 'Course-195835' -> '2542033' / '195835'."""
    try:
        return base64.b64decode(gid).decode().split("-", 1)[1]
    except Exception:  # noqa: BLE001
        return gid


def _content_gid(lesson_gid: str) -> str:
    """A lesson's typed id -> the base64 'Content-<n>' id that node() expects."""
    return base64.b64encode(f"Content-{_gid_num(lesson_gid)}".encode()).decode()


# --------------------------------------------------------------------------- #
# Login — save a browser session once (no password touches the script)
# --------------------------------------------------------------------------- #
def login(cfg: Config) -> None:
    if sync_playwright is None:
        sys.exit("Playwright not installed. Run: pip install playwright && playwright install chromium")
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"https://{cfg.subdomain}.thinkific.com/manage/courses",
                  wait_until="domcontentloaded")
        print("\nSign in to Thinkific in the open window (SSO / 2FA fine).")
        input("  -> press Enter once you can see your admin dashboard... ")
        ctx.storage_state(path=str(cfg.state_path))
        browser.close()
    print(f"v Session saved to {cfg.state_path}.")


# --------------------------------------------------------------------------- #
# Export — structure + all assets via admin GraphQL
# --------------------------------------------------------------------------- #
def export(cfg: Config, only_course: Optional[int] = None,
           skip_videos: bool = False) -> None:
    if sync_playwright is None:
        sys.exit("Playwright not installed. Run: pip install playwright && playwright install chromium")
    if not cfg.state_path.exists():
        sys.exit(f"No saved session at {cfg.state_path}. Run with --login first.")

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    (cfg.work_dir / "FORMAT.md").write_text(FORMAT_DOC, encoding="utf-8")
    media_dir = cfg.work_dir / "_videos"
    dl = requests.Session()
    dl.headers["User-Agent"] = "thinkific-export/3.0"
    saved_videos: dict[str, dict] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(cfg.state_path))
        page = ctx.new_page()
        page.goto(f"https://{cfg.subdomain}.thinkific.com/manage/courses",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        if re.search(r"sign[_-]?in|/users/sign_in|/login", page.url, re.IGNORECASE):
            sys.exit("Session expired — re-run with --login.")

        def gql(query: str, variables: dict) -> dict:
            res = page.evaluate(_GQL_JS, [query, variables])
            if res["status"] != 200:
                sys.exit(f"GraphQL HTTP {res['status']} — session may be expired; "
                         f"re-run with --login.\n{res['body'][:200]}")
            data = json.loads(res["body"])
            if data.get("errors"):
                raise RuntimeError(json.dumps(data["errors"])[:300])
            return data["data"]

        courses, after = [], None
        while True:
            conn = gql(_GQL_COURSES, {"first": 100, "after": after})["courses"]
            courses += [e["node"] for e in conn["edges"]]
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
        if only_course:
            courses = [c for c in courses if _gid_num(c["id"]) == str(only_course)]
        print(f"Found {len(courses)} course(s).\n")

        for course in courses:
            _export_course(course, gql, cfg, media_dir, dl, saved_videos, skip_videos)
        browser.close()

    if saved_videos:
        media_dir.mkdir(parents=True, exist_ok=True)
        vm = {"site": cfg.subdomain, "count": len(saved_videos),
              "videos": sorted(saved_videos.values(), key=lambda e: e["file"])}
        with open(media_dir / "videos_manifest.yaml", "w", encoding="utf-8") as fh:
            yaml.safe_dump(vm, fh, allow_unicode=True, sort_keys=False)
        print(f"\nv {len(saved_videos)} video(s) in {media_dir}")
    print("\nDone. Each manifest.yaml links every lesson to its downloaded file.")


def _export_course(course: dict, gql, cfg: Config, media_dir: Path,
                   dl: requests.Session, saved_videos: dict, skip_videos: bool) -> None:
    cid = _gid_num(course["id"])
    slug = course.get("slug") or f"course-{cid}"
    title = (course.get("product") or {}).get("productableName") or slug
    course_dir = cfg.work_dir / safe_name(slug)
    content_dir = course_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    print(f"> {title}  (id={cid})")

    manifest: dict[str, Any] = {
        "course": {"id": int(cid) if cid.isdigit() else cid, "name": title,
                   "slug": slug, "status": (course.get("product") or {}).get("status"),
                   "instructor": (course.get("instructor") or {}).get("fullName"),
                   "description": (course.get("description") or "").strip() or None},
        "chapters": [],
        "summary": {"downloaded": 0, "manual_required": 0, "no_asset": 0},
    }

    # Course card image (Settings -> course card image). Skip the site-wide
    # generic default so we only capture a real, course-specific thumbnail.
    card = course.get("cardImageUrl")
    if card and card != course.get("defaultCourseCardImageUrl"):
        manifest["course"]["image_url"] = card
        idest = content_dir / f"course_thumbnail{guess_ext(card) or '.png'}"
        manifest["course"]["image_file"] = (
            f"content/{idest.name}" if stream_download(card, idest, dl) else None)

    chapters = gql(_GQL_LESSONS, {"id": course["id"], "first": 1000})["course"]["chapters"]
    n = 0
    for ce in chapters["edges"]:
        node = ce["node"]
        ch_entry = {"position": node.get("position"), "id": _gid_num(node["id"]),
                    "name": node.get("name"), "lessons": []}
        for les in node.get("lessons", []):
            n += 1
            entry = _process_lesson(les, n, gql, content_dir, media_dir,
                                    dl, saved_videos, skip_videos)
            ch_entry["lessons"].append(entry)
            manifest["summary"][entry["download_status"]] += 1
        manifest["chapters"].append(ch_entry)

    with open(course_dir / "manifest.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, allow_unicode=True, sort_keys=False)
    s = manifest["summary"]
    print(f"  manifest -> {course_dir/'manifest.yaml'}")
    print(f"  downloaded={s['downloaded']} manual_required={s['manual_required']} "
          f"no_asset={s['no_asset']}\n")


def _process_lesson(les: dict, n: int, gql, content_dir: Path, media_dir: Path,
                    dl: requests.Session, saved_videos: dict, skip_videos: bool) -> dict:
    name = les.get("name", f"lesson-{n}")
    ltype = les.get("type") or "Unknown"
    base = f"{n:03d}_{safe_name(name)}"
    print(f"    [{n:03d}] {ltype:<12} {name}")

    node = gql(_GQL_DETAIL, {"id": _content_gid(les["id"])})["node"]
    with open(content_dir / f"{base}.json", "w", encoding="utf-8") as fh:
        json.dump({"lesson": les, "detail": node}, fh, ensure_ascii=False, indent=2)

    entry: dict[str, Any] = {
        "position": n, "id": _gid_num(les["id"]), "name": name, "type": ltype,
        "raw": f"content/{base}.json", "file": None, "download_status": "no_asset",
    }

    if ltype == "Video":
        video = node.get("video") or {}
        url = video.get("url")
        orig = video.get("fileName") or (filename_from_url(url) if url else None)
        entry["video_url"] = url
        entry["video_file"] = None
        if (url or video.get("playUrl")) and not skip_videos:
            media_dir.mkdir(parents=True, exist_ok=True)
            dest = media_dir / safe_name(orig or f"{base}.mp4")
            # Try the direct URL first (public S3); fall back to Wistia (403 CDN).
            size = stream_download(url, dest, dl) if url else None
            src = url
            if size is None and video.get("playUrl"):
                alt = wistia_download_url(video["playUrl"], dl)
                if alt:
                    print("    ... not directly downloadable; fetching via Wistia")
                    size = stream_download(alt, dest, dl)
                    src = alt
            if size is not None:
                entry["video_file"] = f"_videos/{dest.name}"
                entry["download_status"] = "downloaded"
                saved_videos[src] = {"file": dest.name, "original_name": orig, "url": src}
            else:
                entry["download_status"] = "manual_required"
        elif url:
            entry["download_status"] = "manual_required"   # url known, download skipped

        # Poster thumbnail — small, always fetched (even with --no-videos) so a
        # future import can reuse it as the lesson image.
        thumb = video.get("thumbnail")
        if thumb:
            entry["thumbnail_url"] = thumb
            tdest = content_dir / f"{base}_thumbnail{guess_ext(thumb) or '.jpg'}"
            if stream_download(thumb, tdest, dl) is not None:
                entry["thumbnail_file"] = f"content/{tdest.name}"
        return entry

    # Non-video assets that expose a direct URL (PDF, audio).
    url = node.get("url")
    if url:
        ext = guess_ext(url) or Path(safe_name(name)).suffix
        dest = content_dir / (base + ext)
        if stream_download(url, dest, dl) is not None:
            entry["file"] = f"content/{dest.name}"
            entry["download_status"] = "downloaded"
        else:
            entry["download_status"] = "manual_required"
    return entry


# --------------------------------------------------------------------------- #
# FORMAT.md — written into every export so the backup is self-documenting
# --------------------------------------------------------------------------- #
FORMAT_DOC = r"""# Thinkific course export — format specification

This folder is a complete, self-contained backup of a Thinkific course site,
produced by `thinkific_export.py`. It is designed to be re-imported into another
LMS. This document describes every file and field so a human or an LLM can
reconstruct the courses without access to the original site or tool.

## 1. Folder structure

```
<work_dir>/
├── FORMAT.md                         # this file
├── <course-slug>/                    # one folder per course
│   ├── manifest.yaml                 # the course: chapters, lessons, asset links
│   └── content/
│       ├── NNN_<lesson>.json         # raw per-lesson data ({lesson, detail})
│       ├── NNN_<lesson>.pdf|.mp3      # downloaded PDF / audio asset (if any)
│       └── NNN_<lesson>_thumbnail.jpg # video poster image (if a video lesson)
├── _videos/
│   ├── <original name>.mp4           # every downloaded video (flat, de-duplicated)
│   └── videos_manifest.yaml          # flat index of downloaded videos
└── .thinkific_session.json           # saved login session (SECRET — not content)
```

`NNN` is a 3-digit sequence number (`001`, `002`, …) counting lessons across the
WHOLE course in display order, and it matches the `position` field in the manifest.
`<course-slug>` and file names are filesystem-sanitised (spaces kept; `/`, `:`,
etc. replaced with `_`), so a name in the manifest may differ slightly from its
file name — always follow the explicit `raw` / `file` / `video_file` /
`thumbnail_file` path fields rather than reconstructing names.

## 2. `manifest.yaml` (one per course)

Top-level keys: `course`, `chapters`, `summary`.

```yaml
course:
  id: 123456                       # numeric Thinkific course id
  name: Example Course Title
  slug: example-course-title
  status: published                # published | draft
  instructor: Jane Doe
  description: Short course description/subtitle from Thinkific  # or null
  image_url: https://.../course_card.png  # source URL of the course card image (omitted if none/default)
  image_file: content/course_thumbnail.png  # local path, or null if the download failed
chapters:                          # ordered list; render in array order
  - position: 0                    # chapter order within course (0-based, from Thinkific)
    id: '742860'                   # numeric chapter id
    name: 1. Introduction ...
    lessons:                       # ordered list; render in array order
      - position: 4               # lesson's 1-based sequence across the course (== NNN prefix)
        id: '2596056'             # numeric lesson (content) id
        name: Chapter 2 Quiz
        type: Quiz                # lesson type (see §5)
        raw: content/011_Chapter 2 Quiz.json   # path to full per-lesson JSON
        file: null                # path to a downloaded PDF/audio asset, or null
        download_status: no_asset # downloaded | manual_required | no_asset
        # --- the following appear only on Video lessons ---
        video_url: https://.../file.mp4        # source URL the video came from
        video_file: _videos/....mp4            # local path to the video, or null
        thumbnail_url: https://.../poster.jpg  # source URL of the poster image
        thumbnail_file: content/..._thumbnail.jpg  # local path, or null
summary:                           # counts of the three download_status values
  downloaded: 14
  manual_required: 0
  no_asset: 12
```

`download_status` meaning:
- `downloaded` — the asset (video/pdf/audio) is present locally (`file` or `video_file`).
- `manual_required` — an asset exists online and its URL is recorded, but the file
  is not local (e.g. exported with `--no-videos`, or a download failed).
- `no_asset` — the lesson has no downloadable file (Text, Quiz, Survey, …). Its
  content, if any, lives inside the `raw` JSON (e.g. `detail.htmlDescription`).

All paths in the manifest are relative to the course folder (so `content/...` and
`_videos/...`; the latter resolves to the sibling top-level `_videos/`).

## 3. Per-lesson JSON — `content/NNN_*.json`

Every lesson has one JSON file with exactly two top-level keys:

```json
{
  "lesson":  { ... },   // summary fields from the course-tree query
  "detail":  { ... }    // type-specific fields (the body/content); see §6
}
```

`lesson` object (same for all types):

| field        | type   | notes |
|--------------|--------|-------|
| `id`         | string | base64 global id, e.g. `VGV4dExlc3Nvbi0yNTk2MDU2` → `TextLesson-2596056` |
| `name`       | string | lesson title |
| `type`       | string | short type name, e.g. `Text`, `Video` (see §5) |
| `__typename` | string | GraphQL type, always `<type>Lesson`, e.g. `QuizLesson` |
| `position`   | int    | position WITHIN its chapter (0-based) |
| `draft`      | bool   | true = unpublished/hidden lesson (still exported) |
| `free`       | bool   | true = free preview lesson |

Note two different `position` values: `lesson.position` here is 0-based within the
chapter; the manifest entry's `position` is the 1-based running index across the
course. Use the manifest for global ordering, the chapter array + this for grouping.

## 4. ID scheme

Thinkific GraphQL ids are base64 of `"<Type>-<number>"`:
- `Q291cnNlLTEyMzQ1Ng==` → `Course-123456`
- `Q2hhcHRlci03NDI4NjA=` → `Chapter-742860`
- `VmlkZW9MZXNzb24tMjU0MjAzMw==` → `VideoLesson-2542033`

The manifest exposes the decoded NUMBER (`123456`, `742860`, …). The per-lesson
JSON keeps the base64 form in `lesson.id` and inside `detail`. These ids are only
meaningful on the original Thinkific site; when importing elsewhere, treat them as
opaque source references / for de-duplication.

## 5. Lesson types

The `type` value (and `__typename = type + "Lesson"`). The exporter recognises all
of these Thinkific lesson types:

`Text`, `Video`, `Audio`, `Pdf`, `Download`, `Multimedia`, `Quiz`, `Survey`,
`Exam`, `Assignment`, `Presentation`, `Disqus`, `Live`, `Canvas`.

Rich, type-specific content is captured in `detail` for: **Text, Video, Audio,
Pdf, Quiz** (see §6). All other types are captured at STRUCTURE level only —
their JSON has `detail: {"__typename": "...Lesson"}` with no body — because they
either hold no exportable file or require site-specific handling. Such lessons
still appear in the manifest (title, type, order) so the course outline is complete.

## 6. `detail` fields by lesson type

HTML fields are Froala-editor HTML, usually wrapped in `<div class="fr-view">…</div>`.

### Text  (`no_asset`)
| field | type | notes |
|-------|------|-------|
| `htmlDescription` | string | the lesson body (HTML) |

### Video  (`downloaded` / `manual_required`)
| field | type | notes |
|-------|------|-------|
| `downloadable` | bool | whether Thinkific offered a student download |
| `htmlDescription` | string\|null | optional text under the video |
| `video` | object | see below |

`video` object:
| field | type | notes |
|-------|------|-------|
| `url` | string | direct source URL. Public S3 URLs download directly; `files.cdn.thinkific.com` URLs are access-controlled (the tool falls back to Wistia to fetch these). |
| `playUrl` | string | signed player page (videoproxy) — NOT the media file |
| `fileName` | string | original upload name; used as the `_videos/` file name |
| `contentType` | string | e.g. `video/mp4` |
| `humanReadableFileSize` | string | e.g. `43.4 MB` |
| `durationInSeconds` | int | video length |
| `thumbnail` | string | poster image URL (downloaded to `*_thumbnail.jpg`) |

### Audio  (`downloaded` / `manual_required`)
| field | type | notes |
|-------|------|-------|
| `url` | string | direct MP3/M4A URL (downloaded to `content/NNN_*.mp3`) |
| `downloadable` | bool | student-download flag |
| `htmlDescription` | string\|null | optional text |

### Pdf  (`downloaded` / `manual_required`)
| field | type | notes |
|-------|------|-------|
| `url` | string | direct PDF URL (downloaded to `content/NNN_*.pdf`) |
| `downloadable` | bool | student-download flag |

### Quiz  (`no_asset`)
| field | type | notes |
|-------|------|-------|
| `passingScore` | int\|null | required score, or null if none |
| `quizType` | string | e.g. `simple` |
| `showExplanation` | bool | whether explanations are shown to students |
| `quizQuestionBank` | bool | true if drawing from a randomised bank |
| `numberOfSelectedQuizQuestions` | int\|null | how many are drawn from the bank |
| `questions` | connection | `questions.edges[].node` = a question (below) |

Question node (`questions.edges[].node`):
| field | type | notes |
|-------|------|-------|
| `id` | string | base64 `Question-<n>` |
| `name` | string | e.g. `Question #1` |
| `prompt` | string | the question text (HTML) |
| `displayType` | string | `radio` = single correct answer; `checkbox` = multiple |
| `textExplanation` | string\|null | explanation shown after answering (HTML) |
| `position` | int | order within the quiz (0-based) |
| `choices` | connection | `choices.edges[].node` = an answer option (below) |

Choice node (`choices.edges[].node`):
| field | type | notes |
|-------|------|-------|
| `id` | string | base64 `Choice-<n>` |
| `text` | string | answer text (HTML) |
| `credited` | bool | **`true` = this is a correct answer** |
| `position` | int | order within the question (0-based) |

To reconstruct a quiz: for each question, the correct answer(s) are the choices
with `credited: true` (one for `radio`, one-or-more for `checkbox`).

## 7. `_videos/videos_manifest.yaml`

A flat index of all downloaded videos (the per-course manifests are the primary
link between a lesson and its video; this is a convenience list):

```yaml
site: myschool
count: 29
videos:
  - file: course welcome.mp4            # file name inside _videos/
    original_name: course welcome.mp4
    url: https://.../file.mp4            # source it was fetched from
```

Videos are stored flat and de-duplicated by source URL, so a video reused across
lessons exists once; multiple lessons' `video_file` may point to the same file.

## 8. Re-import recipe (for another LMS)

1. For each `<course-slug>/manifest.yaml`, create a course from `course.name` /
   `status`; keep `slug`/`id` as an external reference if useful.
2. Iterate `chapters` in array order → create sections/modules from `name`.
3. Within each chapter iterate `lessons` in array order → create a lesson of the
   mapped `type`. Global order is also given by each lesson's `position` / `NNN`.
4. Pull the body from the lesson's `raw` JSON `detail`:
   - Text: `detail.htmlDescription`.
   - Video: upload `video_file`; poster = `thumbnail_file`; caption = `detail.htmlDescription`.
   - Audio / Pdf: upload the manifest `file`; caption = `detail.htmlDescription` (audio).
   - Quiz: build from `detail.questions` (correct = choices with `credited: true`).
   - Other types: only title/type/order are available (structure-only, §5).
5. HTML bodies are Froala HTML; the `<div class="fr-view">` wrapper can be kept or
   stripped. Rewrite any absolute Thinkific asset URLs to your new host as needed.

## 9. Known limitations

- Structure-only lesson types (§5) have no captured body.
- `draft: true` lessons are included; filter them if you only want published content.
- Videos not attached to any lesson (old/unused uploads in the Thinkific video
  library) are intentionally NOT exported — only videos referenced by a lesson.
- Quizzes are captured in full: Thinkific supports only `radio` (single-answer)
  and `checkbox` (multiple-answer) questions, and the export includes every field
  the schema exposes for quizzes, questions and choices — there is no per-choice
  feedback field in Thinkific to capture. However, `Survey` and `Exam` lessons can
  also contain questions and are exported at STRUCTURE level only (§5), so their
  questions/answers are NOT captured.
"""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Export a Thinkific course site (structure + videos/PDFs/audio) "
                    "via the admin GraphQL API, using your logged-in browser session.")
    ap.add_argument("--subdomain", default=None,
                    help="Thinkific subdomain, e.g. 'myschool' for myschool.thinkific.com "
                         "(required; or set THINKIFIC_SUBDOMAIN).")
    ap.add_argument("--work-dir", default=None, help="Output directory (default: ./thinkific_export).")
    ap.add_argument("--login", action="store_true", help="Force an interactive re-login first.")
    ap.add_argument("--course", type=int, default=None, help="Export only this numeric course id.")
    ap.add_argument("--no-videos", action="store_true",
                    help="Export structure + PDFs/audio and record video URLs, but skip video files.")
    args = ap.parse_args()

    cfg = Config.load(args.subdomain, args.work_dir)
    if args.login or not cfg.state_path.exists():
        login(cfg)
    export(cfg, only_course=args.course, skip_videos=args.no_videos)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
frappe_import.py — import a Thinkific export into Frappe LMS
===========================================================

Takes a folder produced by ``thinkific_export.py`` and recreates one of its
courses inside a running **Frappe LMS** instance (https://github.com/frappe/lms),
using Frappe's REST API: it creates the ``LMS Course``, its ``Course Chapter``s
and ``Course Lesson``s, uploads the videos / PDFs / audio, and rebuilds quizzes
(``LMS Quiz`` + ``LMS Question``).

Two things make it safe to re-run:

* **Resume / start-from-existing.** Courses, chapters and lessons are matched by
  title. Anything that already exists (including a course you created by hand in
  Frappe first) is reused, and only the missing pieces are created. So a run that
  dies half-way — or a course you set up manually and want filled in — just
  continues from where it is. Existing lessons are skipped unless
  ``--update-existing`` is given.
* **Ordering is rebuilt from the manifest** every run (the ``Chapter Reference`` /
  ``Lesson Reference`` child tables), so chapters and lessons end up in the same
  order as on Thinkific regardless of what already existed.

Video handling has three modes (``--video-mode``):

* ``frappe`` (default) — upload the MP4 to Frappe and embed it with the
  ``{{ Video("...") }}`` lesson macro.
* ``vimeo`` — upload the MP4 to a **Vimeo** account as a non-public video and
  embed the Vimeo player URL instead (keeps large media off the LMS server).
* ``link`` — do not upload anything; embed the source ``video_url`` from the
  manifest as-is (useful for a quick structural import).

Auth is an API key + secret generated on a Frappe User (Settings → API Access).

Setup:  pip install requests pyyaml            # + PyVimeo only for --video-mode vimeo
        (or, in this repo:  uv sync  /  uv sync --extra vimeo)
Usage:
  python frappe_import.py --export-dir ./thinkific_export --course my-course \
      --frappe-url https://lms.example.com
  # credentials via env: FRAPPE_URL, FRAPPE_API_KEY, FRAPPE_API_SECRET
  python frappe_import.py --export-dir ./out --course my-course --video-mode vimeo
  python frappe_import.py --export-dir ./out --course my-course --dry-run
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

try:  # optional — only needed for --video-mode vimeo
    import vimeo  # type: ignore
except ImportError:
    vimeo = None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    frappe_url: str
    api_key: str
    api_secret: str

    @staticmethod
    def load(url: Optional[str], api_key: Optional[str],
             api_secret: Optional[str]) -> "Config":
        url = url or os.getenv("FRAPPE_URL")
        key = api_key or os.getenv("FRAPPE_API_KEY")
        secret = api_secret or os.getenv("FRAPPE_API_SECRET")
        missing = [n for n, v in (("--frappe-url/FRAPPE_URL", url),
                                  ("FRAPPE_API_KEY", key),
                                  ("FRAPPE_API_SECRET", secret)) if not v]
        if missing:
            sys.exit("Missing Frappe credentials: " + ", ".join(missing) +
                     ".\nGenerate an API key/secret on a Frappe User "
                     "(Settings > API Access) and set FRAPPE_API_KEY / "
                     "FRAPPE_API_SECRET.")
        return Config(frappe_url=url.rstrip("/"), api_key=key, api_secret=secret)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
_TAGS = re.compile(r"<[^>]+>")


def strip_html(s: Optional[str]) -> str:
    """Plain text from an HTML fragment (for fields that aren't rich text)."""
    if not s:
        return ""
    txt = _TAGS.sub("", s)
    return re.sub(r"\s+", " ", txt).strip()


def macro(name: str, arg: str) -> str:
    """A Frappe LMS markdown macro, e.g. Video("/files/x.mp4")."""
    return f'{{{{ {name}("{arg}") }}}}'


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


# --------------------------------------------------------------------------- #
# Frappe REST client (thin wrapper over requests)
# --------------------------------------------------------------------------- #
class Frappe:
    """Minimal Frappe REST client using API key/secret token auth.

    Everything the importer needs: list/get/create/update documents and upload
    files. In ``dry_run`` mode nothing is written — creates/updates/uploads are
    logged and return placeholder values so the walk still completes.
    """

    def __init__(self, cfg: Config, dry_run: bool = False):
        self.base = cfg.frappe_url
        self.dry_run = dry_run
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"token {cfg.api_key}:{cfg.api_secret}"
        self.s.headers["Accept"] = "application/json"

    # -- low level ---------------------------------------------------------- #
    def _req(self, method: str, path: str, **kw) -> Any:
        r = self.s.request(method, self.base + path, timeout=120, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Frappe {method} {path} -> HTTP {r.status_code}: "
                               f"{r.text[:400]}")
        return r.json() if r.content else {}

    # -- documents ---------------------------------------------------------- #
    def get_list(self, doctype: str, filters: Optional[dict] = None,
                 fields: Optional[list[str]] = None) -> list[dict]:
        if self.dry_run:  # pretend nothing exists so the whole walk is exercised
            return []
        params = {"limit_page_length": 0}
        if filters:
            params["filters"] = json.dumps(filters)
        params["fields"] = json.dumps(fields or ["name"])
        path = f"/api/resource/{urllib.parse.quote(doctype)}?" + \
               urllib.parse.urlencode(params)
        return self._req("GET", path).get("data", [])

    def get_doc(self, doctype: str, name: str) -> dict:
        path = f"/api/resource/{urllib.parse.quote(doctype)}/" \
               f"{urllib.parse.quote(name, safe='')}"
        return self._req("GET", path).get("data", {})

    def create(self, doctype: str, doc: dict) -> str:
        if self.dry_run:
            title = doc.get("title") or doc.get("name") or "?"
            print(f"      [dry-run] create {doctype}: {title}")
            return f"DRYRUN-{doctype}-{title}"
        data = self._req("POST", f"/api/resource/{urllib.parse.quote(doctype)}",
                         json=doc).get("data", {})
        return data["name"]

    def update(self, doctype: str, name: str, doc: dict) -> None:
        if self.dry_run:
            print(f"      [dry-run] update {doctype}/{name}: {list(doc)}")
            return
        path = f"/api/resource/{urllib.parse.quote(doctype)}/" \
               f"{urllib.parse.quote(name, safe='')}"
        self._req("PUT", path, json=doc)

    def logged_user(self) -> Optional[str]:
        if self.dry_run:
            return "dry-run@example.com"
        try:
            return self._req("GET", "/api/method/frappe.auth.get_logged_user") \
                .get("message")
        except RuntimeError:
            return None

    # -- files -------------------------------------------------------------- #
    def upload_file(self, path: Path, is_private: bool = False,
                    doctype: Optional[str] = None, docname: Optional[str] = None,
                    fieldname: Optional[str] = None) -> str:
        """Upload a local file; return its ``file_url`` (e.g. ``/files/x.mp4``)."""
        if self.dry_run:
            print(f"      [dry-run] upload {path.name} ({path.stat().st_size/1e6:.1f} MB)")
            return f"/files/{path.name}"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = {"is_private": 1 if is_private else 0, "folder": "Home"}
        if doctype and docname:
            data.update(doctype=doctype, docname=docname, fieldname=fieldname or "")
        with open(path, "rb") as fh:
            r = self.s.post(self.base + "/api/method/upload_file",
                            files={"file": (path.name, fh, mime)},
                            data=data, timeout=1800)
        if r.status_code >= 400:
            raise RuntimeError(f"upload_file {path.name} -> HTTP {r.status_code}: "
                               f"{r.text[:300]}")
        return r.json()["message"]["file_url"]


# --------------------------------------------------------------------------- #
# Vimeo uploader (optional — non-public, embeddable videos)
# --------------------------------------------------------------------------- #
class VimeoUploader:
    """Upload a local MP4 to Vimeo as a non-public, embeddable video.

    ``view='disable'`` hides the video from vimeo.com entirely (embed-only) and
    ``view='unlisted'`` means anyone-with-the-link; both keep it out of search.
    ``disable`` + ``embed='whitelist'`` (the locked-down combination) needs a paid
    Vimeo plan. We poll until transcoding finishes so the embed actually plays.
    """

    def __init__(self, token: str, key: Optional[str], secret: Optional[str],
                 view: str, embed: str, domain: Optional[str],
                 dry_run: bool = False):
        if vimeo is None:
            sys.exit("PyVimeo not installed. Run: pip install PyVimeo "
                     "(or: uv sync --extra vimeo)")
        self.view, self.embed, self.domain = view, embed, domain
        self.dry_run = dry_run
        self.client = vimeo.VimeoClient(token=token, key=key, secret=secret)

    def upload(self, path: Path, name: str, description: str = "") -> Optional[dict]:
        """Return {id, page_url, embed_url} or None on failure."""
        if self.dry_run:
            print(f"      [dry-run] vimeo upload {path.name} (view={self.view})")
            return {"id": "0", "page_url": "https://vimeo.com/0",
                    "embed_url": "https://player.vimeo.com/video/0"}
        data = {"name": name, "description": description,
                "privacy": {"view": self.view, "embed": self.embed,
                            "download": False, "add": False}}
        try:
            uri = self.client.upload(str(path), data=data)  # -> "/videos/<id>"
        except Exception as exc:  # noqa: BLE001 — surface, fall back to manual
            print(f"      x vimeo upload failed: {exc}")
            return None
        vid = uri.rstrip("/").split("/")[-1]
        if self.embed == "whitelist" and self.domain:
            try:
                self.client.put(f"/videos/{vid}/privacy/domains/{self.domain}")
            except Exception as exc:  # noqa: BLE001
                print(f"      ! could not whitelist {self.domain}: {exc}")
        return {"id": vid,
                "page_url": self._poll(uri, vid),
                "embed_url": f"https://player.vimeo.com/video/{vid}"}

    def _poll(self, uri: str, vid: str, timeout: int = 900) -> str:
        """Wait for transcoding; return the canonical page link."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                body = self.client.get(
                    uri, params={"fields": "transcode.status,link"}).json()
            except Exception:  # noqa: BLE001
                break
            status = (body.get("transcode") or {}).get("status")
            if status == "complete":
                return body.get("link") or f"https://vimeo.com/{vid}"
            if status == "error":
                print("      ! vimeo transcode reported error")
                break
            print("      ... waiting for Vimeo to finish transcoding")
            time.sleep(20)
        return f"https://vimeo.com/{vid}"


# --------------------------------------------------------------------------- #
# Export reader
# --------------------------------------------------------------------------- #
def all_course_dirs(export_dir: Path) -> list[tuple[Path, dict]]:
    """Every course folder under the export root, in folder-name order."""
    out: list[tuple[Path, dict]] = []
    for mf in sorted(export_dir.glob("*/manifest.yaml")):
        out.append((mf.parent, _load_yaml(mf)))
    if not out:
        sys.exit(f"No courses (no */manifest.yaml) found under {export_dir}.")
    return out


def resolve_course_dir(export_dir: Path, course: str) -> tuple[Path, dict]:
    """Find the course folder whose manifest matches `course` (folder/slug/name)."""
    direct = export_dir / course / "manifest.yaml"
    if direct.exists():
        return direct.parent, _load_yaml(direct)
    hits: list[tuple[Path, dict]] = []
    for mf in sorted(export_dir.glob("*/manifest.yaml")):
        man = _load_yaml(mf)
        c = man.get("course", {})
        if course in (c.get("slug"), str(c.get("id")), c.get("name"), mf.parent.name):
            hits.append((mf.parent, man))
    if not hits:
        avail = ", ".join(sorted(p.name for p in export_dir.glob("*")
                                 if (p / "manifest.yaml").exists())) or "(none)"
        sys.exit(f"No course matching '{course}' under {export_dir}.\n"
                 f"Available: {avail}")
    if len(hits) > 1:
        sys.exit(f"'{course}' matches multiple courses: "
                 + ", ".join(p.name for p, _ in hits))
    return hits[0]


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_detail(course_dir: Path, lesson: dict) -> dict:
    """The per-lesson raw JSON's `detail` object (body/quiz/asset content)."""
    raw = lesson.get("raw")
    if not raw:
        return {}
    p = course_dir / raw
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as fh:
        return (json.load(fh) or {}).get("detail") or {}


def asset_path(course_dir: Path, rel: str) -> Optional[Path]:
    """Resolve a manifest asset path (content/... under the course, _videos/...
    under the export root) to a local file, or None if it isn't present."""
    for cand in (course_dir / rel, course_dir.parent / rel):
        if cand.exists() and cand.stat().st_size > 0:
            return cand
    return None


# --------------------------------------------------------------------------- #
# Importer
# --------------------------------------------------------------------------- #
class Importer:
    def __init__(self, fr: Frappe, course_dir: Path, manifest: dict,
                 opts: argparse.Namespace, vimeo_up: Optional[VimeoUploader],
                 instructor: str):
        self.fr = fr
        self.dir = course_dir
        self.man = manifest
        self.opts = opts
        self.vimeo = vimeo_up
        self.instructor = instructor

    # -- lookup-or-create --------------------------------------------------- #
    def _find(self, doctype: str, filters: dict) -> Optional[str]:
        rows = self.fr.get_list(doctype, filters=filters)
        return rows[0]["name"] if rows else None

    def run(self) -> None:
        course_name = self._ensure_course()
        ch_names: list[str] = []
        for chapter in self.man.get("chapters", []):
            ch_names.append(self._ensure_chapter(course_name, chapter))
        # Rebuild the course's ordered chapter list from the manifest.
        self._ensure_order("LMS Course", course_name, "chapters", "chapter", ch_names)
        s = self.man.get("summary", {})
        print(f"\nDone. Course '{course_name}' — "
              f"{len(ch_names)} chapter(s); "
              f"source summary downloaded={s.get('downloaded', 0)} "
              f"manual_required={s.get('manual_required', 0)} "
              f"no_asset={s.get('no_asset', 0)}.")
        if self.fr.dry_run:
            print("(dry-run — nothing was written to Frappe.)")

    def _ensure_course(self) -> str:
        c = self.man.get("course", {})
        title = c.get("name") or c.get("slug") or "Imported course"
        existing = self._find("LMS Course", {"title": title})
        if existing:
            print(f"> Course '{title}' already exists ({existing}) — reusing.")
            if self.opts.publish:
                self.fr.update("LMS Course", existing, {"published": 1})
            return existing
        print(f"> Creating course '{title}'")
        doc = {
            "title": title,
            "short_introduction": (strip_html(c.get("name")) or title)[:140],
            "description": f"<p>Imported from Thinkific "
                           f"(slug: {c.get('slug', '')}).</p>",
            "published": 1 if self.opts.publish else 0,
            "instructors": [{"instructor": self.instructor}],
        }
        return self.fr.create("LMS Course", doc)

    def _ensure_chapter(self, course_name: str, chapter: dict) -> str:
        title = chapter.get("name") or f"Chapter {chapter.get('position', '')}"
        existing = self._find("Course Chapter",
                              {"course": course_name, "title": title})
        if existing:
            print(f"  = Chapter '{title}' exists ({existing}) — reusing.")
            ch_name = existing
        else:
            print(f"  + Chapter '{title}'")
            ch_name = self.fr.create("Course Chapter",
                                     {"title": title, "course": course_name})
        les_names: list[str] = []
        for lesson in chapter.get("lessons", []):
            name = self._ensure_lesson(course_name, ch_name, lesson)
            if name:
                les_names.append(name)
        self._ensure_order("Course Chapter", ch_name, "lessons", "lesson", les_names)
        return ch_name

    def _ensure_lesson(self, course_name: str, ch_name: str,
                       lesson: dict) -> Optional[str]:
        title = lesson.get("name") or f"Lesson {lesson.get('position', '')}"
        existing = self._find("Course Lesson",
                              {"chapter": ch_name, "title": title})
        if existing and not self.opts.update_existing:
            print(f"    . Lesson '{title}' exists — skipping.")
            return existing
        detail = load_detail(self.dir, lesson)
        body, quiz_id = self._build_body(course_name, lesson, detail)
        doc: dict[str, Any] = {"title": title, "chapter": ch_name,
                               "include_in_preview": 1 if lesson.get("free") else 0,
                               "body": body}
        if quiz_id:
            doc["quiz_id"] = quiz_id
        if existing:
            print(f"    ~ Updating lesson '{title}'")
            self.fr.update("Course Lesson", existing, doc)
            return existing
        print(f"    + Lesson '{title}'  [{lesson.get('type')}]")
        return self.fr.create("Course Lesson", doc)

    # -- lesson body assembly ---------------------------------------------- #
    def _build_body(self, course_name: str, lesson: dict,
                    detail: dict) -> tuple[str, Optional[str]]:
        """Return (markdown body, quiz_id-or-None) for a lesson."""
        ltype = lesson.get("type")
        parts: list[str] = []
        quiz_id: Optional[str] = None

        if ltype == "Video":
            embed = self._video_embed(lesson)
            if embed:
                parts.append(embed)
        elif ltype == "Audio":
            fu = self._upload_asset(lesson.get("file"))
            if fu:
                parts.append(macro("Audio", fu))
        elif ltype == "Pdf":
            fu = self._upload_asset(lesson.get("file"))
            if fu:
                parts.append(macro("PDF", fu))
        elif ltype == "Quiz":
            quiz_id = self._build_quiz(lesson, detail)

        html = detail.get("htmlDescription")
        if html:
            parts.append(html)
        if not parts:
            parts.append(f"<!-- Imported from Thinkific: {ltype} lesson "
                         f"(no capturable body). -->")
        return "\n\n".join(parts), quiz_id

    def _video_embed(self, lesson: dict) -> Optional[str]:
        mode = self.opts.video_mode
        title = lesson.get("name") or "Video"
        if mode == "link":
            url = lesson.get("video_url")
            return macro("Video", url) if url else None
        local = self._local_video(lesson)
        if mode == "vimeo":
            if not local:
                print("      ! no local video file; recording source URL only")
                url = lesson.get("video_url")
                return macro("Video", url) if url else None
            info = self.vimeo.upload(local, title,
                                     strip_html(lesson.get("name")))
            if info:
                return macro("Embed", f"{title}|||{info['embed_url']}")
            return None
        # mode == "frappe": upload the file to the LMS.
        if not local:
            print("      ! no local video file; recording source URL only")
            url = lesson.get("video_url")
            return macro("Video", url) if url else None
        fu = self.fr.upload_file(local, is_private=self.opts.private_files)
        return macro("Video", fu)

    def _local_video(self, lesson: dict) -> Optional[Path]:
        vf = lesson.get("video_file")
        return asset_path(self.dir, vf) if vf else None

    def _upload_asset(self, rel: Optional[str]) -> Optional[str]:
        if not rel:
            return None
        p = asset_path(self.dir, rel)
        if not p:
            print(f"      ! asset missing on disk: {rel}")
            return None
        return self.fr.upload_file(p, is_private=self.opts.private_files)

    # -- quizzes ------------------------------------------------------------ #
    def _build_quiz(self, lesson: dict, detail: dict) -> Optional[str]:
        questions = (((detail.get("questions") or {}).get("edges")) or [])
        if not questions:
            return None
        q_rows: list[dict] = []
        for qe in questions:
            qn = qe.get("node") or {}
            choices = [ce.get("node") or {}
                       for ce in (((qn.get("choices") or {}).get("edges")) or [])]
            correct = sum(1 for c in choices if c.get("credited"))
            qdoc: dict[str, Any] = {
                "question": qn.get("prompt") or qn.get("name") or "Question",
                "type": "Choices",
                "multiple": 1 if correct > 1 else 0,
            }
            for i, c in enumerate(choices[:10], 1):
                qdoc[f"option_{i}"] = strip_html(c.get("text"))
                qdoc[f"is_correct_{i}"] = 1 if c.get("credited") else 0
            q_name = self.fr.create("LMS Question", qdoc)
            q_rows.append({"question": q_name, "marks": 1})
        title = f"{lesson.get('name') or 'Quiz'}"
        quiz_doc = {
            "title": title,
            "passing_percentage": detail.get("passingScore") or 60,
            "questions": q_rows,
        }
        quiz_name = self.fr.create("LMS Quiz", quiz_doc)
        print(f"      quiz '{title}' with {len(q_rows)} question(s)")
        return quiz_name

    # -- child-table ordering ---------------------------------------------- #
    def _ensure_order(self, parent_doctype: str, parent_name: str,
                      table: str, link_field: str, ordered: list[str]) -> None:
        """Make the parent's child table list `ordered` first (manifest order),
        preserving any extra existing rows after them."""
        if self.fr.dry_run or not ordered:
            return
        doc = self.fr.get_doc(parent_doctype, parent_name)
        existing = [row.get(link_field) for row in (doc.get(table) or [])]
        extra = [n for n in existing if n not in ordered]
        desired = ordered + extra
        if desired != existing:
            self.fr.update(parent_doctype, parent_name,
                           {table: [{link_field: n} for n in desired]})


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Import a Thinkific export (from thinkific_export.py) into a "
                    "Frappe LMS instance via its REST API.")
    ap.add_argument("--export-dir", required=True,
                    help="The export root (the thinkific_export.py --work-dir).")
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--course",
                     help="Which course to import: its folder name, slug, numeric "
                          "id, or exact title.")
    sel.add_argument("--all", action="store_true", dest="all_courses",
                     help="Import every course found under --export-dir.")
    ap.add_argument("--frappe-url", default=None,
                    help="Frappe site URL, e.g. https://lms.example.com "
                         "(or set FRAPPE_URL).")
    ap.add_argument("--api-key", default=None, help="Frappe API key (or FRAPPE_API_KEY).")
    ap.add_argument("--api-secret", default=None,
                    help="Frappe API secret (or FRAPPE_API_SECRET).")
    ap.add_argument("--instructor", default=None,
                    help="User (email) to set as course instructor. Defaults to "
                         "FRAPPE_INSTRUCTOR, else the API key's own user.")
    ap.add_argument("--video-mode", choices=["frappe", "vimeo", "link"],
                    default="frappe",
                    help="frappe: upload MP4s to the LMS (default). vimeo: upload "
                         "to Vimeo (non-public) and embed. link: embed the source "
                         "URL, upload nothing.")
    ap.add_argument("--publish", action="store_true",
                    help="Mark the created course as published (default: draft).")
    ap.add_argument("--private-files", action="store_true",
                    help="Upload assets as private Frappe files (default: public).")
    ap.add_argument("--update-existing", action="store_true",
                    help="Re-process lessons that already exist (default: skip them).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be created; write nothing.")
    # Vimeo options (only used with --video-mode vimeo).
    ap.add_argument("--vimeo-view", default="unlisted",
                    choices=["unlisted", "disable", "nobody", "anybody"],
                    help="Vimeo privacy 'view' (default: unlisted). 'disable' = "
                         "hidden from vimeo.com, embed-only (needs a paid plan).")
    ap.add_argument("--vimeo-embed", default="public",
                    choices=["public", "whitelist", "private"],
                    help="Vimeo embed policy (default: public). 'whitelist' limits "
                         "embedding to --vimeo-domain (needs a paid plan).")
    ap.add_argument("--vimeo-domain", default=None,
                    help="Domain to whitelist for embedding (default: the Frappe "
                         "site's host). Only used with --vimeo-embed whitelist.")
    args = ap.parse_args()

    export_dir = Path(args.export_dir).expanduser()
    if not export_dir.is_dir():
        sys.exit(f"Export dir not found: {export_dir}")

    cfg = Config.load(args.frappe_url, args.api_key, args.api_secret)
    fr = Frappe(cfg, dry_run=args.dry_run)

    instructor = args.instructor or os.getenv("FRAPPE_INSTRUCTOR") \
        or fr.logged_user()
    if not instructor and not args.dry_run:
        sys.exit("Could not determine an instructor. Pass --instructor <email> "
                 "or set FRAPPE_INSTRUCTOR.")

    vimeo_up = None
    if args.video_mode == "vimeo":
        token = os.getenv("VIMEO_ACCESS_TOKEN")
        if not token and not args.dry_run:
            sys.exit("--video-mode vimeo needs VIMEO_ACCESS_TOKEN "
                     "(scopes: public private edit upload).")
        vimeo_up = VimeoUploader(
            token or "dry", os.getenv("VIMEO_CLIENT_ID"),
            os.getenv("VIMEO_CLIENT_SECRET"),
            view=args.vimeo_view, embed=args.vimeo_embed,
            domain=args.vimeo_domain or host_of(cfg.frappe_url),
            dry_run=args.dry_run)

    if args.all_courses:
        courses = all_course_dirs(export_dir)
        print(f"Importing {len(courses)} course(s) from {export_dir} "
              f"-> {cfg.frappe_url}")
    else:
        courses = [resolve_course_dir(export_dir, args.course)]

    for i, (course_dir, manifest) in enumerate(courses, 1):
        label = f"[{i}/{len(courses)}] " if len(courses) > 1 else ""
        print(f"\n{label}Importing {course_dir}/manifest.yaml -> {cfg.frappe_url}\n")
        Importer(fr, course_dir, manifest, args, vimeo_up, instructor or "").run()


if __name__ == "__main__":
    main()

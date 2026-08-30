"""
ingest.py  —  v2: the multi-source knowledge base

What this does:
  Reads ALL your course material from a 'materials' folder and produces two files:

    knowledge.json  -- everything students' answers may be based on:
                         slides       -> cited as (Lecture 9, Slide 4)        [tier 1]
                         transcripts  -> cited as (Lecture 9 Video, ~12:30)   [tier 1]
                         textbook     -> cited as (Textbook, p. 213)          [tier 1]
                         instructor answers -> (Instructor answer, <date>)    [tier 2]

    solutions.json  -- homework solutions. PRIVATE. These go into a separate
                       database that the student-facing side cannot search.
                       They are only used internally so Socratic mode can
                       steer students toward the right answer and verify
                       their work -- never to reveal it.

Folder layout it expects (create these inside your project folder):

    materials/
      slides/                <- your .pptx / .pdf lecture decks (move them here)
      transcripts/           <- lecture video transcripts: .vtt, .srt, or .txt
      textbook/              <- textbook chapters as .pdf or .txt
      homework_solutions/    <- solution files: .pdf or .txt, named like HW4_solutions.pdf

  Plus, optionally, a file named instructor_answers.json in the project folder
  (the dashboard will create and grow this file in a later step).

Run it with:   python ingest.py
Then rebuild the search index with:   python build_index.py
"""

import json
import re
from pathlib import Path

from pptx import Presentation
from pypdf import PdfReader

MATERIALS_DIR = Path("materials")
KNOWLEDGE_FILE = Path("knowledge.json")
SOLUTIONS_FILE = Path("solutions.json")
INSTRUCTOR_ANSWERS_FILE = Path("instructor_answers.json")
PARSE_CACHE_FILE = Path("parse_cache.json")


# ── Incremental parse cache ───────────────────────────────────────────────────
# Text extraction from PDFs/PPTX is the slow part of ingestion. This cache lets
# an "Update index" run skip files that haven't changed since the last ingest:
# for each source file we remember its size + modification time and the chunks
# it produced. If a file's signature is unchanged, we reuse its chunks instead of
# re-reading and re-chunking it. Only new or modified files are parsed.
class ParseCache:
    def __init__(self, path=PARSE_CACHE_FILE, enabled=True):
        self.path = Path(path)
        self.enabled = enabled
        self.old = {}          # source_file -> {"sig":..., "knowledge":[...], "solutions":[...]}
        self.new = {}          # rebuilt this run
        if enabled and self.path.exists():
            try:
                txt = self.path.read_text(encoding="utf-8").strip()
                self.old = json.loads(txt) if txt else {}
            except Exception:
                self.old = {}

    @staticmethod
    def _sig(file_path: Path):
        try:
            st = file_path.stat()
            return f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            return None

    def get(self, file_path: Path, kind: str):
        """Return cached chunks for this file if its signature is unchanged.
        kind is 'knowledge' or 'solutions'. Returns None on a miss.

        On a hit, the entry is carried forward into the new cache so it persists
        across runs — otherwise a run where everything is a cache hit would save
        an empty cache and the next run would re-parse everything."""
        if not self.enabled:
            return None
        key = file_path.name
        entry = self.old.get(key)
        if entry and entry.get("sig") == self._sig(file_path):
            # carry the whole entry forward so it survives into the next run
            self.new[key] = entry
            return entry.get(kind, [])
        return None

    def put(self, file_path: Path, kind: str, chunks: list):
        """Record the chunks a file produced, tagged with its current signature."""
        key = file_path.name
        rec = self.new.setdefault(key, {"sig": self._sig(file_path),
                                        "knowledge": [], "solutions": []})
        rec[kind] = chunks

    def save(self):
        if self.enabled:
            self.path.write_text(json.dumps(self.new, ensure_ascii=False), encoding="utf-8")

# ── Year handling (dynamic, from the system calendar) ─────────────────────────
# The current year is ALWAYS today's calendar year — never hardcoded, so the
# instructor never has to update a constant each semester. A process left running
# across New Year's Eve will correctly see the new year because current_year() is
# recomputed on each call.
#
# How far the upload dropdown reaches past and future. Wide by design so the
# instructor can archive old years and pre-stage future ones. These bounds only
# affect which years the UI offers and which existing folders get scanned; they
# don't force any folder to exist.
YEARS_BACK    = 8    # e.g. 2018 if current year is 2026
YEARS_FORWARD = 3    # e.g. 2029 if current year is 2026

import datetime as _dt


def current_year() -> int:
    """Today's calendar year. Single source of truth for 'the current year'."""
    return _dt.date.today().year


def supported_years() -> list:
    """The full span of years the UI offers, newest first. Includes future years
    (for pre-staging) and past years (for archives)."""
    cy = current_year()
    return list(range(cy + YEARS_FORWARD, cy - YEARS_BACK - 1, -1))


def scannable_years() -> list:
    """Years to actually scan during ingest: every year that has a folder on disk,
    from the whole supported span. Ascending so newest-year dedup works via the
    reverse-sorted iteration in _year_dirs()."""
    return sorted(y for y in supported_years() if (MATERIALS_DIR / str(y)).exists())


# Backward-compatible module-level names. These are recomputed at import time.
# Code that needs a live value should call current_year(); these constants exist
# so existing imports keep working, and are refreshed by _refresh_year_globals().
CURRENT_YEAR = current_year()
YEARS_SUPPORTED = supported_years()


def _refresh_year_globals():
    """Refresh the module-level CURRENT_YEAR / YEARS_SUPPORTED from today's date.
    Called at the top of main() so a long-running process stays accurate."""
    global CURRENT_YEAR, YEARS_SUPPORTED
    CURRENT_YEAR = current_year()
    YEARS_SUPPORTED = supported_years()


# How big each searchable chunk of text should be, in characters.
CHUNK_TARGET = 1100


# ── Standard naming convention ────────────────────────────────────────────────
# The module ONLY indexes files that follow these patterns. This lets the
# instructor keep original downloaded files (e.g. ccn2025-lecture1.pptx) sitting
# alongside the standardised copies the module saves on upload
# (Lecture_01_slides.pptx) WITHOUT double-indexing: the originals are simply
# ignored everywhere — ingestion, counts, and the guardrail all read the same
# standard-only set, so they stay perfectly consistent.
#
# These MUST match the patterns the upload flow generates and the materials
# browser displays (in instructor_dashboard.py).
STANDARD_NAME_PATTERNS = {
    "slides":             re.compile(r"^Lecture_\d{2}_slides\.(pptx|pdf)$", re.IGNORECASE),
    "transcripts":        re.compile(r"^Lecture_\d{2}(_part\d+)?_transcript\.(vtt|srt|txt)$", re.IGNORECASE),
    "handwritten_notes":  re.compile(r"^Lecture_\d{2}_notes\.(pdf|pptx)$", re.IGNORECASE),
    "homework_problems":  re.compile(r"^HW\d+_problems\.(pdf|txt)$", re.IGNORECASE),
    "homework_solutions": re.compile(r"^HW\d+_solutions\.(pdf|txt)$", re.IGNORECASE),
    "textbook":           re.compile(r"^Textbook(_[^.]+)?\.(pdf|txt)$", re.IGNORECASE),
}


def is_standard_name(category: str, filename: str) -> bool:
    """True if filename follows the standard naming convention for its category.
    Non-standard files (original downloads, ad-hoc names) are ignored by every
    part of the pipeline."""
    pat = STANDARD_NAME_PATTERNS.get(category)
    return bool(pat and pat.match(filename))


def _standard_files(folder: Path, category: str):
    """Yield only the files in `folder` whose names match the standard
    convention for `category`, sorted. Everything else is skipped."""
    if not folder.exists():
        return []
    return [p for p in sorted(folder.iterdir())
            if p.is_file() and is_standard_name(category, p.name)]


# ----------------------------- small helpers -----------------------------

def lecture_number_from_filename(filename):
    """Find a lecture number in a filename, or None if there isn't one."""
    match = re.search(r"(?:lecture|lec|l)\s*_?\s*(\d+)", filename, re.IGNORECASE)
    return int(match.group(1)) if match else None


def hw_id_from_filename(filename):
    """'HW4_solutions.pdf' -> 'HW4'. Falls back to the bare filename."""
    match = re.search(r"(?:hw|homework)\s*_?\s*(\d+)", filename, re.IGNORECASE)
    return f"HW{match.group(1)}" if match else Path(filename).stem


def chunk_text(text, target=CHUNK_TARGET):
    """Split long text into chunks of roughly `target` characters,
    breaking at sentence ends so chunks stay readable."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target:
            chunks.append(current.strip())
            current = sentence
        else:
            current = (current + " " + sentence).strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


def read_pptx(path):
    """Return a list of (slide_number, text) for one PowerPoint file.
    Captures text boxes, TABLE cells, and the slide's SPEAKER NOTES -- the
    previous version read only text boxes, so tables (routing tables,
    comparison charts -- common in networking slides) and the instructor's
    notes were silently lost."""
    presentation = Presentation(path)
    results = []
    for slide_number, slide in enumerate(presentation.slides, start=1):
        pieces = []
        for shape in slide.shapes:
            # plain text boxes
            if shape.has_text_frame and shape.text_frame.text.strip():
                pieces.append(shape.text_frame.text.strip())
            # tables: read every cell, row by row
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        pieces.append(" | ".join(cells))
        # speaker notes (if any)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                pieces.append(f"[Speaker notes] {notes}")
        if pieces:
            results.append((slide_number, "\n".join(pieces)))
    return results


def read_pdf_pages(path, report=None):
    """Return a list of (page_number, text) for one PDF file.

    Faithful text extraction via pypdf is used first (exact characters, exact
    page numbers -- this is why citations stay accurate). If a page has NO
    extractable text (a scanned/image-only page), we don't silently drop it:
    we try OCR as a fallback, and if OCR isn't available we record it in
    `report` (a dict) so the caller can surface the problem in the UI."""
    reader = PdfReader(path)
    results = []
    empty_pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            results.append((page_number, text))
        else:
            ocr_text = _ocr_pdf_page(path, page_number - 1)
            if ocr_text:
                results.append((page_number, ocr_text))
                print(f"      (page {page_number} had no text layer -- recovered via OCR)")
            else:
                empty_pages.append(page_number)
    if empty_pages:
        print(f"      WARNING: {path.name} pages {empty_pages} have no extractable text "
              f"and OCR was unavailable/empty -- that content is NOT in the knowledge base. "
              f"To capture it, install OCR (pip install pymupdf pytesseract pillow, plus the "
              f"Tesseract engine) or replace those pages with a text-based version.")
        if report is not None:
            report.setdefault("no_text", []).append({
                "file": path.name,
                "pages": empty_pages,
                "total_pages": len(reader.pages),
            })
    return results


def _ocr_pdf_page(path, page_index):
    """OCR a single PDF page (0-indexed). Returns extracted text, or '' if all
    methods fail. Only ever called for pages with no text layer.

    Tries in order:
      1. Tesseract (via PyMuPDF + pytesseract) — most accurate for printed text
      2. LLM vision fallback (via llm_client) — useful when Tesseract isn't
         installed, e.g. for handwritten notes on a scanned page. The LLM
         is instructed to transcribe faithfully, not summarise.
    """
    # ── attempt 1: Tesseract ──────────────────────────────────────────────
    try:
        import fitz
        import pytesseract
        from PIL import Image
        import io
        doc = fitz.open(path)
        pix = doc[page_index].get_pixmap(dpi=300)
        image = Image.open(io.BytesIO(pix.tobytes("png")))
        text = pytesseract.image_to_string(image).strip()
        if text:
            return text
    except ImportError:
        pass   # Tesseract not installed — fall through to LLM
    except Exception:
        pass   # Tesseract failed on this page — fall through to LLM

    # ── attempt 2: LLM vision fallback ───────────────────────────────────
    # Render the page to a PNG image, then ask the LLM to transcribe it.
    # This works for handwritten notes and scanned documents even without
    # Tesseract, as long as Ollama is running and the model supports vision.
    try:
        import fitz
        import base64
        import io as _io
        doc = fitz.open(path)
        pix = doc[page_index].get_pixmap(dpi=200)   # lower DPI than Tesseract
                                                      # since LLM doesn't need 300
        png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode("ascii")

        import llm_client
        ok, _ = llm_client.check_connection()
        if not ok:
            return ""

        contents = [{
            "role": "user",
            "text": (
                "This is a scanned page from a course document. "
                "Please transcribe ALL the text you can see on this page, "
                "exactly as written. Do not summarise, interpret, or add "
                "anything. Output only the transcribed text, nothing else."
            ),
            "attachments": [{"bytes": png_bytes, "mime_type": "image/png"}],
        }]
        transcribed = llm_client.generate(
            llm_client.TUTOR_MODEL,
            "You are a faithful document transcriber. Reproduce the text exactly as it appears.",
            contents,
            temperature=0.0,
        ).strip()
        return transcribed
    except Exception:
        return ""   # LLM also unavailable or failed — caller will warn


def read_full_text(path):
    """Read a whole .pdf or .txt file as one string."""
    if path.suffix.lower() == ".pdf":
        return "\n".join(text for _, text in read_pdf_pages(path))
    return path.read_text(encoding="utf-8", errors="replace")


# ------------------------- transcript handling ---------------------------

def _vtt_time_to_seconds(stamp):
    """'01:02:03.500' or '02:03.500' -> seconds (as a number)."""
    parts = stamp.strip().split(":")
    try:
        parts = [float(p.replace(",", ".")) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return None


def _seconds_to_label(seconds):
    """125 -> '02:05', 3725 -> '1:02:05'."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def read_timed_transcript(path):
    """Parse a .vtt or .srt subtitle file into blocks of ~CHUNK_TARGET chars,
    each labeled with the video timestamp where the block starts.
    Returns a list of (time_label, text)."""
    cues = []          # (start_seconds, line_of_text)
    current_start = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if "-->" in line:
            current_start = _vtt_time_to_seconds(line.split("-->")[0])
        elif line and not line.isdigit() and line.upper() != "WEBVTT" and current_start is not None:
            cues.append((current_start, line))

    blocks, block_text, block_start = [], "", None
    for start, line in cues:
        if block_start is None:
            block_start = start
        block_text = (block_text + " " + line).strip()
        if len(block_text) >= CHUNK_TARGET:
            blocks.append((_seconds_to_label(block_start), block_text))
            block_text, block_start = "", None
    if block_text:
        blocks.append((_seconds_to_label(block_start), block_text))
    return blocks


# ------------------------------- main ------------------------------------

def _ingest_notes(year_dir, year, knowledge, report=None, cache=None):
    """Handwritten or typed lecture notes (PDFs/images). Treated like slides
    for indexing purposes but given a distinct type so citations are clear.
    Cited as (Lecture N Notes, p. P) or (Notes: filename, p. P) if no
    lecture number is detectable."""
    notes_dir = year_dir / "handwritten_notes"
    if not notes_dir.exists():
        return
    is_current = (year == CURRENT_YEAR or year is None)
    for path in _standard_files(notes_dir, "handwritten_notes"):
        cached = cache.get(path, "knowledge") if cache else None
        if cached is not None:
            knowledge.extend(cached)
            print(f"  notes [{year or 'flat'}]: {len(cached):>4} pages from {path.name} (cached, unchanged)")
            continue
        produced = []
        lecture = lecture_number_from_filename(path.name)
        pages = read_pptx(path) if path.suffix.lower() == ".pptx" else read_pdf_pages(path, report)
        if not pages and report is not None:
            report.setdefault("empty_files", []).append(path.name)
        for page_num, text in pages:
            if lecture is not None:
                where = f"Lecture {lecture} Notes"
            else:
                where = f"Notes: {path.stem}"
            citation = f"({where}, p. {page_num})"
            produced.append({
                "type": "notes", "tier": 1,
                "year": year or CURRENT_YEAR, "is_current_year": is_current,
                "citation": citation, "citation_current": citation,
                "source_file": path.name, "text": text,
            })
        knowledge.extend(produced)
        if cache:
            cache.put(path, "knowledge", produced)
        print(f"  notes [{year or 'flat'}]: {len(pages):>4} pages from {path.name}")


def _year_dirs():
    """Return (year, dir_path) pairs for all year subfolders that exist on disk,
    newest first (so deduplication keeps the current/newest year).

    Scans EVERY numeric year folder present under materials/, not a fixed range,
    so materials for any year the instructor created — however old or far in the
    future — are ingested without code changes."""
    dirs = []
    if MATERIALS_DIR.exists():
        year_folders = []
        for child in MATERIALS_DIR.iterdir():
            if child.is_dir() and child.name.isdigit():
                year_folders.append(int(child.name))
        for year in sorted(year_folders, reverse=True):
            dirs.append((year, MATERIALS_DIR / str(year)))
    # Also yield the flat materials/ folder itself as year=None (backwards compatibility)
    if MATERIALS_DIR.exists():
        dirs.append((None, MATERIALS_DIR))
    return dirs


def _ingest_slides(year_dir, year, knowledge, report=None, cache=None):
    slides_dir = year_dir / "slides"
    if not slides_dir.exists():
        return
    is_current = (year == CURRENT_YEAR or year is None)
    for path in _standard_files(slides_dir, "slides"):
        cached = cache.get(path, "knowledge") if cache else None
        if cached is not None:
            knowledge.extend(cached)
            print(f"  slides [{year or 'flat'}]: {len(cached):>4} slides from {path.name} (cached, unchanged)")
            continue
        produced = []
        lecture = lecture_number_from_filename(path.name)
        pages = read_pptx(path) if path.suffix.lower() == ".pptx" else read_pdf_pages(path, report)
        if not pages and report is not None:
            report.setdefault("empty_files", []).append(path.name)
        for number, text in pages:
            where = f"Lecture {lecture}" if lecture is not None else path.stem
            citation = f"({where}, Slide {number})"
            if year and not is_current:
                citation = f"({where}, Slide {number}, {year})"
            produced.append({
                "type": "slide", "tier": 1, "year": year or CURRENT_YEAR,
                "is_current_year": is_current,
                "citation": citation,
                "citation_current": f"({where}, Slide {number})",  # always current-year label
                "source_file": path.name,
                "text": text,
            })
        knowledge.extend(produced)
        if cache:
            cache.put(path, "knowledge", produced)
        print(f"  slides [{year or 'flat'}]: {len(pages):>4} slides from {path.name}")


def _ingest_transcripts(year_dir, year, knowledge, cache=None):
    """Transcripts: ONLY ingest from the current year folder (or flat for
    backwards compatibility). Older years' transcripts are intentionally
    excluded -- citations must not point to a different year's video."""
    tr_dir = year_dir / "transcripts"
    if not tr_dir.exists():
        return
    is_current = (year == CURRENT_YEAR or year is None)
    if not is_current:
        print(f"  transcripts [{year}]: skipped (only current-year transcripts are indexed)")
        return
    for path in _standard_files(tr_dir, "transcripts"):
        cached = cache.get(path, "knowledge") if cache else None
        if cached is not None:
            knowledge.extend(cached)
            print(f"  transcripts [{year or 'flat'}]: {len(cached):>4} segments from {path.name} (cached, unchanged)")
            continue
        produced = []
        lecture = lecture_number_from_filename(path.name)
        where = f"Lecture {lecture} Video" if lecture is not None else f"{path.stem} Video"
        if path.suffix.lower() in {".vtt", ".srt"}:
            blocks = read_timed_transcript(path)
            for label, text in blocks:
                produced.append({
                    "type": "transcript", "tier": 1,
                    "year": CURRENT_YEAR, "is_current_year": True,
                    "citation": f"({where}, ~{label})",
                    "citation_current": f"({where}, ~{label})",
                    "source_file": path.name, "text": text,
                })
        else:
            blocks = chunk_text(read_full_text(path))
            for i, text in enumerate(blocks, start=1):
                produced.append({
                    "type": "transcript", "tier": 1,
                    "year": CURRENT_YEAR, "is_current_year": True,
                    "citation": f"({where}, part {i})",
                    "citation_current": f"({where}, part {i})",
                    "source_file": path.name, "text": text,
                })
        knowledge.extend(produced)
        if cache:
            cache.put(path, "knowledge", produced)
        print(f"  transcripts [{year or 'flat'}]: {len(blocks):>4} segments from {path.name}")


class _TextbookStructure:
    """Tracks chapter and section context as pages are processed in order.

    Reads patterns that appear in the text of each page — chapter headers like
    'CHAPTER 3  •  TRANSPORT LAYER', section headers like '3.5 Connection-Oriented
    Transport: TCP', and content-type markers like 'Problems', 'Summary', 'Index' —
    and maintains a running state of which chapter/section we're in.

    This lets every chunk be annotated with its structural context, which:
      - Improves retrieval: embedding 'Chapter 3: Transport Layer | Section 3.7:
        TCP Congestion Control' alongside the actual text makes the chunk far more
        likely to surface for relevant queries than plain page text alone.
      - Improves citations: instead of just '(Textbook, p. 412)' the student sees
        '(Textbook, Ch.3 §3.7, p. 412)' — they can open the book to the right section.
      - Allows skipping non-explanatory pages: problem sets, index, and reference
        pages are tagged so they're not cited as explanations.
    """

    CHAPTER_PAT  = re.compile(r'CHAPTER\s+(\d+)\s*[•·|—\-]\s*([A-Z][A-Z ,:\-]+)',
                               re.IGNORECASE)
    # Section like "3.5 Connection-Oriented Transport: TCP"
    SECTION_PAT  = re.compile(r'^\s*(\d+)\.(\d+)\s+([A-Z][A-Za-z,:\- ]+)',
                               re.MULTILINE)
    # Sub-section like "3.5.1 The TCP Connection"
    SUBSECTION_PAT = re.compile(r'^\s*(\d+)\.(\d+)\.(\d+)\s+([A-Z][A-Za-z,:\- ]+)',
                                re.MULTILINE)
    # Pages that are primarily non-explanatory
    SKIP_PAT     = re.compile(
        r'^(PROBLEMS|SUMMARY|INTERVIEW|INDEX|REFERENCES|APPENDIX|WIRESHARK\s+LAB|'
        r'HOMEWORK\s+PROBLEMS|REVIEWING\s+QUESTIONS|DISCUSSION\s+QUESTIONS)',
        re.IGNORECASE | re.MULTILINE,
    )
    # Table of contents / front matter markers
    TOC_PAT      = re.compile(r'TABLE\s+OF\s+CONTENTS|Brief\s+Contents', re.IGNORECASE)

    def __init__(self):
        self.chapter_num   = 0
        self.chapter_title = ""
        self.section_num   = ""
        self.section_title = ""

    def _title(self, s):
        """Normalize a title string to title case, collapsing whitespace."""
        return re.sub(r'\s+', ' ', s.strip()).title()

    def update(self, page_text: str):
        """Update structural context from the text of one page. Call in page order."""
        # Chapter header (printed in margin/header on each page of a chapter)
        m = self.CHAPTER_PAT.search(page_text)
        if m:
            ch = int(m.group(1))
            if ch != self.chapter_num:
                self.chapter_num   = ch
                self.chapter_title = self._title(m.group(2))
                self.section_num   = ""
                self.section_title = ""

        # Section heading (e.g. "3.5 Connection-Oriented Transport: TCP")
        # Match only when it belongs to the current chapter
        for m in self.SECTION_PAT.finditer(page_text):
            if int(m.group(1)) == self.chapter_num:
                new_sec = f"{m.group(1)}.{m.group(2)}"
                if new_sec != self.section_num:
                    self.section_num   = new_sec
                    self.section_title = self._title(m.group(3))
                break   # take the first match per page

    def context_prefix(self) -> str:
        """Return the structural context string to prepend to chunk text.
        Prepended text is included in the embedding so retrieval is context-aware."""
        if not self.chapter_num:
            return ""
        parts = [f"Chapter {self.chapter_num}: {self.chapter_title}"]
        if self.section_num:
            parts.append(f"Section {self.section_num}: {self.section_title}")
        return " | ".join(parts)

    def citation(self, page_number: int) -> str:
        """Human-readable citation for a chunk on this page."""
        if not self.chapter_num:
            return f"(Textbook, p. {page_number})"
        if self.section_num:
            return f"(Textbook, Ch.{self.chapter_num} §{self.section_num}, p. {page_number})"
        return f"(Textbook, Ch.{self.chapter_num}, p. {page_number})"

    def page_is_explanatory(self, page_text: str) -> bool:
        """True if this page primarily contains explanatory content worth citing.
        Problem sets, summaries, indexes, and TOC pages are not explanatory — they
        may mention concepts without explaining them, so citing them would be
        misleading."""
        if self.TOC_PAT.search(page_text):
            return False
        # 'PROBLEMS' or 'SUMMARY' appearing as a section header (all-caps, near top)
        if self.SKIP_PAT.search(page_text[:600]):
            return False
        # Index-style pages: mostly single-line entries with page numbers
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]
        if len(lines) > 10:
            # If more than 70% of lines are short (index entries), skip
            short = sum(1 for l in lines if len(l) < 50)
            if short / len(lines) > 0.70:
                return False

        # Problem-statement patterns — questions/exercises within the text
        # "P8." "R3." "WE3." at the start of a line (standard textbook numbering)
        prob_items = re.findall(
            r'^\s*[PRW][\dE]\d*\.',   # P1. R3. WE2. at line start
            page_text, re.MULTILINE,
        )
        if len(prob_items) >= 2:
            return False

        # "a. ... b. ... c." multiple lettered sub-questions
        lettered = re.findall(r'^\s*[a-e]\.\s', page_text, re.MULTILINE)
        if len(lettered) >= 3:
            return False

        # Prose-style problem indicators: "Consider a ...", "Suppose that ..."
        # appearing multiple times on one page indicates a problem set page
        prose_prob = re.findall(
            r'^\s*(Consider |Suppose |Assume |Show that |Calculate |Compute |'
            r'Explain why |Why is |What is the |How many |Prove that )',
            page_text, re.MULTILINE | re.IGNORECASE,
        )
        if len(prose_prob) >= 3:
            return False

        return True


def _ingest_textbook(year_dir, year, knowledge, report=None, cache=None):
    tb_dir = year_dir / "textbook"
    if not tb_dir.exists():
        return
    is_current = (year == CURRENT_YEAR or year is None)
    for path in _standard_files(tb_dir, "textbook"):
        cached = cache.get(path, "knowledge") if cache else None
        if cached is not None:
            knowledge.extend(cached)
            print(f"  textbook [{year or 'flat'}]: {len(cached):>4} chunks from {path.name} (cached, unchanged)")
            continue

        produced = []
        skipped_pages = 0
        if path.suffix.lower() == ".pdf":
            structure = _TextbookStructure()
            # Read ALL pages first so we can process in order for context tracking
            all_pages = read_pdf_pages(path, report)
            for page_number, page_text in all_pages:
                structure.update(page_text)
                if not structure.page_is_explanatory(page_text):
                    skipped_pages += 1
                    continue
                prefix = structure.context_prefix()
                citation = structure.citation(page_number)
                for piece in chunk_text(page_text):
                    # Prepend structural context to the chunk text so embeddings
                    # carry chapter/section information. Prefix is separated by
                    # a newline so it reads naturally if ever shown.
                    enriched_text = f"{prefix}\n{piece}" if prefix else piece
                    produced.append({
                        "type": "textbook", "tier": 1,
                        "year": year or CURRENT_YEAR, "is_current_year": is_current,
                        "citation": citation,
                        "citation_current": citation,
                        "source_file": path.name,
                        "text": enriched_text,
                        # Store the raw page text separately so chunk_text chunking
                        # operates on the clean text but citations stay page-accurate
                        "chapter": structure.chapter_num,
                        "section": structure.section_num,
                    })
            if not produced and report is not None:
                report.setdefault("empty_files", []).append(path.name)
            knowledge.extend(produced)
            if cache:
                cache.put(path, "knowledge", produced)
            print(f"  textbook [{year or 'flat'}]: {len(produced):>4} chunks from "
                  f"{path.name} ({skipped_pages} non-explanatory pages skipped)")

        elif path.suffix.lower() == ".txt":
            # For plain-text exports, structure detection still works
            full_text = read_full_text(path)
            structure = _TextbookStructure()
            structure.update(full_text)   # best-effort single-pass
            prefix = structure.context_prefix()
            pieces = chunk_text(full_text)
            for i, piece in enumerate(pieces, start=1):
                enriched = f"{prefix}\n{piece}" if prefix else piece
                produced.append({
                    "type": "textbook", "tier": 1,
                    "year": year or CURRENT_YEAR, "is_current_year": is_current,
                    "citation": f"(Textbook: {path.stem}, part {i})",
                    "citation_current": f"(Textbook: {path.stem}, part {i})",
                    "source_file": path.name, "text": enriched,
                })
            knowledge.extend(produced)
            if cache:
                cache.put(path, "knowledge", produced)
            print(f"  textbook [{year or 'flat'}]: {len(pieces):>4} chunks from {path.name}")


def _ingest_homework_solutions(year_dir, year, solutions, cache=None):
    sol_dir = year_dir / "homework_solutions"
    if not sol_dir.exists():
        return
    is_current = (year == CURRENT_YEAR or year is None)
    for path in _standard_files(sol_dir, "homework_solutions"):
        cached = cache.get(path, "solutions") if cache else None
        if cached is not None:
            solutions.extend(cached)
            print(f"  SOLUTIONS [{year or 'flat'}]: {len(cached):>4} chunks from {path.name} (cached, unchanged)")
            continue
        produced = []
        hw_id = hw_id_from_filename(path.name)
        # Tag with year so the guard can apply Socratic mode regardless of year
        hw_id_with_year = f"{hw_id}-{year}" if year and not is_current else hw_id
        pieces = chunk_text(read_full_text(path))
        for i, piece in enumerate(pieces, start=1):
            produced.append({
                "hw_id": hw_id,           # base id (HW4) for vault lookup
                "hw_id_year": hw_id_with_year,
                "year": year or CURRENT_YEAR,
                "is_current_year": is_current,
                "part": i, "source_file": path.name, "text": piece,
            })
        solutions.extend(produced)
        if cache:
            cache.put(path, "solutions", produced)
        print(f"  SOLUTIONS [{year or 'flat'}]: {len(pieces):>4} chunks from {path.name} (private, {hw_id_with_year})")


def _deduplicate(knowledge):
    """Remove chunks that are TRULY duplicated across years — same full text
    AND same citation slot — keeping the current/newest year's copy so citations
    point to the current year.

    Critical correctness note: the fingerprint is the FULL text (hashed), not a
    prefix. An earlier version used only the first 200 characters, which caused
    distinct slides that share opening boilerplate (e.g. a repeated course-title
    header, section dividers, or short slides like "Questions?") to collide and
    be silently dropped — entire lecture decks collapsed to a single chunk. Using
    the full text as the key guarantees that two chunks are only ever treated as
    duplicates when their entire content is identical.

    Transcripts and instructor answers are never deduplicated (they're already
    current-year-only). Homework-solution chunks live in a separate file and
    don't pass through here at all.

    Deduplication only collapses entries that are BOTH (a) full-text identical
    AND (b) from the same citation position — i.e. genuinely the same material
    re-used across years. Two different slides with identical text (rare but
    possible) from the SAME year are both kept, because dropping one would lose
    a real chunk."""
    import hashlib

    def _key(item):
        text_hash = hashlib.md5(item["text"].encode("utf-8")).hexdigest()
        # citation_current is the year-agnostic label (e.g. "(Lecture 9, Slide 4)")
        # so the same slide across years shares a key, but different slides don't.
        slot = item.get("citation_current", item.get("citation", ""))
        return f'{item["type"]}|{slot}|{text_hash}'

    seen = {}   # key -> index in result
    result = []
    for item in knowledge:
        if item["type"] in ("transcript", "instructor_answer"):
            result.append(item)
            continue
        key = _key(item)
        if key not in seen:
            seen[key] = len(result)
            result.append(item)
        else:
            existing = result[seen[key]]
            new_year = item.get("year", 0)
            ex_year = existing.get("year", 0)
            # Keep the current-year copy; else the newest year's copy.
            if item.get("is_current_year") and not existing.get("is_current_year"):
                result[seen[key]] = item
            elif (not item.get("is_current_year")
                  and not existing.get("is_current_year")
                  and new_year > ex_year):
                result[seen[key]] = item
    return result


def main(incremental=True):
    _refresh_year_globals()   # CURRENT_YEAR / YEARS_SUPPORTED = today's calendar year
    if not MATERIALS_DIR.exists():
        print("I couldn't find a 'materials' folder. Create it and run again.")
        print()
        print("Year-aware layout (recommended):")
        print("  materials/2025/slides/    materials/2025/transcripts/")
        print("  materials/2025/textbook/  materials/2025/homework_solutions/")
        print("  materials/2024/slides/    materials/2024/homework_solutions/")
        print("  ... (years 2021-present supported)")
        print()
        print("Flat layout (backwards compatible, treated as current year):")
        print("  materials/slides/  materials/transcripts/  etc.")
        return

    knowledge = []
    solutions = []
    report = {"no_text": [], "empty_files": []}
    # Parse cache: on an incremental run, unchanged files are reused instead of
    # re-read. On a full rebuild (incremental=False) the cache starts empty so
    # every file is parsed fresh.
    cache = ParseCache(enabled=incremental)

    for year, year_dir in _year_dirs():
        print(f"\n--- {'flat (current year)' if year is None else str(year)} ---")
        _ingest_slides(year_dir, year, knowledge, report, cache)
        _ingest_transcripts(year_dir, year, knowledge, cache)
        _ingest_notes(year_dir, year, knowledge, report, cache)
        _ingest_textbook(year_dir, year, knowledge, report, cache)
        _ingest_homework_solutions(year_dir, year, solutions, cache)

    # Instructor-approved answers (tier 2, always current)
    if INSTRUCTOR_ANSWERS_FILE.exists():
        text = INSTRUCTOR_ANSWERS_FILE.read_text(encoding="utf-8").strip()
        approved = json.loads(text) if text else []
        for item in approved:
            date = item.get("date", "")
            knowledge.append({
                "type": "instructor_answer", "tier": 2,
                "year": CURRENT_YEAR, "is_current_year": True,
                "citation": f"(Instructor answer, {date})" if date else "(Instructor answer)",
                "citation_current": f"(Instructor answer, {date})" if date else "(Instructor answer)",
                "source_file": INSTRUCTOR_ANSWERS_FILE.name,
                "text": f'Q: {item["question"]}\nA: {item["answer"]}',
            })
        print(f"\n  instructor  : {len(approved):>4} approved answers")

    knowledge = _deduplicate(knowledge)

    KNOWLEDGE_FILE.write_text(json.dumps(knowledge, indent=2, ensure_ascii=False), encoding="utf-8")
    SOLUTIONS_FILE.write_text(json.dumps(solutions, indent=2, ensure_ascii=False), encoding="utf-8")
    cache.save()   # persist per-file signatures + chunks for the next incremental run

    # Write a report of any files that produced no searchable text, so the
    # dashboard can show the instructor exactly which files failed and why.
    Path("ingest_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    by_type = {}
    for item in knowledge:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    print(f"\nDone. knowledge.json: {len(knowledge)} chunks {by_type}")
    print(f"      solutions.json: {len(solutions)} private chunks")
    print(f"      Current year = {CURRENT_YEAR} (automatic, from today's date)")
    print("Next: python build_index.py")


if __name__ == "__main__":
    main()

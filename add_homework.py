"""
add_homework.py  —  v3: LLM-parsed homework registration

Put every homework file (.pdf or .txt) in:    materials\\homework_problems\\
Name them so the number is visible: HW1.pdf, hw4_problems.pdf ...

Commands:
    python add_homework.py                  parse ALL files into assignments.json
    python add_homework.py status           list every homework + active state
    python add_homework.py activate HW4     mark all of HW4's problems active
    python add_homework.py deactivate HW2   mark all of HW2's problems inactive
    python add_homework.py --regex          parse without the LLM (offline fallback)

What's new in v3:
  Parsing is now done BY THE LLM. Gemini reads each file's raw extracted text
  and returns clean per-problem entries: it finds the problem boundaries even
  when PDF extraction mangles the headings, keeps all sub-parts (a, b, c...)
  with their problem, repairs broken line breaks, and preserves every given
  number exactly. The old heading-regex splitter remains as an offline fallback.

Re-running is always safe: entries are rebuilt from the files, but your
active/inactive choices are preserved. Brand-new entries default to active.
"""

import os
import sys
import re
import json
from pathlib import Path

from pypdf import PdfReader
from dotenv import load_dotenv

import llm_client   # ****** see llm_client.py to change which model/provider is used ******

load_dotenv()

ASSIGNMENTS_FILE = Path("assignments.json")
HOMEWORK_DIR = Path("materials") / "homework_problems"   # flat (legacy)
HW_PARSE_CACHE_FILE = Path("homework_parse_cache.json")  # per-file signatures


def _file_sig(path: Path):
    """Signature of a homework file: size + mtime. Used to detect whether a file
    changed since it was last parsed, so unchanged files can be reused instead
    of re-running the (expensive) LLM problem-splitter on them."""
    try:
        st = path.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return None


def _homework_dirs():
    """Return (year, path) pairs for all homework_problems folders that exist,
    newest year first, then the flat folder. Scans every numeric year folder on
    disk so any year the instructor created is picked up automatically."""
    dirs = []
    materials = Path("materials")
    if materials.exists():
        year_folders = sorted(
            (int(c.name) for c in materials.iterdir()
             if c.is_dir() and c.name.isdigit()),
            reverse=True,
        )
        for year in year_folders:
            d = materials / str(year) / "homework_problems"
            if d.exists():
                dirs.append((year, d))
    # flat fallback
    if HOMEWORK_DIR.exists():
        dirs.append((None, HOMEWORK_DIR))
    return dirs
# **** ALIAS, NOT THE SOURCE OF TRUTH -- edit llm_client.py to change models ****
MODEL = llm_client.TUTOR_MODEL

PROBLEM_HEADING = re.compile(r"(?:^|\n)\s*(?:Problem|Question|Q\.?)\s*(\d+)", re.IGNORECASE)

PARSER_INSTRUCTION = """You convert the raw extracted text of a university homework into clean, structured problems.

Rules:
- Identify each numbered problem. Keep ALL of a problem's sub-parts (a, b, c, i, ii...) together inside that problem's text.
- Repair extraction damage: rejoin broken lines, fix spacing, drop page headers/footers and course boilerplate (title, due date, instructions about submission).
- PRESERVE every given number, unit, and symbol exactly as written. Do not solve, shorten, or summarize the problems.
- If the text contains no numbered problems, return the whole assignment as one problem with number 1.

Respond with ONLY a JSON array:
[{"number": 1, "text": "<complete cleaned problem statement>"}, ...]"""


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    return path.read_text(encoding="utf-8", errors="replace")


def hw_id_from_filename(filename: str) -> str:
    match = re.search(r"(?:hw|homework)\s*_?\s*(\d+)", filename, re.IGNORECASE)
    return f"HW{match.group(1)}" if match else Path(filename).stem


# ----------------------------- two parsers --------------------------------

def llm_split(text: str, client):
    """Ask the LLM to return the problems as clean JSON. None on any failure."""
    try:
        reply_text = client.generate(
            MODEL, PARSER_INSTRUCTION,
            [{"role": "user", "text": f"RAW HOMEWORK TEXT:\n\n{text}"}],
            temperature=0.0, json_mode=True,
        )
        problems = json.loads(reply_text)
        if isinstance(problems, list) and all("number" in p and "text" in p for p in problems):
            return [(int(p["number"]), p["text"].strip()) for p in problems]
    except Exception as error:
        print(f"    (LLM parse failed: {error})")
    return None


def regex_split(text: str):
    """Offline fallback: split on 'Problem N' style headings."""
    matches = list(PROBLEM_HEADING.finditer(text))
    if not matches:
        return None
    problems = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        problems.append((int(match.group(1)), re.sub(r"\s+", " ", text[start:end]).strip()))
    return problems


def entries_from_file(path: Path, client):
    hw_id = hw_id_from_filename(path.name)
    text = read_text(path)

    problems, how = None, ""
    if client is not None:
        problems = llm_split(text, client)
        how = "LLM-parsed"
    if problems is None:
        problems = regex_split(text)
        how = "regex fallback"
    if problems is None:
        problems = [(1, re.sub(r"\s+", " ", text).strip())]
        how = "whole file (no headings found)"

    entries = [
        {"id": f"{hw_id}-P{number}", "active": True, "text": problem_text,
         "source_file": path.name}
        for number, problem_text in problems
    ]
    return hw_id, entries, how


# --------------------------- assignments file -----------------------------

def _safe_read_json(path):
    """Read a JSON file safely — returns [] on missing, empty, or corrupt file."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"WARNING: {p.name} is corrupt ({e}). Treating as empty.")
        return []


def load_assignments():
    return _safe_read_json(ASSIGNMENTS_FILE)


def save_assignments(entries):
    ASSIGNMENTS_FILE.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _content_hash(text: str) -> str:
    """Short hash of problem text — used to detect when file content changed
    between scans so we know to update the entry without losing the active flag."""
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def scan_folder(use_llm=True, incremental=True):
    """Parse every homework file on disk and write the fully-synchronised
    assignments.json.

    Synchronisation rules (the core of why this exists):
      1. Problem TEXT always comes from the file on disk — never from a cached
         version. If the instructor replaces HW4, the new problem text is used.
      2. The ACTIVE flag is always preserved from the previous entry for that id.
         Replacing a file does not reset the instructor's active/inactive toggle.
      3. Entries whose files no longer exist on disk are REMOVED. Stale entries
         from deleted files do not linger in the guardrail's check list.
      4. A content_hash is stored per entry. If the hash changes between scans,
         the update is logged so the instructor knows the guardrail text changed.
      5. For speed: if a file's mtime hasn't changed since the last scan and the
         entry is already present, re-parsing is skipped (hash confirms no change).
    """
    dirs = _homework_dirs()
    if not dirs:
        print("No homework_problems folder found under materials/. "
              "Upload homework files first via the instructor dashboard.")
        return

    client = None
    if use_llm:
        ok, info = llm_client.check_connection()
        if ok:
            client = llm_client
        else:
            print(f"Local model server not reachable — using regex fallback parser. ({info})\n")

    try:
        from ingest import current_year
        CURRENT_YEAR = current_year()
    except ImportError:
        import datetime as _dt
        CURRENT_YEAR = _dt.date.today().year

    # Build lookup from existing entries: id -> entry (for active flag + hash)
    existing = load_assignments()
    existing_by_id = {e["id"]: e for e in existing}
    # Group existing entries by their source file so we can reuse a whole file's
    # problems without re-parsing when the file is unchanged.
    existing_by_file = {}
    for e in existing:
        sf = e.get("source_file")
        if sf:
            existing_by_file.setdefault(sf, []).append(e)

    # Load the per-file signature cache from the last scan.
    old_sigs = {}
    if incremental and HW_PARSE_CACHE_FILE.exists():
        try:
            txt = HW_PARSE_CACHE_FILE.read_text(encoding="utf-8").strip()
            old_sigs = json.loads(txt) if txt else {}
        except Exception:
            old_sigs = {}
    new_sigs = {}

    rebuilt = []       # all entries we produce this run
    rebuilt_ids = set()
    changed_count = 0
    new_count = 0
    reused_count = 0

    # Only parse standard-named homework problem files (HW<n>_problems.pdf/.txt).
    # Original downloads with other names are ignored, matching ingestion.
    try:
        from ingest import is_standard_name
    except ImportError:
        import re as _re
        _hw_pat = _re.compile(r"^HW\d+_problems\.(pdf|txt)$", _re.IGNORECASE)
        def is_standard_name(cat, fn): return bool(_hw_pat.match(fn))

    for year, folder in dirs:
        files = sorted(p for p in folder.iterdir()
                       if p.suffix.lower() in {".pdf", ".txt"}
                       and is_standard_name("homework_problems", p.name))
        if not files:
            continue

        year_label = str(year) if year else "flat (current year)"
        print(f"\n--- homework_problems [{year_label}] ---")
        is_current = (year == CURRENT_YEAR or year is None)

        for path in files:
            sig = _file_sig(path)
            # ── Reuse path: file unchanged since last scan AND we still have its
            #    parsed entries in assignments.json → skip the LLM entirely.
            cached_entries = existing_by_file.get(path.name)
            if (incremental and cached_entries
                    and old_sigs.get(path.name) == sig):
                for e in cached_entries:
                    # Keep everything (active flag, content_hash, text) as-is.
                    rebuilt.append(e)
                    rebuilt_ids.add(e["id"])
                new_sigs[path.name] = sig
                reused_count += len(cached_entries)
                print(f"  {path.name:<36} -> reused {len(cached_entries)} problem(s) (unchanged)")
                continue

            # ── Parse path: new or modified file → run the splitter.
            hw_id, entries, how = entries_from_file(path, client)

            for entry in entries:
                # Namespace past-year ids so HW4-P3 from 2025 ≠ HW4-P3 from 2026
                if not is_current:
                    entry["id"] = f'{entry["id"]}-{year}'
                entry["year"] = year or CURRENT_YEAR
                entry["is_current_year"] = is_current

                new_hash = _content_hash(entry["text"])
                prev = existing_by_id.get(entry["id"])

                if prev is None:
                    # Brand-new problem — default active for current year only
                    entry["active"] = is_current
                    entry["content_hash"] = new_hash
                    new_count += 1
                else:
                    # ALWAYS preserve the instructor's active/inactive toggle
                    entry["active"] = prev.get("active", is_current)
                    entry["content_hash"] = new_hash
                    if prev.get("content_hash") != new_hash:
                        # Content changed — log it so the instructor is aware
                        print(f"    ⟳ {entry['id']}: content updated "
                              f"(guardrail now checks the new version)")
                        changed_count += 1

                rebuilt_ids.add(entry["id"])

            rebuilt.extend(entries)
            new_sigs[path.name] = sig
            print(f"  {path.name:<36} -> {hw_id}: {len(entries)} problem(s)  [{how}]")

    # ── Report entries that disappeared (files deleted) ───────────────────────
    removed = [eid for eid in existing_by_id if eid not in rebuilt_ids]
    if removed:
        print(f"\n  Removed {len(removed)} stale entry(ies) whose files no longer exist: "
              + ", ".join(removed))

    # ── Do NOT keep stale entries — only what's on disk right now ─────────────
    # This is intentional: if a homework file is deleted, its problems should
    # no longer appear in the guardrail. "Keep" would let deleted homeworks
    # silently linger.
    save_assignments(rebuilt)

    # Persist the per-file signatures so the next scan can reuse unchanged files.
    try:
        HW_PARSE_CACHE_FILE.write_text(json.dumps(new_sigs, ensure_ascii=False),
                                       encoding="utf-8")
    except Exception:
        pass

    active_count   = sum(1 for e in rebuilt if e.get("active"))
    current_active = sum(1 for e in rebuilt if e.get("active") and e.get("is_current_year", True))
    print(f"\nassignments.json synchronised: {len(rebuilt)} total entries "
          f"({new_count} new, {changed_count} updated, {reused_count} reused unchanged, "
          f"{len(removed)} removed), "
          f"{active_count} active ({current_active} from current year {CURRENT_YEAR}).")


def show_status():
    entries = load_assignments()
    if not entries:
        print("assignments.json is empty. Run 'python add_homework.py' first.")
        return
    by_hw = {}
    for entry in entries:
        by_hw.setdefault(entry["id"].split("-")[0], []).append(entry)
    for hw, group in sorted(by_hw.items()):
        states = {"active" if e["active"] else "inactive" for e in group}
        state = states.pop() if len(states) == 1 else "MIXED"
        print(f"  {hw:<8} {len(group)} problem(s)   {state}")


def set_active(hw_id: str, value: bool, year: int | None = None):
    """Set the active flag for all problems under a given hw_id.

    If year is given, only entries for that specific year are changed.
    If year is None, all entries whose id starts with hw_id are changed
    (backward-compatible behaviour for terminal use and for current-year
    entries which have plain ids like HW4-P3).
    """
    entries = load_assignments()
    hw_id_upper = hw_id.upper()
    changed = 0
    for entry in entries:
        eid = entry["id"].upper()
        matches_id = (eid == hw_id_upper or eid.startswith(hw_id_upper + "-"))
        matches_year = (year is None or entry.get("year") == year)
        if matches_id and matches_year:
            entry["active"] = value
            changed += 1
    if changed:
        save_assignments(entries)
        print(f"{hw_id}: {changed} problem(s) set to {'active' if value else 'inactive'}.")
    else:
        print(f"No entries found for {hw_id}. Try 'python add_homework.py status'.")


def main():
    args = sys.argv[1:]
    if not args:
        scan_folder(use_llm=True)
    elif args[0] == "--regex":
        scan_folder(use_llm=False)
    elif args[0].lower() == "status":
        show_status()
    elif args[0].lower() == "activate" and len(args) == 2:
        set_active(args[1], True)
    elif args[0].lower() == "deactivate" and len(args) == 2:
        set_active(args[1], False)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

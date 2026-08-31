"""
build_index.py  —  v3: builds TWO separate search indexes, incrementally

  1. 'ccn_knowledge'  -- slides, transcripts, textbook, instructor answers.
                         This is the ONLY index the student-facing side searches.
  2. 'ccn_solutions'  -- homework solutions. A physically separate collection,
                         consulted only by homework mode, behind the scenes,
                         to guide and verify -- never shown to students.

v3 change: rebuilds are now INCREMENTAL. Embedding text (running the neural
embedding model over every chunk) is the slow part. Previously every rebuild
deleted the whole index and re-embedded every chunk of every file, even ones
that hadn't changed -- so adding one small file re-embedded the entire
textbook. Now we keep chunks whose text is unchanged and only embed new or
changed ones. A first build, or a build after deleting chroma_db, still does
everything (nothing to reuse yet).

Run it after every 'python ingest.py':   python build_index.py
"""

import json
from pathlib import Path

import chromadb

KNOWLEDGE_FILE = Path("knowledge.json")
SOLUTIONS_FILE = Path("solutions.json")
DB_FOLDER = "chroma_db"


def _noop(*args, **kwargs):
    pass


def rebuild(client, name, documents, metadatas, ids, progress=_noop):
    """Sync a collection to exactly (documents, metadatas, ids), reusing any
    chunks already embedded under the same id WITH the same text, so unchanged
    content is never re-embedded.

    progress(done, total, label): optional callback for UI progress reporting.
    """
    # What's already in the index, and what text each id currently holds?
    try:
        collection = client.get_collection(name)
        existing = collection.get(include=["documents"])
        existing_text = dict(zip(existing["ids"], existing["documents"]))
    except Exception:
        collection = client.create_collection(name=name)
        existing_text = {}

    wanted_ids = set(ids)

    # 1. Remove ids that should no longer exist (files deleted/changed).
    stale = [i for i in existing_text if i not in wanted_ids]
    if stale:
        collection.delete(ids=stale)

    # 2. Figure out which chunks actually need embedding: brand-new ids, or
    #    ids whose text changed since last time. Unchanged ones are skipped.
    new_idx = [k for k, cid in enumerate(ids)
               if cid not in existing_text or existing_text[cid] != documents[k]]

    total = len(new_idx)
    if total == 0:
        progress(0, 0, f"{name}: already up to date, nothing to embed")
        return collection

    # 3. Changed ids must be deleted before re-adding (add won't overwrite).
    changed = [ids[k] for k in new_idx if ids[k] in existing_text]
    if changed:
        collection.delete(ids=changed)

    # 4. Embed + store only the new/changed chunks, in batches, reporting progress.
    batch = 100
    done = 0
    for start in range(0, total, batch):
        sub = new_idx[start:start + batch]
        collection.add(
            documents=[documents[k] for k in sub],
            metadatas=[metadatas[k] for k in sub],
            ids=[ids[k] for k in sub],
        )
        done += len(sub)
        progress(done, total, f"{name}: embedded {done} / {total} new/changed chunks")
    return collection


def _stable_id(prefix, item, index):
    """A content-stable id: same chunk -> same id across rebuilds, so unchanged
    chunks line up and get reused. Uses source file + position + a short hash
    of the text so reordering or edits are detected."""
    import hashlib
    h = hashlib.md5(item["text"].encode("utf-8")).hexdigest()[:8]
    return f'{prefix}-{item["source_file"]}-{index}-{h}'


def _safe_read_json(path, default=None):
    """Safe JSON reader — returns default ([] by default) on missing/empty/corrupt."""
    if default is None:
        default = []
    p = Path(path)
    if not p.exists():
        return default
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"WARNING: {p.name} is not valid JSON — treating as empty.")
        return default


def main(progress=_noop):
    if not KNOWLEDGE_FILE.exists():
        print("knowledge.json not found. Run 'python ingest.py' first.")
        return

    client = chromadb.PersistentClient(path=DB_FOLDER)

    # ---- knowledge index (student-facing) ----
    knowledge = _safe_read_json(KNOWLEDGE_FILE)
    if not knowledge:
        print("knowledge.json is empty — nothing to index. Run ingest.py first.")
        return
    documents, metadatas, ids = [], [], []
    for i, item in enumerate(knowledge):
        documents.append(item["text"])
        # Carry ALL retrieval-relevant metadata into the index. Previously only
        # citation/type/tier/source_file were copied, which silently dropped the
        # year fields -- so citations couldn't show the year and current-year
        # precedence couldn't work. ChromaDB rejects None metadata values, so we
        # only include optional fields when present.
        meta = {
            "citation": item["citation"],
            "type": item["type"],
            "tier": item["tier"],
            "source_file": item["source_file"],
        }
        if item.get("year") is not None:
            meta["year"] = item["year"]
        if item.get("is_current_year") is not None:
            meta["is_current_year"] = bool(item["is_current_year"])
        if item.get("citation_current"):
            meta["citation_current"] = item["citation_current"]
        metadatas.append(meta)
        ids.append(_stable_id("k", item, i))
    print(f"Indexing {len(documents)} knowledge chunks...")
    rebuild(client, "ccn_knowledge", documents, metadatas, ids, progress=progress)

    # ---- solutions vault (private) ----
    solutions = _safe_read_json(SOLUTIONS_FILE)
    if solutions:
        documents = [s["text"] for s in solutions]
        metadatas = [{
            "hw_id": s["hw_id"],
            "hw_id_year": s.get("hw_id_year", s["hw_id"]),
            "year": s.get("year", 0),
            "source_file": s["source_file"],
        } for s in solutions]
        ids = [_stable_id("s", s, i) for i, s in enumerate(solutions)]
        print(f"Indexing {len(documents)} PRIVATE solution chunks...")
        rebuild(client, "ccn_solutions", documents, metadatas, ids, progress=progress)
    else:
        print("No solution files found -- skipping the solutions vault (that's fine).")

    print("\nDone. Both indexes live in the 'chroma_db' folder.")


if __name__ == "__main__":
    main()

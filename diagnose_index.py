#!/usr/bin/env python3
"""
diagnose_index.py

Diagnostic for confirming what is actually in the knowledge index. Run this on
the server to check whether all material types (slides, textbook, transcripts,
notes) are indexed, and how many chunks of each exist. This answers "are slides
even in the index, and are they retrievable?".

Usage (from the project directory, with the venv active):
    python diagnose_index.py
    python diagnose_index.py "TCP slow start"     # also test a sample query
"""

import sys
import collections
import chromadb
import ask


def main():
    try:
        client = chromadb.PersistentClient(path=ask.DB_FOLDER)
        col = client.get_collection(ask.COLLECTION_NAME)
    except Exception as e:
        print(f"Could not open the knowledge index: {e}")
        print("Have you run 'python ingest.py' then 'python build_index.py'?")
        return

    total = col.count()
    print(f"Knowledge index: {total} total chunks\n")

    # Pull everything's metadata to tally by type and year.
    data = col.get(include=["metadatas"])
    metas = data["metadatas"]

    by_type = collections.Counter(m.get("type", "?") for m in metas)
    by_year = collections.Counter(str(m.get("year", "?")) for m in metas)
    by_type_year = collections.Counter(
        (m.get("type", "?"), str(m.get("year", "?"))) for m in metas)

    print("Chunks by material type:")
    for t, n in by_type.most_common():
        print(f"  {t:<14} {n:>6}")
    if "slide" not in by_type:
        print("\n  ⚠️  NO slide chunks are indexed. Check that your slide files are")
        print("      named 'Lecture_NN_slides.pptx/pdf' and live under materials/<year>/slides/,")
        print("      then re-run ingest.py and build_index.py.")

    print("\nChunks by year:")
    for y, n in by_year.most_common():
        print(f"  {y:<14} {n:>6}")

    print("\nChunks by type and year:")
    for (t, y), n in sorted(by_type_year.items()):
        print(f"  {t:<14} {y:<6} {n:>6}")

    # Optional: run a sample query and show what types come back.
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        print(f"\nSample query: {q!r}")
        chunks = ask.retrieve(col, q, ask.HOW_MANY)
        print(f"Top {len(chunks)} results (type — citation):")
        for c in chunks:
            print(f"  {c['type']:<12} {c['citation']}")
        types_back = set(c["type"] for c in chunks)
        print(f"\nMaterial types represented in results: {', '.join(sorted(types_back))}")


if __name__ == "__main__":
    main()

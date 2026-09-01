#!/usr/bin/env python3
"""
diagnose_citations.py

Checks whether the indexed metadata carries the `year` field (needed for the
year to appear in citations) and shows sample citations as students would see
them. Run on the server after a rebuild.

    python diagnose_citations.py
    python diagnose_citations.py "TCP congestion control"   # test a query
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
        print(f"Could not open the index: {e}")
        return

    data = col.get(include=["metadatas"])
    metas = data["metadatas"]
    total = len(metas)
    print(f"Index has {total} chunks.\n")

    # How many chunks carry a year, broken down by type?
    by_type_total = collections.Counter(m.get("type", "?") for m in metas)
    by_type_with_year = collections.Counter(
        m.get("type", "?") for m in metas if m.get("year"))

    print("Year field presence by type (with-year / total):")
    for t in by_type_total:
        wy = by_type_with_year.get(t, 0)
        flag = "  <-- MISSING year!" if wy < by_type_total[t] else ""
        print(f"  {t:<12} {wy:>5} / {by_type_total[t]:<5}{flag}")

    missing = total - sum(by_type_with_year.values())
    if missing:
        print(f"\n⚠️  {missing} chunk(s) have NO year field. The year cannot appear")
        print("    in their citations. This means the index was built without the")
        print("    year metadata -- rebuild with the current build_index.py:")
        print("        rm -rf chroma_db && python ingest.py && python build_index.py")
    else:
        print("\n✅ Every chunk carries a year -- citations will show it.")

    # Show sample citations exactly as students see them.
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        print(f"\nSample citations for query {q!r}:")
        for c in ask.retrieve(col, q, ask.HOW_MANY):
            print(f"  {c['type']:<11} {c['citation']}")


if __name__ == "__main__":
    main()

"""
check_detection.py  —  X-ray for homework detection (no API needed, free)

Type any question and see exactly what detection thinks:
  - keyword signal:  did the question name an assignment?
  - meaning signal:  which active problem is closest, and its score
  - numbers signal:  which distinctive numbers the question shares with a problem
  - the final verdict (homework mode ON or OFF)

It also health-checks your setup first: how many problems are active, and a
preview of each so you can spot garbled PDF extractions at a glance.

Run it with:   python check_detection.py
"""

import ask   # reuses the exact detection logic the app uses


def main():
    active = ask.load_active_assignments()
    print(f"Active assignments loaded: {len(active)}")
    if not active:
        print("  !! ZERO active assignments -- detection can only use keywords.")
        print("  !! Run: python add_homework.py   then   python add_homework.py status")
        print()
    else:
        for a in active:
            preview = a["text"][:80].replace("\n", " ")
            print(f'  {a["id"]:>10}  "{preview}..."')
        print("  (If any preview looks like gibberish, that PDF extracted badly --")
        print("   fix its text directly in assignments.json.)")
        print()

    assignment_col = ask.build_assignment_index(active)
    print("Type a student question to test (or 'quit'):\n")
    while True:
        question = input("Question: ").strip()
        if question.lower() in {"quit", "exit", ""}:
            break

        # the same three signals detect_homework uses, shown individually
        keyword_hit = bool(ask.HOMEWORK_KEYWORDS.search(question))

        best_id, best_score = None, 0.0
        if assignment_col is not None:
            result = assignment_col.query(query_texts=[question], n_results=1)
            if result["ids"][0]:
                best_id = result["ids"][0][0]
                best_score = 1 / (1 + result["distances"][0][0])

        question_numbers = ask.distinctive_numbers(question)
        overlaps = []
        for a in active:
            shared = question_numbers & ask.distinctive_numbers(a["text"])
            if shared:
                overlaps.append((a["id"], sorted(shared)))

        is_hw, reason, _ = ask.detect_homework(question, assignment_col)

        print(f"  keyword signal : {'HIT' if keyword_hit else 'no'}")
        print(f"  meaning signal : closest = {best_id} at {best_score:.2f} "
              f"(trigger >= {ask.HOMEWORK_MATCH_THRESHOLD})")
        print(f"  numbers signal : question has {sorted(question_numbers) or 'none'}; "
              f"overlaps = {overlaps or 'none'} "
              f"(trigger >= {ask.NUMBER_OVERLAP_TRIGGER} shared with one problem)")
        print(f"  VERDICT        : {'HOMEWORK MODE ON' if is_hw else 'normal mode'}"
              f"{'  (' + reason + ')' if reason else ''}")
        print()


if __name__ == "__main__":
    main()

"""
resolve_tickets.py  —  instructor tool for the escalation loop (Phase C)

Tickets reach you two ways:
  - coverage : the course materials didn't cover a question well
  - rating   : a student rated an answer "Partly" or "Not yet" understood

This tool lets you read the open ones, write your own answer, and (optionally)
publish that answer into the Tier-2 knowledge base so the NEXT student who asks
something similar gets your explanation, cited as "(Instructor answer, <date>)".

Commands:
    python resolve_tickets.py            list open tickets (most-asked first)
    python resolve_tickets.py answer     work through open tickets one by one

A nicer point-and-click version of this is coming in the Phase D dashboard;
this is the working engine underneath it.
"""

import sys
import ask


def list_open():
    tickets = [t for t in ask.load_tickets() if t.get("status") == "open"]
    if not tickets:
        print("No open tickets. (Nice.)")
        return []
    tickets.sort(key=lambda t: t.get("count", 1), reverse=True)
    print(f"{len(tickets)} open ticket(s), most-asked first:\n")
    for ticket in tickets:
        asked = f'asked x{ticket.get("count", 1)}'
        print(f'  [{ticket["id"]}] ({ticket["source"]}, {asked})  {ticket["reason"]}')
        print(f'      Q: {ticket["question"][:100]}')
    print()
    return tickets


def answer_loop():
    tickets = list_open()
    if not tickets:
        return
    print("Work through them now? For each: type your answer, or 's' to skip, or 'q' to quit.\n")
    for ticket in tickets:
        print("=" * 70)
        print(f'Ticket {ticket["id"]}  ({ticket["source"]}, asked x{ticket.get("count", 1)})')
        print(f'Question: {ticket["question"]}')
        notes = ticket.get("student_notes", [])
        if notes:
            print("\nWhat students said they didn't understand:")
            for note in notes:
                print(f'  - {note}')
        if ticket.get("shown_answer"):
            shown = ticket["shown_answer"]
            print(f'\nWhat the assistant had told the student:\n{shown[:600]}'
                  + ("..." if len(shown) > 600 else ""))
        print("-" * 70)
        print("Your answer (single line; press Enter to submit), or 's' / 'q':")
        reply = input("> ").strip()
        if reply.lower() == "q":
            print("Stopping.")
            break
        if reply.lower() == "s" or not reply:
            print("Skipped.\n")
            continue

        publish = input("Publish this to the knowledge base for future students? [Y/n]: ").strip().lower()
        do_publish = publish != "n"
        if do_publish and ticket["source"] == "rating":
            # gentle guard-rail reminder for homework-derived tickets
            print("  (Note: if this question was homework-related, the homework guard still")
            print("   protects active assignments even after publishing.)")
        result = ask.resolve_ticket(ticket["id"], reply, publish=do_publish)
        print(f"  -> {result}\n")

    print("Done. Published answers are live immediately for new questions.")
    print("They're also saved to instructor_answers.json, so they persist through")
    print("the next 'python ingest.py' + 'python build_index.py'.")


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "answer":
        answer_loop()
    else:
        list_open()
        print("Run 'python resolve_tickets.py answer' to respond to them.")


if __name__ == "__main__":
    main()

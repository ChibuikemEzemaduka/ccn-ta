"""
auth.py  —  student roster and authentication

Stores a roster of enrolled students in students.json (managed by the
instructor in the dashboard). Each student logs in with their RPI email and
a course password the instructor sets.

Passwords are stored as SHA-256 hashes -- not plaintext. Not as strong as
bcrypt, but it requires no extra install and is fine for a small-cohort
course tool that is not exposed to the open internet.

Usage (from student_chat.py):
    auth.render_login_gate()          # renders login or returns silently if
                                       # already authenticated; call st.stop()
                                       # after this if you want to gate the page
    info = auth.current_student()     # {'email', 'name', 'student_id'} or None
    auth.logout()                     # clear session

Usage (from instructor_dashboard.py):
    students = auth.load_roster()
    auth.save_roster(students)
    auth.add_student(name, email, password)
    auth.remove_student(email)
    auth.reset_password(email, new_password)
"""

import hashlib
import json
import secrets
from pathlib import Path

import streamlit as st


def _read_json(path, default=None):
    """Safe JSON reader — returns default ([] by default) if the file is
    missing, empty, or contains invalid JSON."""
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
    except json.JSONDecodeError as e:
        print(f"WARNING: {p.name} contains invalid JSON ({e}). Treating as empty.")
        return default

ROSTER_FILE = Path("students.json")
SESSION_KEY = "ccnta_student"


# ----------------------------- hashing ------------------------------------

def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _check_password(plain: str, stored_hash: str) -> bool:
    return _hash_password(plain) == stored_hash


# ----------------------------- roster -------------------------------------

def load_roster() -> list:
    """Return list of student dicts: {student_id, name, email, password_hash,
    active, created}."""
    if ROSTER_FILE.exists():
        return _read_json(ROSTER_FILE)
    return []


def save_roster(students: list):
    ROSTER_FILE.write_text(json.dumps(students, indent=2, ensure_ascii=False),
                           encoding="utf-8")


def add_student(name: str, email: str, password: str) -> dict:
    """Add a student to the roster. Returns the new record.
    Raises ValueError if the email already exists."""
    students = load_roster()
    email = email.strip().lower()
    if any(s["email"] == email for s in students):
        raise ValueError(f"A student with email {email} already exists.")
    record = {
        "student_id": f"s-{len(students) + 1}-{secrets.token_hex(4)}",
        "name": name.strip(),
        "email": email,
        "password_hash": _hash_password(password),
        "active": True,
        "created": _now(),
    }
    students.append(record)
    save_roster(students)
    return record


def remove_student(email: str):
    """Deactivate (soft-delete) a student. Keeps their history intact."""
    email = email.strip().lower()
    students = load_roster()
    for s in students:
        if s["email"] == email:
            s["active"] = False
    save_roster(students)


def reset_password(email: str, new_password: str):
    email = email.strip().lower()
    students = load_roster()
    for s in students:
        if s["email"] == email:
            s["password_hash"] = _hash_password(new_password)
    save_roster(students)


def _lookup(email: str) -> dict | None:
    email = email.strip().lower()
    for s in load_roster():
        if s["email"] == email and s.get("active", True):
            return s
    return None


# ----------------------------- session ------------------------------------

def authenticate(email: str, password: str) -> bool:
    """Try to log the student in. Returns True on success."""
    student = _lookup(email)
    if student and _check_password(password, student["password_hash"]):
        st.session_state[SESSION_KEY] = {
            "student_id": student["student_id"],
            "name": student["name"],
            "email": student["email"],
        }
        return True
    return False


def current_student() -> dict | None:
    """Return {student_id, name, email} for the logged-in student, or None."""
    return st.session_state.get(SESSION_KEY)


def logout():
    st.session_state.pop(SESSION_KEY, None)
    # clear any chat state too so the next student starts fresh
    for key in list(st.session_state.keys()):
        if key not in (SESSION_KEY,):
            st.session_state.pop(key, None)


def is_logged_in() -> bool:
    return SESSION_KEY in st.session_state


def require_login() -> bool:
    """Render the login form if the student is not authenticated. Returns True
    if the student is now logged in (either was already, or just logged in
    successfully). Returns False and renders the login form if not."""
    if is_logged_in():
        return True

    if not ROSTER_FILE.exists() or not load_roster():
        st.warning("The instructor hasn't set up the student roster yet. "
                   "Check back once enrollment is configured.")
        st.stop()

    st.markdown("### Sign in to the CCN AI Teaching Assistant")
    st.caption("Use your RPI email and the course password your instructor gave you.")
    email = st.text_input("RPI email", key="login_email", placeholder="you@rpi.edu")
    password = st.text_input("Course password", type="password", key="login_password")
    if st.button("Sign in", type="primary"):
        if authenticate(email, password):
            st.rerun()
        else:
            st.error("Email or password not recognised. "
                     "Check your RPI email address and try again, "
                     "or ask the instructor to reset your password.")
    st.stop()


# ----------------------------- replies ------------------------------------
# Instructor replies to escalated tickets are stored here so students can see
# them on their account when they next sign in.

REPLIES_FILE = Path("student_replies.json")


def load_replies() -> list:
    if REPLIES_FILE.exists():
        return _read_json(REPLIES_FILE)
    return []


def save_replies(replies: list):
    REPLIES_FILE.write_text(json.dumps(replies, indent=2, ensure_ascii=False),
                            encoding="utf-8")


def post_reply_to_student(student_id: str, ticket_id: str,
                           question: str, answer: str):
    """Post an instructor reply to a student's account. Only called when the
    instructor resolves a ticket that was raised non-anonymously."""
    replies = load_replies()
    replies.append({
        "id": f"r-{len(replies) + 1}",
        "student_id": student_id,
        "ticket_id": ticket_id,
        "question": question,
        "answer": answer,
        "time": _now(),
        "read": False,
    })
    save_replies(replies)


def get_student_replies(student_id: str) -> list:
    """All instructor replies for this student, newest first."""
    return sorted(
        [r for r in load_replies() if r["student_id"] == student_id],
        key=lambda r: r["time"], reverse=True,
    )


def mark_replies_read(student_id: str):
    replies = load_replies()
    for r in replies:
        if r["student_id"] == student_id:
            r["read"] = True
    save_replies(replies)


# ----------------------------- public board -------------------------------
# Publicised tickets are stored separately so the public class board page
# can display them without loading the full ticket store.

PUBLIC_BOARD_FILE = Path("public_board.json")


def load_public_board() -> list:
    if PUBLIC_BOARD_FILE.exists():
        return _read_json(PUBLIC_BOARD_FILE)
    return []


def publish_to_board(ticket_id: str, question: str, answer: str, topic: str = "",
                     subquestion: str = ""):
    """Publish a resolved ticket to the anonymous public class board.

    question:    the MAIN question (the original triggering question).
    subquestion: the student's follow-up note written when they escalated to
                 the teaching staff (the more specific thing they were stuck on).
                 Shown separately on the board so readers see both the general
                 topic and the specific point of confusion. May be empty."""
    board = load_public_board()
    board.insert(0, {
        "id": f"b-{len(board) + 1}",
        "ticket_id": ticket_id,
        "question": question,
        "subquestion": subquestion,
        "answer": answer,
        "topic": topic,
        "time": _now(),
    })
    PUBLIC_BOARD_FILE.write_text(json.dumps(board, indent=2, ensure_ascii=False),
                                 encoding="utf-8")


# ----------------------------- utilities ----------------------------------

def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def generate_temp_password(length: int = 10) -> str:
    """Generate a readable temporary password for new students."""
    import random
    words = ["packet", "switch", "router", "frame", "ack", "syn", "port",
             "queue", "latency", "throughput", "subnet", "gateway"]
    nums = "".join(str(random.randint(0, 9)) for _ in range(3))
    return random.choice(words) + nums

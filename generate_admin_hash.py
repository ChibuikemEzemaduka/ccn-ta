#!/usr/bin/env python3
"""
generate_admin_hash.py

Sets the Teaching Staff dashboard admin credentials.

The dashboard login checks an admin username and password. For security the
password is NEVER stored in plain text -- only a bcrypt hash of it is written to
the .env file. This script prompts for a username and password, generates the
hash, and writes ADMIN_USERNAME and ADMIN_PASSWORD_HASH into .env for you.

It updates those two lines in place if they already exist, appends them if they
do not, and leaves every other line in .env (such as OLLAMA_API_KEY) untouched.
A timestamped backup of the existing .env is made before writing.

Usage:
    python generate_admin_hash.py

Requires: bcrypt   (already a project dependency; otherwise: pip install bcrypt)
"""

import getpass
import os
import shutil
import sys
from datetime import datetime

try:
    import bcrypt
except ImportError:
    sys.exit("bcrypt is not installed. Activate your environment and run: "
             "pip install bcrypt")

# .env lives in the same directory as this script (the project root).
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def upsert_env(path, updates):
    """Update or insert KEY=value lines in an .env file without disturbing the
    rest of it. `updates` is a dict of {KEY: value}. Existing keys are replaced
    in place; missing keys are appended. Returns nothing; writes the file."""
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()

    remaining = dict(updates)  # keys still to write
    out = []
    for line in lines:
        stripped = line.strip()
        # Only touch real KEY=value lines, never comments or blanks.
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}\n")
                continue
        out.append(line if line.endswith("\n") else line + "\n")

    # Append any keys that weren't already present.
    if remaining:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        for key, value in remaining.items():
            out.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(out)


def main():
    print("Teaching Staff dashboard -- admin credential setup\n")

    username = input("Choose an admin username: ").strip()
    if not username:
        sys.exit("Username cannot be empty.")

    password = getpass.getpass("Choose an admin password: ")
    if len(password) < 8:
        sys.exit("Password should be at least 8 characters. Nothing was written.")
    confirm = getpass.getpass("Re-enter the password to confirm: ")
    if password != confirm:
        sys.exit("Passwords did not match. Nothing was written.")

    # bcrypt.gensalt() builds in a work factor, making the hash deliberately slow
    # to compute and therefore resistant to brute-force guessing.
    hash_str = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    # Back up an existing .env before modifying it, so nothing can be lost.
    if os.path.exists(ENV_PATH):
        backup = f"{ENV_PATH}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(ENV_PATH, backup)
        print(f"\nExisting .env backed up to: {os.path.basename(backup)}")

    upsert_env(ENV_PATH, {
        "ADMIN_USERNAME": username,
        "ADMIN_PASSWORD_HASH": hash_str,
    })

    print(f"\nDone. ADMIN_USERNAME and ADMIN_PASSWORD_HASH were written to {ENV_PATH}")
    print("All other entries in .env were left unchanged.")
    print("Restart the dashboard for the new credentials to take effect.")
    print("\nKeep .env private and never commit it. To change the password "
          "later, run this script again.")


if __name__ == "__main__":
    main()

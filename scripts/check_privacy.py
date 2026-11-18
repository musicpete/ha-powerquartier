#!/usr/bin/env python3
"""Fail if tracked files contain data that looks like real personal data.

Secret scanners (gitleaks, GitHub push protection) find credentials, not
personal data. This check covers what they miss for this project: e-mail
addresses, IBANs, Austrian points of delivery (Zählpunkte), phone numbers,
JWTs and UUIDs that are not obvious placeholders.

Test data policy (see CONTRIBUTING.md): fixtures use only synthetic values -
example.com addresses, all-zero identifiers, placeholder names such as
"Profil-A" or "Tarif A". Never copy values from a real account, not even
renamed ones.

A line can be exempted with the marker `privacy: allow` and a reason.

Usage: python scripts/check_privacy.py [files...]   (default: git ls-files)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "users.noreply.github.com",
}

EMAIL = re.compile(r"\b[\w.+*-]+@((?:[\w-]+\.)+[A-Za-z]{2,})\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[0-9A-Z]{4}){3,7}\b")
# Austrian point of delivery: AT + 31 digits/characters. Placeholders are
# all zeros except for a short counter at the end.
POD = re.compile(r"\bAT[0-9A-Z]{31}\b")
POD_PLACEHOLDER = re.compile(r"^AT0{25,}[0-9]{1,6}$")
PHONE = re.compile(r"(?<![\w.])\+(?:43|49|41)[ \d/-]{6,}\d")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip"}
SELF = Path(__file__).resolve()


def _uuid_is_placeholder(value: str) -> bool:
    # e.g. aaaaaaaa-0000-0000-0000-000000000001 or 00000000-1111-2222-...
    groups = value.lower().split("-")
    return all(len(set(g)) <= 2 for g in groups)


def check_line(line: str) -> list[str]:
    if "privacy: allow" in line:
        return []
    problems = []
    for m in EMAIL.finditer(line):
        domain = m.group(1).lower()
        if domain not in ALLOWED_EMAIL_DOMAINS and not domain.endswith(".example"):
            problems.append(f"e-mail address outside example domains: {m.group(0)}")
    for m in IBAN.finditer(line):
        if set(m.group(0)[4:].replace(" ", "")) - {"0"}:  # all-zero = placeholder
            problems.append("IBAN-like number")
    for m in POD.finditer(line):
        if not POD_PLACEHOLDER.match(m.group(0)):
            problems.append("point of delivery (Zählpunkt) that is not an all-zero placeholder")
    if PHONE.search(line):
        problems.append("phone number")
    if JWT.search(line):
        problems.append("JWT (use a generated test token instead)")
    for m in UUID.finditer(line):
        if not _uuid_is_placeholder(m.group(0)):
            problems.append(f"UUID that is not an obvious placeholder: {m.group(0)}")
    return problems


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [Path(p) for p in out.decode().split("\0") if p]


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] if argv else tracked_files()
    failures = 0
    for path in files:
        if path.suffix.lower() in SKIP_SUFFIXES or path.resolve() == SELF or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for problem in check_line(line):
                failures += 1
                print(f"{path}:{no}: {problem}")
    if failures:
        print(f"\n{failures} possible personal-data finding(s). Replace them with synthetic values.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

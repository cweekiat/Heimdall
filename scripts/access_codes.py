"""Manage hosted-mode access codes (task 062). Codes are shown once and stored only as hashes.

  uv run python scripts/access_codes.py add alice            # generated code, printed once
  uv run python scripts/access_codes.py add wk --admin --custom  # own code, typed hidden
  uv run python scripts/access_codes.py list
  uv run python scripts/access_codes.py revoke alice

Re-running `add` for an existing name replaces the code and signs that person out.
A running server picks up changes at once (the file is re-read when it changes).
Secrets dir: data/secrets (override HEIMDALL_SECRETS_DIR).
"""

from __future__ import annotations

import argparse
import getpass
import sys

from heimdall.adapters.access_file import FileAccessStore
from heimdall.api.access import secrets_dir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="issue or replace a person's code")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true", help="may change API keys and sources")
    a.add_argument("--custom", action="store_true", help="type your own code (min 12 chars)")
    sub.add_parser("list", help="list people (never codes)")
    r = sub.add_parser("revoke", help="disable a code and sign that person out")
    r.add_argument("name")
    args = p.parse_args(argv)
    store = FileAccessStore(secrets_dir() / "access.json")

    if args.cmd == "add":
        code = None
        if args.custom:
            code = getpass.getpass("Access code: ")
            if code != getpass.getpass("Repeat: "):
                print("codes differ", file=sys.stderr)
                return 1
        try:
            issued = store.issue(args.name, admin=args.admin, code=code)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        role = "admin" if args.admin else "analyst"
        if args.custom:
            print(f"{args.name} ({role}): custom code saved.")
        else:
            print(
                f"{args.name} ({role}) access code (shown once, send it privately):\n\n  {issued}\n"
            )
        return 0
    if args.cmd == "list":
        for u in store.list():
            flags = ("admin " if u["admin"] else "") + ("REVOKED" if u["revoked"] else "active")
            print(f"{u['name']:<24} {flags:<14} {u['created']}")
        return 0
    if not store.revoke(args.name):
        print(f"no such person: {args.name}", file=sys.stderr)
        return 1
    print(f"{args.name}: revoked, signed out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

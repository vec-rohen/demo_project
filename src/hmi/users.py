"""Manage HMI accounts from the command line (e.g. to recover a lost password).

    python -m src.hmi.users list
    python -m src.hmi.users add alice --role operator
    python -m src.hmi.users passwd alice
    python -m src.hmi.users role alice engineer
    python -m src.hmi.users enable|disable alice
    python -m src.hmi.users delete alice
"""

import argparse
import getpass
import os
import sys
from datetime import datetime

from .auth import ROLES, AuthError, UserStore
from .store import Store


def _ask_password() -> str:
    first = getpass.getpass("New password: ")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage HMI user accounts")
    parser.add_argument("--db", default=os.environ.get("HMI_DB", "data/edge.db"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("username")
    add.add_argument("--role", choices=ROLES, default="operator")
    add.add_argument("--name", default="")
    for cmd in ("passwd", "delete", "enable", "disable"):
        sub.add_parser(cmd).add_argument("username")
    role = sub.add_parser("role")
    role.add_argument("username")
    role.add_argument("role", choices=ROLES)
    args = parser.parse_args()

    store = UserStore(Store(args.db))
    try:
        if args.cmd == "list":
            for u in sorted(store.users.values(), key=lambda u: u.username):
                last = datetime.fromtimestamp(u.last_login).strftime("%Y-%m-%d %H:%M") if u.last_login else "never"
                flags = " (disabled)" if u.disabled else ""
                print(f"{u.username:<20} {u.role:<9} last login {last}{flags}")
        elif args.cmd == "add":
            store.add(args.username, _ask_password(), args.role, args.name)
            print(f"Created {args.username}; they will be asked to change the password at first sign-in")
        elif args.cmd == "passwd":
            store.update(args.username, password=_ask_password())
            print(f"Password reset for {args.username}")
        elif args.cmd == "role":
            store.update(args.username, role=args.role)
        elif args.cmd in ("enable", "disable"):
            store.update(args.username, disabled=args.cmd == "disable")
        elif args.cmd == "delete":
            store.delete(args.username)
    except AuthError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()

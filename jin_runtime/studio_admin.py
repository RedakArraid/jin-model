"""Interactive administration CLI for the first JIN Studio account."""
from __future__ import annotations

import argparse
import getpass
import json

from jin_runtime.studio_security import ROLE_PERMISSIONS, StudioSecurityError
from jin_runtime.studio_store import StudioConflictError, StudioStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Administrate JIN Studio accounts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-user", help="Create a Studio user")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument(
        "--role",
        action="append",
        dest="roles",
        choices=sorted(ROLE_PERMISSIONS),
        default=[],
        help="Repeat this option to grant several roles",
    )
    subparsers.add_parser("list-users", help="List Studio users")
    return parser


def main() -> int:
    args = _parser().parse_args()
    store = StudioStore()
    if args.command == "list-users":
        print(json.dumps(store.list_users(), ensure_ascii=False, indent=2))
        return 0

    password = getpass.getpass("Mot de passe : ")
    confirmation = getpass.getpass("Confirmez le mot de passe : ")
    if password != confirmation:
        raise SystemExit("Les mots de passe ne correspondent pas.")
    try:
        user = store.create_user(
            username=args.username,
            display_name=args.display_name,
            password=password,
            roles=args.roles or ["ADMIN"],
            created_by="studio-admin-cli",
        )
    except (StudioSecurityError, StudioConflictError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Compte créé : {user['username']} ({', '.join(user['roles'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

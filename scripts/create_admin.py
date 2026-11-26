"""Create an admin user, or promote an existing user to admin.

Usage:
    python -m scripts.create_admin --email admin@example.com --name "Admin"
The password is prompted for (never passed on the command line, where it would
end up in shell history). For automation set ADMIN_PASSWORD instead.
"""

import argparse
import getpass
import os
import sys

from pydantic import ValidationError

from app.core.security import hash_password
from app.database.connection import session_scope
from app.models import UserRole
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserCreate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Administrator")
    args = parser.parse_args()

    with session_scope() as db:
        repo = UserRepository(db)
        existing = repo.get_by_email(args.email)
        if existing:
            existing.role = UserRole.ADMIN
            print(f"Promoted existing user {existing.email} to admin.")
            return 0

        password = os.getenv("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
        try:
            data = UserCreate(name=args.name, email=args.email, password=password)
        except ValidationError as exc:
            print(f"Invalid input: {exc}", file=sys.stderr)
            return 1
        repo.create(
            name=data.name, email=data.email, hashed_password=hash_password(data.password), role=UserRole.ADMIN
        )
        print(f"Created admin {data.email}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

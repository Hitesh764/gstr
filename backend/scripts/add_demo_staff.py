"""
Adds a manager, an associate and a partner to the demo firm, without resetting anything.

    seed.py with SEED_EXTRA_STAFF=1 does the same but wipes the database first.
    This script only adds the missing people, so data you have uploaded stays.

The approval chain needs several people: whoever drafts a notice response can
never approve it, so trying it out takes at least two accounts.

Run: python scripts/add_demo_staff.py

All accounts get the demo password. Development only.
"""
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app import models  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.security import hash_password  # noqa: E402

DEMO_OWNER = "demo@sharma-ca.in"
DEMO_PASSWORD = "demo-password-123"

STAFF = [
    ("Rohit Desai", "manager@sharma-ca.in", models.UserRole.MANAGER),
    ("Priya Nair", "associate@sharma-ca.in", models.UserRole.ASSOCIATE),
    ("Vikram Shah", "partner@sharma-ca.in", models.UserRole.PARTNER),
]


def main() -> None:
    if settings.environment != "development":
        sys.exit("This script creates accounts with a known password and only runs "
                 "when ENVIRONMENT=development.")
    with SessionLocal() as db:
        owner = db.query(models.User).filter(models.User.email == DEMO_OWNER).one_or_none()
        if owner is None:
            sys.exit(f"No account {DEMO_OWNER} found. Run scripts/seed.py first.")

        created = []
        for name, email, role in STAFF:
            if db.query(models.User).filter(models.User.email == email).first():
                continue
            db.add(models.User(firm_id=owner.firm_id, name=name, email=email, role=role,
                               password_hash=hash_password(DEMO_PASSWORD), email_verified=True))
            created.append(email)
        db.commit()

    print("Created: " + (", ".join(created) if created else "nothing (all already exist)"))
    print(f"\nSign in with any of these, password {DEMO_PASSWORD}:")
    print(f"  {DEMO_OWNER:<26} Owner      can approve notice responses")
    for name, email, role in STAFF:
        print(f"  {email:<26} {role.value.title():<10} {name}")


if __name__ == "__main__":
    main()

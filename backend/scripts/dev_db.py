"""Start (or reuse) the no-Docker local development database.

Boots a user-space PostgreSQL via pgserver on ./.local-postgres, migrates to
head, and prints the PO_DATABASE_URL to export. The server keeps running after
this script exits; re-running is idempotent.

Usage:
    uv run python scripts/dev_db.py           # start + migrate, print URL
    uv run python scripts/dev_db.py --stop    # stop the server
"""

from __future__ import annotations

import sys
from pathlib import Path

import pgserver

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Outside the repo on purpose: pgserver passes the socket path unquoted to
# pg_ctl, so paths containing spaces (or >104-char socket paths) break.
DATA_DIR = Path.home() / ".local" / "share" / "procurement-optimizer" / "pgdata"


def main() -> None:
    if "--stop" in sys.argv:
        if DATA_DIR.exists():
            server = pgserver.get_server(str(DATA_DIR), cleanup_mode=None)
            server.stop()
            print("dev database stopped")
        return

    server = pgserver.get_server(str(DATA_DIR), cleanup_mode=None)
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)

    import os

    os.environ["PO_DATABASE_URL"] = url
    os.chdir(BACKEND_DIR)
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")

    print()
    print("dev database ready (survives this process; --stop to stop):")
    print(f'export PO_DATABASE_URL="{url}"')


if __name__ == "__main__":
    main()

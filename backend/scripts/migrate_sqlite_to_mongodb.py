"""Opt-in, repeatable import of an existing SQLite database into MongoDB."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from app.config import database_path, storage_backend
from app.database.mongo_reviews import MongoReviewStore
from app.database.mongo_connection import close_mongo_client, get_mongo_database


def migrate(source_path: Path, store: MongoReviewStore) -> dict[str, int]:
    """Copy persisted data from SQLite without changing or deleting its source."""
    source_path = source_path.expanduser().resolve(strict=True)
    uri = source_path.as_uri() + "?mode=ro"
    counts = {"reviews": 0, "saved_pull_requests": 0, "learning_attempts": 0}
    with closing(sqlite3.connect(uri, uri=True)) as source:
        source.row_factory = sqlite3.Row
        for row in source.execute("SELECT payload FROM reviews"):
            review: dict[str, Any] = json.loads(row["payload"])
            # The source database has no authenticated owner for its old records.
            store.save_review(review, "legacy:unassigned")
            counts["reviews"] += 1
        for row in source.execute("SELECT saved_id, url, created_at FROM saved_pull_requests"):
            store.import_saved_record(row["saved_id"], row["url"], row["created_at"])
            counts["saved_pull_requests"] += 1
        for row in source.execute("SELECT attempt_id, exercise_id, category, correct, created_at FROM learning_attempts"):
            store.import_learning_attempt(
                row["attempt_id"], row["exercise_id"], row["category"], bool(row["correct"]), row["created_at"]
            )
            counts["learning_attempts"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=database_path(), help="SQLite file to read (default: PR_MENTOR_DATABASE_PATH)")
    args = parser.parse_args()
    if storage_backend() != "mongodb":
        parser.error("Set PR_MENTOR_STORAGE=mongodb and configure MONGODB_URI before migrating.")
    try:
        store = MongoReviewStore(get_mongo_database(), ensure_indexes=False)
        counts = migrate(args.source, store)
        print("SQLite import complete (safe to repeat): " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        return 0
    finally:
        close_mongo_client()


if __name__ == "__main__":
    raise SystemExit(main())

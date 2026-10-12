"""Inspects and clears the summary cache.

The cache invalidates on a change to the patient's facts and deliberately not on
a change of model, so switching OLLAMA_MODEL leaves every stored summary in place
with the previous model's prose. That is the intended trade -- patient data moves
constantly, the model moves when an operator decides -- and this is the lever for
the second case:

    python -m scripts.cache_admin --stats
    python -m scripts.cache_admin --clear

Run from the repo root with SUMMARY_DB_URL set, or with the default in
app/config.py pointing at the published port.
"""

import argparse
import sys

import psycopg

from app import summary_cache
from app.config import settings

STATS_SQL = """
SELECT model, count(*) AS rows, round(avg(latency_ms)) AS avg_ms,
       min(updated_at) AS oldest, max(updated_at) AS newest
FROM summary_cache
GROUP BY model
ORDER BY rows DESC
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clear", action="store_true",
                        help="delete every cached summary (use after a model change)")
    parser.add_argument("--stats", action="store_true",
                        help="rows per model, mean generation latency, age range")
    args = parser.parse_args()

    if not (args.clear or args.stats):
        parser.error("nothing to do: pass --stats or --clear")

    if not summary_cache.enabled():
        print("summary cache disabled: SUMMARY_DB_URL is unset", file=sys.stderr)
        return 1

    print(f"cache: {settings.summary_db_url}")

    try:
        with psycopg.connect(settings.summary_db_url, autocommit=True) as conn:
            if args.stats:
                rows = conn.execute(STATS_SQL).fetchall()
                if not rows:
                    print("empty")
                for model, count, avg_ms, oldest, newest in rows:
                    print(f"  {model:14} {count:5d} rows  "
                          f"avg {avg_ms or 0:>6.0f} ms to generate  "
                          f"{oldest:%Y-%m-%d %H:%M} .. {newest:%Y-%m-%d %H:%M}")

            if args.clear:
                # TRUNCATE rather than DELETE: the table is a cache, so there is
                # nothing here worth a rollback and nothing to vacuum afterwards.
                before = conn.execute("SELECT count(*) FROM summary_cache").fetchone()[0]
                conn.execute("TRUNCATE summary_cache")
                print(f"cleared {before} cached summaries")
    except (psycopg.Error, OSError) as exc:
        print(f"cache unreachable: {exc!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

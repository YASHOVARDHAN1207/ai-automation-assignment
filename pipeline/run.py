"""CLI entry point: read 3 CSVs -> clean -> merge -> SQLite.

    python -m pipeline.run                 # rebuild db/consultbae.db
    python -m pipeline.run --summary       # rebuild and print a summary
    python -m pipeline.run --db /tmp/x.db  # write somewhere else
"""
import argparse
import os
import sys
from collections import Counter

from . import audit, config, extract, load, match, report
from .issues import IssueLog
from .normalize import ERROR, INFO, WARN


def run(db_path=None, verbose=True):
    log = IssueLog()

    records = extract.extract_all(log, db_path=db_path)
    people, conflicts, reviews, uf = match.resolve(records, log)
    # Dataset-level checks run after merging, because several of them (rate
    # distribution, match-key coverage, skill agreement between sources) are
    # only answerable once rows have been collapsed into people.
    audit.audit(people, records, log)
    run_id = load.load(records, people, conflicts, reviews, log, db_path=db_path)

    result = {
        "run_id": run_id,
        "rows_read": len(records),
        "rows_used": sum(1 for r in records if r.usable),
        "people": len(people),
        "issues": len(log),
        "conflicts": len(conflicts),
        "reviews": len(reviews),
        "log": log,
        "people_rows": people,
        "records": records,
        "conflict_rows": conflicts,
        "review_rows": reviews,
    }
    if verbose:
        _print_summary(result)
    return result


def _print_summary(result):
    log = result["log"]
    people = result["people_rows"]
    severity = log.counts_by_severity()

    print("")
    print("ConsultBae merge pipeline - run %s" % result["run_id"])
    print("=" * 62)
    print("  CSV rows read           : %d" % result["rows_read"])
    print("  CSV rows usable         : %d" % result["rows_used"])
    print("  people after merge      : %d" % result["people"])
    print("  rows collapsed away     : %d" % (result["rows_used"] - result["people"]))
    print("  data issues logged      : %d  (error %d / warn %d / info %d)" % (
        result["issues"], severity.get(ERROR, 0), severity.get(WARN, 0), severity.get(INFO, 0)))
    print("  field conflicts         : %d" % result["conflicts"])
    print("  merges left for a human : %d" % result["reviews"])

    by_source_count = Counter(p["source_count"] for p in people)
    print("")
    print("  people by number of source files they appear in")
    for count in sorted(by_source_count):
        print("    in %d file(s) : %d" % (count, by_source_count[count]))

    methods = Counter(p["match_methods"] for p in people)
    print("")
    print("  how each person was assembled")
    for method, count in methods.most_common():
        print("    %-42s %d" % (method, count))

    print("")
    print("  top data-issue categories")
    for category, count in log.counts_by_category().most_common(10):
        print("    %-32s %d" % (category, count))

    weak = [p for p in people if p["match_confidence"] < 1.0]
    if weak:
        print("")
        print("  merged on weak evidence (name + city, no shared key)")
        for person in weak:
            print("    #%-4d %-18s %-10s conf %.1f  %s" % (
                person["person_id"], person["full_name"], person["city"],
                person["match_confidence"], person["sources"]))

    print("")
    print("  database: %s" % (config.DB_PATH))
    print("")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Merge the three ConsultBae CSVs into one SQLite database")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="output SQLite path (default db/consultbae.db)")
    parser.add_argument("--quiet", action="store_true", help="suppress the summary")
    parser.add_argument("--report", action="store_true",
                        help="also export the Task 4 data-issues report")
    args = parser.parse_args(argv)

    result = run(db_path=args.db_path, verbose=not args.quiet)

    if args.report:
        print("  exporting the data-issues report")
        for path, count in report.export(db_path=args.db_path):
            print("    %-44s %s" % (os.path.relpath(path, config.ROOT),
                                    "" if count is None else "%d rows" % count))
        print("")
    errors = result["log"].counts_by_severity().get(ERROR, 0)
    # Errors are reported, not fatal: every one of them is a row or field the
    # pipeline consciously rejected, and the run is still a success.
    return 0 if result["people"] else 1


if __name__ == "__main__":
    sys.exit(main())

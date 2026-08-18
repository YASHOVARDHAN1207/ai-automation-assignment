"""Collector for every data-quality problem the pipeline notices.

This is the backing store for Task 4. The rule the pipeline follows is that a
problem is never handled silently: whatever the code decides to do about a bad
value, it records what the value was, what it did, and why.
"""
from collections import Counter, OrderedDict

from . import config
from .normalize import ERROR, INFO, WARN

SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


class IssueLog(object):
    def __init__(self):
        self.rows = []

    def add(self, category, description, action, severity=WARN,
            source=None, row_number=None, field=None, raw_value=None,
            entity=None):
        self.rows.append(OrderedDict([
            ("severity", severity),
            ("category", category),
            ("source", config.SOURCE_LABELS.get(source, source)),
            ("row_number", row_number),
            ("field", field),
            ("raw_value", None if raw_value is None else str(raw_value)),
            ("entity", entity),
            ("description", description),
            ("action_taken", action),
        ]))

    def extend(self, problems, source=None, row_number=None, entity=None):
        """Absorb the problem dicts produced by pipeline.normalize."""
        for p in problems:
            self.add(
                category=p["category"],
                description=p["description"],
                action=p["action"],
                severity=p["severity"],
                source=source,
                row_number=row_number,
                field=p.get("field"),
                raw_value=p.get("raw_value"),
                entity=entity,
            )

    # --- reporting helpers -------------------------------------------------

    def __len__(self):
        return len(self.rows)

    def sorted_rows(self):
        return sorted(self.rows, key=lambda r: (
            SEVERITY_ORDER.get(r["severity"], 9),
            r["category"],
            r["source"] or "",
            r["row_number"] if r["row_number"] is not None else -1,
        ))

    def counts_by_severity(self):
        return Counter(r["severity"] for r in self.rows)

    def counts_by_category(self):
        return Counter(r["category"] for r in self.rows)

    def grouped(self):
        """Group into (severity, category, field) buckets for the Markdown report."""
        buckets = OrderedDict()
        for row in self.sorted_rows():
            key = (row["severity"], row["category"], row["field"])
            bucket = buckets.setdefault(key, {
                "severity": row["severity"],
                "category": row["category"],
                "field": row["field"],
                "count": 0,
                "sources": Counter(),
                "examples": [],
                "actions": OrderedDict(),
            })
            bucket["count"] += 1
            if row["source"]:
                bucket["sources"][row["source"]] += 1
            if len(bucket["examples"]) < 4:
                bucket["examples"].append(row)
            bucket["actions"][row["action_taken"]] = True
        return buckets

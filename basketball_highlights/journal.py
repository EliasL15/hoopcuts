"""Transactional incremental state; full JSON is exported only at boundaries."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS detections (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contexts (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (sequence INTEGER PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.changed = {"detections": {}, "contexts": {}}
        self.request_offset = 0

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        changes = {table: dict(values) for table, values in self.changed.items()}
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.changed = changes
            raise

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def put(self, table, key, value):
        if table not in self.changed:
            raise ValueError("Invalid journal table")
        self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (key, json.dumps(value)))
        self.changed[table][key] = value

    def records(self, table):
        if table not in self.changed:
            raise ValueError("Invalid journal table")
        return {key: json.loads(value) for key, value in self.db.execute(f"SELECT key,value FROM {table} ORDER BY key")}

    def save_budget(self, data):
        # Only newly appended ledger entries and bounded scalar/pending metadata.
        with self.transaction():
            offset = self.meta("request_count_saved", 0)
            for sequence in range(offset, len(data["requests"])):
                self.db.execute("INSERT INTO requests VALUES (?,?)", (sequence, json.dumps(data["requests"][sequence])))
            self.set_meta("request_count_saved", len(data["requests"]))
            self.set_meta("budget", {key: value for key, value in data.items() if key != "requests"})

    def budget(self):
        data = self.meta("budget")
        if data is None:
            return None
        data["requests"] = [json.loads(row[0]) for row in self.db.execute("SELECT value FROM requests ORDER BY sequence")]
        return data

    def checkpoint(self, signature, errors, completed, compact=False):
        state = {"signature": signature, "errors": errors, "completed": completed,
                 "detections": self.records("detections") if compact else dict(self.changed["detections"]),
                 "contexts": self.records("contexts") if compact else dict(self.changed["contexts"]),
                 "journal_delta": not compact}
        budget = self.meta("budget")
        offset = 0 if compact else self.request_offset
        budget["requests"] = [json.loads(row[0]) for row in self.db.execute(
            "SELECT value FROM requests WHERE sequence>=? ORDER BY sequence", (offset,))]
        if not compact:
            budget["requests_offset"] = offset
        return state, budget

    def published(self):
        self.changed = {"detections": {}, "contexts": {}}
        self.request_offset = self.meta("request_count_saved", 0)

    def export(self, directory):
        state, budget = self.checkpoint(self.meta("signature"), self.meta("errors", []), 0, compact=True)
        state.pop("journal_delta")
        state.pop("completed")
        for name, payload in (("run_state.json", state), ("budget.json", budget)):
            path = directory / name
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload))
            temp.replace(path)

    def close(self):
        self.db.close()

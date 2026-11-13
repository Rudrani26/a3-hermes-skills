#!/usr/bin/env python3
"""deadline-tracker — a tiny, dependency-free task & deadline tracker for agents.

Usable two ways:
  * CLI (what the Hermes skill runs):  python deadlines.py add "Finish report" --due "+3d" --priority high
  * Library (import this file):        TaskStore(path, tz).add(...)

Storage: one SQLite file (default $HERMES_HOME/deadline-tracker/tasks.db, or
$DEADLINES_DB). Standard library only; Python 3.10+.

Due-date syntax: 2026-11-20 | 2026-11-20 17:00 | today | tomorrow | +3d | +6h |
monday..sunday (next occurrence) | "eod" (today 17:00). Date-only deadlines are
treated as due at 23:59 local time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PRIORITIES = ("low", "normal", "high", "urgent")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MAX_TITLE = 200
MAX_NOTES = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  due_utc TEXT,            -- ISO-8601 UTC, NULL = no deadline
  all_day INTEGER NOT NULL DEFAULT 0,
  priority TEXT NOT NULL DEFAULT 'normal',
  project TEXT,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done
  created_utc TEXT NOT NULL,
  completed_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_utc);
"""


class TaskError(ValueError):
    """Invalid input; message is safe to show to the user."""


@dataclass
class Task:
    id: int
    title: str
    due_utc: str | None
    all_day: bool
    priority: str
    project: str | None
    notes: str | None
    status: str
    created_utc: str
    completed_utc: str | None

    def due_local(self, tz: ZoneInfo) -> datetime | None:
        return datetime.fromisoformat(self.due_utc).astimezone(tz) if self.due_utc else None

    def to_dict(self, tz: ZoneInfo, now: datetime | None = None) -> dict:
        d = asdict(self)
        due = self.due_local(tz)
        d["due_local"] = due.strftime("%a %b %d %Y") + ("" if self.all_day else due.strftime(" %H:%M")) if due else None
        if due and self.status == "pending":
            delta = due - (now or datetime.now(tz))
            d["hours_left"] = round(delta.total_seconds() / 3600, 1)
            d["overdue"] = delta.total_seconds() < 0
        return d


def _clean(text: str | None, limit: int, field: str) -> str | None:
    if text is None:
        return None
    text = "".join(ch for ch in str(text) if ch == "\n" or ch >= " ").strip()  # drop control chars
    if len(text) > limit:
        raise TaskError(f"{field} too long (max {limit} chars)")
    return text or None


def parse_due(raw: str | None, tz: ZoneInfo, now: datetime | None = None) -> tuple[datetime | None, bool]:
    """Returns (aware local datetime, all_day)."""
    if raw is None or str(raw).strip() == "":
        return None, False
    s = str(raw).strip().lower()
    now = (now or datetime.now(tz)).astimezone(tz)
    end_of_day = time(23, 59)
    if s in ("today",):
        return datetime.combine(now.date(), end_of_day, tz), True
    if s == "eod":
        return datetime.combine(now.date(), time(17, 0), tz), False
    if s == "tomorrow":
        return datetime.combine(now.date() + timedelta(days=1), end_of_day, tz), True
    m = re.fullmatch(r"\+(\d{1,3})\s*([dhw])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "h":
            return now + timedelta(hours=n), False
        days = n * (7 if unit == "w" else 1)
        return datetime.combine(now.date() + timedelta(days=days), end_of_day, tz), True
    if s in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(s) - now.weekday()) % 7 or 7
        return datetime.combine(now.date() + timedelta(days=ahead), end_of_day, tz), True
    for fmt, all_day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%dT%H:%M", False), ("%Y-%m-%d", True)):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if all_day:
            return datetime.combine(dt.date(), end_of_day, tz), True
        return dt.replace(tzinfo=tz), False
    raise TaskError(f"unrecognised due date {raw!r}; use YYYY-MM-DD[ HH:MM], today, tomorrow, +3d, +6h or a weekday")


class TaskStore:
    def __init__(self, path: str | os.PathLike, tz: str = "UTC"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(tz)
        self._conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _row(self, r: sqlite3.Row) -> Task:
        return Task(**{k: r[k] for k in r.keys()} | {"all_day": bool(r["all_day"])})

    def add(self, title: str, due: str | None = None, priority: str = "normal",
            project: str | None = None, notes: str | None = None, now: datetime | None = None) -> Task:
        title = _clean(title, MAX_TITLE, "title")
        if not title:
            raise TaskError("title is required")
        priority = (priority or "normal").lower()
        if priority not in PRIORITIES:
            raise TaskError(f"priority must be one of {PRIORITIES}")
        due_dt, all_day = parse_due(due, self.tz, now)
        cur = self._conn.execute(
            "INSERT INTO tasks(title,due_utc,all_day,priority,project,notes,created_utc) VALUES (?,?,?,?,?,?,?)",
            (title, due_dt.astimezone(timezone.utc).isoformat(timespec="seconds") if due_dt else None,
             int(all_day), priority, _clean(project, 80, "project"), _clean(notes, MAX_NOTES, "notes"), self._utcnow()),
        )
        return self.get(cur.lastrowid)

    def get(self, task_id: int) -> Task:
        r = self._conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        if not r:
            raise TaskError(f"no task with id {task_id}")
        return self._row(r)

    def list(self, status: str = "pending", project: str | None = None, due_within_days: float | None = None,
             now: datetime | None = None) -> list[Task]:
        q, args = "SELECT * FROM tasks WHERE 1=1", []
        if status in ("pending", "done"):
            q += " AND status=?"
            args.append(status)
        if project:
            q += " AND project=?"
            args.append(project)
        if due_within_days is not None:
            limit = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) + timedelta(days=due_within_days)
            q += " AND due_utc IS NOT NULL AND due_utc <= ?"
            args.append(limit.isoformat(timespec="seconds"))
        q += " ORDER BY due_utc IS NULL, due_utc, CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, id"
        return [self._row(r) for r in self._conn.execute(q, args)]

    def complete(self, task_id: int) -> Task:
        t = self.get(task_id)
        if t.status == "done":
            return t
        self._conn.execute("UPDATE tasks SET status='done', completed_utc=? WHERE id=?", (self._utcnow(), t.id))
        return self.get(t.id)

    def reopen(self, task_id: int) -> Task:
        self.get(task_id)
        self._conn.execute("UPDATE tasks SET status='pending', completed_utc=NULL WHERE id=?", (int(task_id),))
        return self.get(task_id)

    def delete(self, task_id: int) -> None:
        self.get(task_id)
        self._conn.execute("DELETE FROM tasks WHERE id=?", (int(task_id),))

    def completed_between(self, start_utc: datetime, end_utc: datetime) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status='done' AND completed_utc >= ? AND completed_utc < ? ORDER BY completed_utc",
            (start_utc.isoformat(timespec="seconds"), end_utc.isoformat(timespec="seconds")))
        return [self._row(r) for r in rows]

    def reminders(self, within_days: float = 3, now: datetime | None = None) -> dict[str, list[Task]]:
        """Pending tasks bucketed into overdue / due_24h / due_soon (≤ within_days)."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        out: dict[str, list[Task]] = {"overdue": [], "due_24h": [], "due_soon": []}
        for t in self.list("pending", due_within_days=within_days, now=now):
            due = datetime.fromisoformat(t.due_utc)
            if due < now:
                out["overdue"].append(t)
            elif due <= now + timedelta(hours=24):
                out["due_24h"].append(t)
            else:
                out["due_soon"].append(t)
        return out


# ------------------------------------------------------------------------- CLI
def _default_db() -> Path:
    explicit = os.environ.get("DEADLINES_DB")  # .get() reads only: Hermes' skill scanner flags other env access
    if explicit:
        return Path(explicit)
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "deadline-tracker" / "tasks.db"


def _fmt(t: Task, tz: ZoneInfo) -> str:
    d = t.to_dict(tz)
    due = f" — due {d['due_local']}" if d["due_local"] else ""
    flag = " ⚠ OVERDUE" if d.get("overdue") else ""
    proj = f" [{t.project}]" if t.project else ""
    mark = "✓" if t.status == "done" else "•"
    return f"{mark} #{t.id} {t.title}{proj} ({t.priority}){due}{flag}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deadlines", description="Track tasks and deadlines.")
    p.add_argument("--db", default=str(_default_db()))
    p.add_argument("--tz", default=os.environ.get("DEADLINES_TZ") or os.environ.get("TZ") or "UTC")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("title"); a.add_argument("--due"); a.add_argument("--priority", default="normal")
    a.add_argument("--project"); a.add_argument("--notes")
    ls = sub.add_parser("list"); ls.add_argument("--status", default="pending", choices=["pending", "done", "all"])
    ls.add_argument("--project"); ls.add_argument("--due-within", type=float)
    for name in ("done", "reopen", "delete"):
        sp = sub.add_parser(name); sp.add_argument("id", type=int)
    r = sub.add_parser("remind"); r.add_argument("--within", type=float, default=3)
    args = p.parse_args(argv)

    try:
        store = TaskStore(args.db, args.tz)
    except Exception as e:  # zoneinfo / sqlite problems
        print(f"error: {e}", file=sys.stderr)
        return 2
    tz = store.tz
    try:
        if args.cmd == "add":
            t = store.add(args.title, args.due, args.priority, args.project, args.notes)
            out = t.to_dict(tz) if args.json else f"Added {_fmt(t, tz)}"
        elif args.cmd == "list":
            ts = store.list(args.status, args.project, args.due_within)
            out = [t.to_dict(tz) for t in ts] if args.json else ("\n".join(_fmt(t, tz) for t in ts) or "No tasks.")
        elif args.cmd in ("done", "reopen"):
            t = getattr(store, "complete" if args.cmd == "done" else "reopen")(args.id)
            out = t.to_dict(tz) if args.json else f"Updated {_fmt(t, tz)}"
        elif args.cmd == "delete":
            store.delete(args.id)
            out = {"deleted": args.id} if args.json else f"Deleted #{args.id}"
        else:  # remind
            b = store.reminders(args.within)
            if args.json:
                out = {k: [t.to_dict(tz) for t in v] for k, v in b.items()}
            else:
                lines = []
                for label, key in (("Overdue", "overdue"), ("Due within 24h", "due_24h"), (f"Due within {args.within:g} days", "due_soon")):
                    if b[key]:
                        lines.append(f"{label}:")
                        lines += ["  " + _fmt(t, tz) for t in b[key]]
                out = "\n".join(lines) or "Nothing due soon. 🎉"
    except TaskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

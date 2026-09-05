from __future__ import annotations

import json
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import ShotGroup, WorkflowTask


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, episode INTEGER NOT NULL, group_index INTEGER NOT NULL,
  prompt TEXT NOT NULL, references_json TEXT NOT NULL, model TEXT NOT NULL,
  duration_seconds INTEGER NOT NULL, aspect_ratio TEXT NOT NULL,
  duration_map_json TEXT NOT NULL DEFAULT '{}',
  prompt_hash TEXT,
  references_hash TEXT,
  submitted_account TEXT,
  quota_recorded INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, output_path TEXT, error TEXT, submitted_at TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  attempt_account TEXT, attempt_state TEXT, attempt_started_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
  label TEXT PRIMARY KEY, switch_order INTEGER NOT NULL, day TEXT NOT NULL,
  success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
  last_task_id TEXT, cooldown_until TEXT, availability_state TEXT NOT NULL DEFAULT 'unknown',
  availability_reason TEXT, verified_at TEXT, daily_limit INTEGER NOT NULL DEFAULT 3
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, category TEXT NOT NULL,
  message TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self._ensure_columns()
        self._reset_accounts_for_new_day()
        self.db.commit()

    def _ensure_columns(self) -> None:
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "duration_map_json" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN duration_map_json TEXT NOT NULL DEFAULT '{}'")
        if "submitted_at" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN submitted_at TEXT")
        if "prompt_hash" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN prompt_hash TEXT")
        if "references_hash" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN references_hash TEXT")
        if "submitted_account" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN submitted_account TEXT")
        if "quota_recorded" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN quota_recorded INTEGER NOT NULL DEFAULT 0")
        for column, definition in (("attempt_account", "TEXT"), ("attempt_state", "TEXT"), ("attempt_started_at", "TEXT")):
            if column not in columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        account_columns = {row[1] for row in self.db.execute("PRAGMA table_info(accounts)")}
        if "cooldown_until" not in account_columns:
            self.db.execute("ALTER TABLE accounts ADD COLUMN cooldown_until TEXT")
        for column, definition in (("availability_state", "TEXT NOT NULL DEFAULT 'unknown'"), ("availability_reason", "TEXT"), ("verified_at", "TEXT"), ("daily_limit", "INTEGER NOT NULL DEFAULT 3")):
            if column not in account_columns:
                self.db.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
        rows = self.db.execute("SELECT task_id,prompt,references_json FROM tasks WHERE prompt_hash IS NULL OR references_hash IS NULL").fetchall()
        for task_id, prompt, references_json in rows:
            try:
                references = json.loads(references_json)
            except (TypeError, json.JSONDecodeError):
                references = []
            self.db.execute(
                "UPDATE tasks SET prompt_hash=?,references_hash=? WHERE task_id=?",
                (
                    hashlib.sha256(str(prompt).encode("utf-8")).hexdigest(),
                    hashlib.sha256(json.dumps(sorted(map(str, references)), ensure_ascii=False).encode("utf-8")).hexdigest(),
                    task_id,
                ),
            )
        # Existing submitted records predate the timestamp column. Their
        # update time is the only durable lower bound for download recovery.
        self.db.execute("UPDATE tasks SET submitted_at=updated_at WHERE submitted_at IS NULL AND status IN ('submitted','download_pending','submitted_unconfirmed','completed')")
        self.db.execute("UPDATE tasks SET quota_recorded=1 WHERE status IN ('submitted','download_pending','completed')")
        self._backfill_submission_accounts()

    def _backfill_submission_accounts(self) -> None:
        """Bind legacy submitted tasks only when durable evidence is unique."""
        accounts = list(self.db.execute("SELECT label,last_task_id FROM accounts"))
        task_ids = [row[0] for row in self.db.execute(
            "SELECT task_id FROM tasks WHERE submitted_account IS NULL AND status IN ('submitted','download_pending','submitted_unconfirmed','completed')"
        )]
        for task_id in task_ids:
            messages = [row[0] for row in self.db.execute("SELECT message FROM events WHERE task_id=?", (task_id,))]
            candidates = {
                label for label, last_task_id in accounts
                if last_task_id == task_id or any(label in str(message) for message in messages)
            }
            if len(candidates) == 1:
                self.db.execute("UPDATE tasks SET submitted_account=? WHERE task_id=?", (candidates.pop(), task_id))

    def _reset_accounts_for_new_day(self) -> None:
        """Reset stale quota fences even when a day starts before prepare."""
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.db.execute(
            """UPDATE accounts
               SET day=?,success_count=0,failure_count=0,cooldown_until=NULL,
                   availability_state='unknown',availability_reason=NULL,verified_at=NULL
               WHERE day<>?""",
            (today, today),
        )

    def upsert_group(self, group: ShotGroup, prompt: str, references: list[str], model: str = "Seedance 2.0 Fast", duration_seconds: int = 10, aspect_ratio: str = "16:9", manual_reason: str | None = None) -> None:
        task = WorkflowTask(group.task_id, group.episode, group.index, prompt, references, model, duration_seconds, aspect_ratio, group.status, group.duration_map)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        references_hash = hashlib.sha256(json.dumps(sorted(map(str, references)), ensure_ascii=False).encode("utf-8")).hexdigest()
        self.db.execute(
            """INSERT INTO tasks(task_id,episode,group_index,prompt,references_json,model,duration_seconds,aspect_ratio,duration_map_json,prompt_hash,references_hash,status,error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET prompt=excluded.prompt,references_json=excluded.references_json,
               model=excluded.model,duration_seconds=excluded.duration_seconds,aspect_ratio=excluded.aspect_ratio,
               duration_map_json=excluded.duration_map_json,
               prompt_hash=excluded.prompt_hash,references_hash=excluded.references_hash,
               error=CASE WHEN tasks.status IN ('ready','manual','obsolete','paused') THEN excluded.error ELSE tasks.error END,
               status=CASE WHEN tasks.status IN ('ready','manual','obsolete','paused') THEN excluded.status ELSE tasks.status END""",
            (task.task_id, task.episode, task.group_index, task.prompt, json.dumps(task.references, ensure_ascii=False), task.model, task.duration_seconds, task.aspect_ratio, json.dumps(task.duration_map, ensure_ascii=False), prompt_hash, references_hash, task.status, manual_reason if task.status == "manual" else None),
        )
        self.db.commit()

    def sync_task_ids(self, task_ids: list[str]) -> None:
        """Mark tasks from an obsolete plan without deleting their audit history."""
        if not task_ids:
            raise ValueError("拒绝用空任务计划覆盖现有队列")
        placeholders = ",".join("?" for _ in task_ids)
        self.db.execute(f"UPDATE tasks SET status='obsolete',updated_at=CURRENT_TIMESTAMP WHERE task_id NOT IN ({placeholders})", task_ids)
        self.db.commit()

    def sync_accounts(self, accounts: list[dict], day: str) -> None:
        labels = [account["label"] for account in accounts]
        if labels:
            placeholders = ",".join("?" for _ in labels)
            self.db.execute(f"DELETE FROM accounts WHERE label NOT IN ({placeholders})", labels)
        for account in accounts:
            self.db.execute(
                """INSERT INTO accounts(label,switch_order,day,daily_limit) VALUES(?,?,?,?)
                   ON CONFLICT(label) DO UPDATE SET switch_order=excluded.switch_order,
                   success_count=CASE WHEN accounts.day<>excluded.day THEN 0 ELSE accounts.success_count END,
                   failure_count=CASE WHEN accounts.day<>excluded.day THEN 0 ELSE accounts.failure_count END,
                   cooldown_until=CASE WHEN accounts.day<>excluded.day THEN NULL ELSE accounts.cooldown_until END,
                   availability_state=CASE WHEN accounts.day<>excluded.day THEN 'unknown' ELSE accounts.availability_state END,
                   availability_reason=CASE WHEN accounts.day<>excluded.day THEN NULL ELSE accounts.availability_reason END,
                   verified_at=CASE WHEN accounts.day<>excluded.day THEN NULL ELSE accounts.verified_at END,
                   daily_limit=excluded.daily_limit,
                   day=excluded.day""",
                (account["label"], int(account["switch_order"]), day, int(account.get("daily_limit", 3))),
            )
        self.db.commit()

    def event(self, task_id: str | None, category: str, message: str) -> None:
        # Re-running prepare should not append identical audit noise forever.
        # Keep the first occurrence as durable evidence; state changes still
        # create distinct messages and remain fully auditable.
        self.db.execute(
            """INSERT INTO events(task_id,category,message)
               SELECT ?,?,?
               WHERE NOT EXISTS (
                 SELECT 1 FROM events WHERE task_id IS ? AND category=? AND message=?
               )""",
            (task_id, category, message, task_id, category, message),
        )
        self.db.commit()

    def tasks(self) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        return list(self.db.execute("SELECT * FROM tasks ORDER BY episode,group_index"))

    def next_ready(self) -> sqlite3.Row | None:
        self.db.row_factory = sqlite3.Row
        return self.db.execute("SELECT * FROM tasks WHERE status='ready' ORDER BY episode,group_index LIMIT 1").fetchone()

    def pending_tasks(self) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        return list(self.db.execute(
            """SELECT * FROM tasks WHERE status IN ('submitted','download_pending','submitted_unconfirmed')
               OR attempt_state IN ('click_pending','submitted_unconfirmed') ORDER BY episode,group_index"""
        ))

    def set_status(self, task_id: str, status: str, error: str | None = None, output_path: str | None = None) -> None:
        if output_path is None:
            self.db.execute("UPDATE tasks SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?", (status, error, task_id))
        else:
            self.db.execute("UPDATE tasks SET status=?,error=?,output_path=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?", (status, error, output_path, task_id))
        self.db.commit()

    def account_candidates(self, limit: int | None = None) -> list[sqlite3.Row]:
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.db.row_factory = sqlite3.Row
        rows = self.db.execute(
            """SELECT * FROM accounts WHERE day=? AND success_count < daily_limit
               AND availability_state NOT IN ('quota_exhausted','login_required','platform_blocked')
               AND (cooldown_until IS NULL OR cooldown_until <= datetime('now'))
               ORDER BY switch_order""", (today,)
        ).fetchall()
        return list(rows[:limit] if limit is not None else rows)

    def daily_success_count(self) -> int:
        """Return the number of confirmed submissions for the current account day."""
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        row = self.db.execute(
            "SELECT COALESCE(SUM(success_count), 0) FROM accounts WHERE day=?",
            (today,),
        ).fetchone()
        return int(row[0] or 0)

    def recovery_retry_count(self, task_id: str) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND category='recovery' AND message LIKE '%一次性重试%'", (task_id,)).fetchone()
        return int(row[0] or 0)

    def choose_account(self, limit: int = 3) -> str | None:
        rows = self.account_candidates()
        return rows[0]["label"] if rows else None

    def set_account_state(self, label: str, state: str, reason: str | None = None, cooldown_minutes: int | None = None) -> None:
        allowed = {"unknown", "verified", "quota_exhausted", "login_required", "ui_cooldown", "platform_blocked"}
        if state not in allowed:
            raise ValueError(f"未知账号状态：{state}")
        cooldown = None if cooldown_minutes is None else f"+{int(cooldown_minutes)} minutes"
        if cooldown:
            cursor = self.db.execute("UPDATE accounts SET availability_state=?,availability_reason=?,verified_at=CURRENT_TIMESTAMP,cooldown_until=datetime('now', ?) WHERE label=?", (state, reason, cooldown, label))
        else:
            cursor = self.db.execute("UPDATE accounts SET availability_state=?,availability_reason=?,verified_at=CURRENT_TIMESTAMP WHERE label=?", (state, reason, label))
        if cursor.rowcount != 1:
            self.db.rollback()
            raise ValueError(f"账号未登记：{label}")
        self.db.commit()

    def begin_submission_attempt(self, task_id: str, label: str, started_at: str | None = None) -> None:
        task = self.db.execute("SELECT status,submitted_account,quota_recorded,attempt_state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"任务不存在，拒绝建立提交栅栏：{task_id}")
        if task[1] and task[1] != label:
            raise ValueError(f"任务提交账号冲突：{task_id} 已绑定 {task[1]}")
        if task[2] or task[3] in {"click_pending", "submitted_unconfirmed"}:
            raise ValueError(f"任务已有提交栅栏：{task_id}")
        self.db.execute("UPDATE tasks SET attempt_account=?,attempt_state='click_pending',attempt_started_at=COALESCE(?,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE task_id=?", (label, started_at, task_id))
        self.db.commit()

    def clear_submission_attempt(self, task_id: str) -> None:
        self.db.execute("UPDATE tasks SET attempt_account=NULL,attempt_state=NULL,attempt_started_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND quota_recorded=0 AND status IN ('ready','submitted_unconfirmed')", (task_id,))
        self.db.commit()

    def record_account(self, label: str, task_id: str, success: bool) -> None:
        field = "success_count" if success else "failure_count"
        cooldown = None if success else "+5 minutes"
        if cooldown:
            cursor = self.db.execute(f"UPDATE accounts SET {field}={field}+1,last_task_id=?,cooldown_until=datetime('now', ?) WHERE label=?", (task_id, cooldown, label))
        else:
            cursor = self.db.execute(f"UPDATE accounts SET {field}={field}+1,last_task_id=?,cooldown_until=NULL WHERE label=?", (task_id, label))
        if cursor.rowcount != 1:
            self.db.rollback()
            raise ValueError(f"账号未登记，拒绝记录结果：{label}")
        self.db.commit()

    def record_submission(self, label: str, task_id: str, submitted_at: str | None = None) -> bool:
        """Record one confirmed quota use exactly once and bind its account."""
        if self.db.execute("SELECT 1 FROM accounts WHERE label=?", (label,)).fetchone() is None:
            raise ValueError(f"账号未登记，拒绝记额度：{label}")
        task = self.db.execute("SELECT submitted_account,quota_recorded FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"任务不存在，拒绝记额度：{task_id}")
        owner, quota_recorded = task[0], int(task[1] or 0)
        if owner and owner != label:
            raise ValueError(f"任务提交账号冲突：{task_id} 已绑定 {owner}，拒绝改为 {label}")
        if quota_recorded:
            if not owner:
                self.db.execute("UPDATE tasks SET submitted_account=? WHERE task_id=?", (label, task_id))
                self.db.commit()
            return False
        try:
            self.db.execute("UPDATE accounts SET success_count=success_count+1,last_task_id=?,availability_state='verified',availability_reason=NULL WHERE label=? AND success_count < daily_limit", (task_id, label))
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError(f"账号额度已达上限，拒绝记额度：{label}")
            self.db.execute(
                """UPDATE tasks SET status='submitted',submitted_account=?,quota_recorded=1,
                   submitted_at=COALESCE(?, submitted_at),attempt_account=NULL,attempt_state=NULL,attempt_started_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE task_id=?""",
                (label, submitted_at, task_id),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return True

    def revert_submission(self, label: str, task_id: str, reason: str) -> bool:
        """Undo a wrongly recorded quota when the platform reports a block."""
        if self.db.execute("SELECT 1 FROM accounts WHERE label=?", (label,)).fetchone() is None:
            raise ValueError(f"账号未登记，拒绝回滚额度：{label}")
        task = self.db.execute(
            "SELECT submitted_account, quota_recorded FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise ValueError(f"任务不存在，拒绝回滚额度：{task_id}")
        owner, quota_recorded = task[0], int(task[1] or 0)
        if owner != label:
            raise ValueError(f"任务提交账号不匹配，拒绝回滚额度：{task_id}")
        if not quota_recorded:
            return False
        cursor = self.db.execute(
            "UPDATE accounts SET success_count=MAX(success_count-1,0) WHERE label=?", (label,)
        )
        if cursor.rowcount != 1:
            self.db.rollback()
            raise ValueError(f"账号未登记，拒绝回滚额度：{label}")
        self.db.execute(
            """UPDATE tasks SET quota_recorded=0, submitted_account=NULL, submitted_at=NULL,
               attempt_account=NULL, attempt_state=NULL, attempt_started_at=NULL,
               status='moderation_blocked', error=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?""",
            (reason, task_id),
        )
        self.db.commit()
        return True

    def mark_submission_unconfirmed(self, label: str, task_id: str, error: str, submitted_at: str | None = None) -> None:
        """Preserve account ownership after a click whose quota result is unknown."""
        if self.db.execute("SELECT 1 FROM accounts WHERE label=?", (label,)).fetchone() is None:
            raise ValueError(f"账号未登记，拒绝记录未确认提交：{label}")
        task = self.db.execute("SELECT submitted_account FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"任务不存在，拒绝记录未确认提交：{task_id}")
        if task[0] and task[0] != label:
            raise ValueError(f"任务提交账号冲突：{task_id} 已绑定 {task[0]}，拒绝改为 {label}")
        self.db.execute(
            """UPDATE tasks SET status='submitted_unconfirmed',submitted_account=?,error=?,
               submitted_at=COALESCE(?, submitted_at),attempt_account=?,attempt_state='submitted_unconfirmed',attempt_started_at=COALESCE(attempt_started_at,?),updated_at=CURRENT_TIMESTAMP WHERE task_id=?""",
            (label, error, submitted_at, label, submitted_at, task_id),
        )
        self.db.commit()

#!/usr/bin/env python3
"""Local Kanban board with agent CLI commands and an optional stdio MCP server."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = ROOT / "data" / "board.sqlite3"
PORT = int(os.environ.get("AGENT_BOARD_PORT", "8765"))
LEASE_MINUTES = 120
STATUSES = {"backlog", "ready", "in_progress", "review", "blocked", "done"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                brief TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'backlog',
                priority TEXT NOT NULL DEFAULT 'normal',
                repo TEXT NOT NULL DEFAULT 'default',
                scope TEXT NOT NULL DEFAULT '[]',
                dependencies TEXT NOT NULL DEFAULT '[]',
                acceptance TEXT NOT NULL DEFAULT '[]',
                progress_note TEXT NOT NULL DEFAULT '',
                worktree TEXT NOT NULL DEFAULT '',
                agent_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        if "dependencies" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN dependencies TEXT NOT NULL DEFAULT '[]'")
        db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL,
                token TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('working', 'review')),
                expires_at TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                actor TEXT NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id DESC)")


def row_task(db: sqlite3.Connection, task_id: str) -> dict | None:
    row = db.execute("""
        SELECT t.*, c.id AS claim_id, c.agent_id AS claim_agent,
               c.state AS claim_state, c.expires_at AS claim_expires_at
        FROM tasks t LEFT JOIN claims c ON c.task_id = t.id
        WHERE t.id = ?
    """, (task_id,)).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["scope"] = json.loads(value["scope"])
    value["dependencies"] = json.loads(value["dependencies"])
    value["acceptance"] = json.loads(value["acceptance"])
    value["claim"] = None
    if value.pop("claim_id"):
        value["claim"] = {
            "agent_id": value.pop("claim_agent"),
            "state": value.pop("claim_state"),
            "expires_at": value.pop("claim_expires_at"),
        }
    else:
        value.pop("claim_agent")
        value.pop("claim_state")
        value.pop("claim_expires_at")
    return value


def add_event(db: sqlite3.Connection, task_id: str, actor: str, kind: str, message: str) -> None:
    db.execute(
        "INSERT INTO events(task_id, actor, kind, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, actor[:120], kind[:40], message[:2000], now_iso()),
    )


def parse_list(value, field: str, max_items: int = 50) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        result = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        result = [item.strip() for item in value if item.strip()]
    else:
        raise ValueError(f"{field} must be a list of text values")
    if len(result) > max_items or any(len(item) > 240 for item in result):
        raise ValueError(f"{field} is too long")
    return result


def clean_scope(paths) -> list[str]:
    scopes = parse_list(paths, "scope")
    cleaned = []
    for raw in scopes:
        path = raw.replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        if path.startswith("/") or ":" in path or any(part == ".." for part in path.split("/")):
            raise ValueError(f"Scope paths must be relative to the repository: {raw}")
        if any(char in path for char in "*?[]"):
            raise ValueError("Use exact paths or directory paths ending in /; wildcards are not supported")
        path = path.casefold()
        if path and path not in cleaned:
            cleaned.append(path)
    return cleaned


def clean_dependencies(values) -> list[str]:
    return list(dict.fromkeys(item.upper() for item in parse_list(values, "dependencies", max_items=100)))


def validate_dependencies(db: sqlite3.Connection, dependencies: list[str], task_id: str) -> None:
    if task_id in dependencies:
        raise ValueError("A task cannot depend on itself")
    if len(dependencies) > 100:
        raise ValueError("A task can have at most 100 prerequisites")
    all_tasks = {row["id"]: json.loads(row["dependencies"]) for row in db.execute("SELECT id, dependencies FROM tasks")}
    missing = [dependency for dependency in dependencies if dependency not in all_tasks]
    if missing:
        raise ValueError("Unknown prerequisite task ID: " + ", ".join(missing))
    for dependency in dependencies:
        pending = [dependency]
        seen = set()
        while pending:
            current = pending.pop()
            if current == task_id:
                raise ValueError("That prerequisite would create a dependency cycle")
            if current in seen:
                continue
            seen.add(current)
            pending.extend(all_tasks.get(current, []))


def paths_conflict(left: list[str], right: list[str]) -> bool:
    # An empty scope is an exclusive repository-wide lock.
    if not left or not right:
        return True
    for a in left:
        for b in right:
            if a == b:
                return True
            if a.endswith("/") and b.startswith(a):
                return True
            if b.endswith("/") and a.startswith(b):
                return True
    return False


def expire_claims(db: sqlite3.Connection) -> None:
    timestamp = now_iso()
    rows = db.execute(
        "SELECT task_id, agent_id FROM claims WHERE state = 'working' AND expires_at < ?", (timestamp,)
    ).fetchall()
    for row in rows:
        db.execute("DELETE FROM claims WHERE task_id = ?", (row["task_id"],))
        db.execute(
            "UPDATE tasks SET status = 'ready', agent_id = '', worktree = '', progress_note = 'Claim expired; task is ready again.', updated_at = ? WHERE id = ?",
            (timestamp, row["task_id"]),
        )
        add_event(db, row["task_id"], "Board", "claim_expired", f"Claim from {row['agent_id']} expired; task returned to Ready.")


def board_tasks() -> dict:
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        expire_claims(db)
        tasks = [row_task(db, row["id"]) for row in db.execute("SELECT id FROM tasks ORDER BY created_at DESC")]
        events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30")]
        db.commit()
    return {"tasks": tasks, "events": events, "server_time": now_iso(), "lease_minutes": LEASE_MINUTES}


def create_task(payload: dict, actor: str = "Board") -> dict:
    title = str(payload.get("title", "")).strip()
    if not title or len(title) > 180:
        raise ValueError("Add a title under 180 characters")
    status = payload.get("status", "backlog")
    if status not in STATUSES - {"in_progress", "review", "blocked", "done"}:
        raise ValueError("New tasks can start in Backlog or Ready")
    priority = payload.get("priority", "normal")
    if priority not in {"low", "normal", "high", "urgent"}:
        raise ValueError("Choose a valid priority")
    repo = str(payload.get("repo", "default")).strip()[:180] or "default"
    scope = clean_scope(payload.get("scope", []))
    dependencies = clean_dependencies(payload.get("dependencies", []))
    acceptance = parse_list(payload.get("acceptance", []), "acceptance")
    brief = str(payload.get("brief", "")).strip()[:12000]
    task_id = "AB-" + uuid.uuid4().hex[:6].upper()
    timestamp = now_iso()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        validate_dependencies(db, dependencies, task_id)
        db.execute("""
            INSERT INTO tasks(id, title, brief, status, priority, repo, scope, dependencies, acceptance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, title, brief, status, priority, repo, json.dumps(scope), json.dumps(dependencies), json.dumps(acceptance), timestamp, timestamp))
        add_event(db, task_id, actor, "created", "Task added to " + status.replace("_", " ").title())
        db.commit()
        return row_task(db, task_id)


def update_task(task_id: str, payload: dict) -> dict:
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        task = row_task(db, task_id)
        if not task:
            raise KeyError("Task not found")
        if task["claim"]:
            raise ValueError("This task is claimed or waiting for review. Accept or reopen it before changing task details.")
        fields = {}
        if "title" in payload:
            title = str(payload["title"]).strip()
            if not title or len(title) > 180:
                raise ValueError("Add a title under 180 characters")
            fields["title"] = title
        if "brief" in payload:
            fields["brief"] = str(payload["brief"]).strip()[:12000]
        if "repo" in payload:
            fields["repo"] = str(payload["repo"]).strip()[:180] or "default"
        if "scope" in payload:
            fields["scope"] = json.dumps(clean_scope(payload["scope"]))
        if "dependencies" in payload:
            dependencies = clean_dependencies(payload["dependencies"])
            validate_dependencies(db, dependencies, task_id)
            fields["dependencies"] = json.dumps(dependencies)
        if "acceptance" in payload:
            fields["acceptance"] = json.dumps(parse_list(payload["acceptance"], "acceptance"))
        if "priority" in payload:
            if payload["priority"] not in {"low", "normal", "high", "urgent"}:
                raise ValueError("Choose a valid priority")
            fields["priority"] = payload["priority"]
        if "status" in payload:
            status = payload["status"]
            if status not in STATUSES or status in {"in_progress", "review", "blocked"}:
                raise ValueError("Use the agent controls for In Progress, Review, and Blocked")
            if status == "done":
                db.execute("DELETE FROM claims WHERE task_id = ?", (task_id,))
            fields["status"] = status
        if fields:
            fields["updated_at"] = now_iso()
            assignment = ", ".join(f"{key} = ?" for key in fields)
            db.execute(f"UPDATE tasks SET {assignment} WHERE id = ?", (*fields.values(), task_id))
            if "status" in fields:
                add_event(db, task_id, "Board", "status", "Moved to " + fields["status"].replace("_", " ").title())
        db.commit()
        return row_task(db, task_id)


def review_task(task_id: str, action: str) -> dict:
    if action not in {"approve", "reopen"}:
        raise ValueError("Action must be approve or reopen")
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        task = row_task(db, task_id)
        if not task:
            raise KeyError("Task not found")
        if task["status"] != "review":
            raise ValueError("Only tasks in Review can be accepted or reopened")
        status = "done" if action == "approve" else "ready"
        db.execute("DELETE FROM claims WHERE task_id = ?", (task_id,))
        db.execute("UPDATE tasks SET status = ?, agent_id = '', worktree = '', updated_at = ? WHERE id = ?", (status, now_iso(), task_id))
        add_event(db, task_id, "Board", "review", "Accepted and marked Done." if action == "approve" else "Reopened and returned to Ready.")
        db.commit()
        return row_task(db, task_id)


def delete_task(task_id: str) -> None:
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        dependents = [row["id"] for row in db.execute("SELECT id, dependencies FROM tasks WHERE id != ?", (task_id,)) if task_id in json.loads(row["dependencies"])]
        if dependents:
            db.rollback()
            raise ValueError("Other tasks depend on this one: " + ", ".join(dependents))
        cur = db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if not cur.rowcount:
            raise KeyError("Task not found")
        db.commit()


def task_events(task_id: str) -> list[dict]:
    with connect_db() as db:
        if not db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            raise KeyError("Task not found")
        return [dict(row) for row in db.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id DESC LIMIT 100", (task_id,))]


def claim_task(task_id: str, agent_id: str, worktree: str = "") -> dict:
    agent_id = str(agent_id).strip()[:120]
    worktree = str(worktree).strip()[:1000]
    if not agent_id:
        raise ValueError("Pass a short, unique chat label as agent_id")
    if not worktree:
        raise ValueError("Pass this chat's unique worktree path as worktree")
    timestamp = now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES)).isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        expire_claims(db)
        task = row_task(db, task_id)
        if not task:
            db.rollback()
            raise KeyError("Task not found")
        if task["status"] != "ready":
            db.rollback()
            raise ValueError("This task is not Ready. Choose a Ready card from the board.")
        waiting_on = []
        for dependency_id in task["dependencies"]:
            dependency = db.execute("SELECT status FROM tasks WHERE id = ?", (dependency_id,)).fetchone()
            if not dependency or dependency["status"] != "done":
                waiting_on.append(dependency_id)
        if waiting_on:
            db.rollback()
            raise ValueError("Cannot claim yet. Waiting for prerequisite task(s) to reach Done: " + ", ".join(waiting_on))
        existing_worktree = db.execute("""
            SELECT t.id, t.title FROM tasks t JOIN claims c ON c.task_id = t.id
            WHERE t.worktree COLLATE NOCASE = ? AND (c.state = 'review' OR c.expires_at > ?)
        """, (worktree, timestamp)).fetchone()
        if existing_worktree:
            db.rollback()
            raise ValueError(f"This worktree already owns {existing_worktree['id']} ({existing_worktree['title']}). Use a fresh Codex worktree for each task.")
        active_claims = db.execute("""
            SELECT t.id, t.title, t.repo, t.scope, c.agent_id, c.state, c.expires_at
            FROM tasks t JOIN claims c ON c.task_id = t.id
            WHERE t.repo COLLATE NOCASE = ? AND (c.state = 'review' OR c.expires_at > ?)
        """, (task["repo"], timestamp)).fetchall()
        blockers = []
        for other in active_claims:
            if paths_conflict(task["scope"], json.loads(other["scope"])):
                blockers.append({"task_id": other["id"], "title": other["title"], "agent_id": other["agent_id"]})
        if blockers:
            db.rollback()
            names = ", ".join(f"{item['task_id']} ({item['agent_id']})" for item in blockers)
            raise ValueError("File scope is locked by " + names + ". Adjust the scope or claim another task.")
        db.execute("INSERT INTO claims(id, task_id, agent_id, token, state, expires_at, created_at, last_seen) VALUES (?, ?, ?, ?, 'working', ?, ?, ?)",
                   (uuid.uuid4().hex, task_id, agent_id, token, expires_at, timestamp, timestamp))
        db.execute("UPDATE tasks SET status = 'in_progress', agent_id = ?, worktree = ?, progress_note = '', updated_at = ? WHERE id = ?",
                   (agent_id, worktree, timestamp, task_id))
        add_event(db, task_id, agent_id, "claimed", f"Claimed for {LEASE_MINUTES} minutes." + (f" Worktree: {worktree}." if worktree else ""))
        db.commit()
        task = row_task(db, task_id)
    return {"task": task, "lease_token": token, "lease_expires_at": expires_at, "lease_minutes": LEASE_MINUTES}


def owned_claim(db: sqlite3.Connection, task_id: str, token: str) -> sqlite3.Row:
    claim = db.execute("SELECT * FROM claims WHERE task_id = ? AND token = ?", (task_id, token)).fetchone()
    if not claim:
        raise ValueError("Claim token is invalid or has been released")
    if claim["state"] == "working" and claim["expires_at"] < now_iso():
        db.execute("DELETE FROM claims WHERE task_id = ?", (task_id,))
        db.execute("UPDATE tasks SET status = 'ready', agent_id = '', worktree = '', updated_at = ? WHERE id = ?", (now_iso(), task_id))
        raise ValueError("Claim expired. Claim the task again if it is still Ready.")
    return claim


def heartbeat(task_id: str, token: str) -> dict:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES)).isoformat(timespec="seconds")
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        claim = owned_claim(db, task_id, token)
        if claim["state"] != "working":
            raise ValueError("Task is in Review; the board owner will release the file lock after review")
        db.execute("UPDATE claims SET expires_at = ?, last_seen = ? WHERE task_id = ?", (expires_at, now_iso(), task_id))
        db.commit()
    return {"task_id": task_id, "lease_expires_at": expires_at, "lease_minutes": LEASE_MINUTES}


def update_progress(task_id: str, token: str, note: str = "", status: str = "in_progress") -> dict:
    if status not in {"in_progress", "blocked"}:
        raise ValueError("Progress status must be in_progress or blocked; use complete when ready for review")
    note = str(note).strip()[:2000]
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        claim = owned_claim(db, task_id, token)
        if claim["state"] != "working":
            raise ValueError("Task is already in Review")
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES)).isoformat(timespec="seconds")
        db.execute("UPDATE claims SET expires_at = ?, last_seen = ? WHERE task_id = ?", (expires_at, now_iso(), task_id))
        db.execute("UPDATE tasks SET status = ?, progress_note = ?, updated_at = ? WHERE id = ?", (status, note, now_iso(), task_id))
        add_event(db, task_id, claim["agent_id"], status, note or ("Task is blocked." if status == "blocked" else "Work resumed."))
        db.commit()
        result = row_task(db, task_id)
    return {"task": result, "lease_expires_at": expires_at}


def complete_task(task_id: str, token: str, summary: str = "") -> dict:
    summary = str(summary).strip()[:2000]
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        claim = owned_claim(db, task_id, token)
        if claim["state"] != "working":
            raise ValueError("Task is already in Review")
        db.execute("UPDATE claims SET state = 'review', expires_at = NULL, last_seen = ? WHERE task_id = ?", (now_iso(), task_id))
        db.execute("UPDATE tasks SET status = 'review', progress_note = ?, updated_at = ? WHERE id = ?", (summary or "Ready for review.", now_iso(), task_id))
        add_event(db, task_id, claim["agent_id"], "review", summary or "Implementation is ready for review.")
        db.commit()
        return row_task(db, task_id)


def release_task(task_id: str, token: str, note: str = "") -> dict:
    note = str(note).strip()[:1000]
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        claim = owned_claim(db, task_id, token)
        agent_id = claim["agent_id"]
        db.execute("DELETE FROM claims WHERE task_id = ?", (task_id,))
        db.execute("UPDATE tasks SET status = 'ready', agent_id = '', worktree = '', progress_note = ?, updated_at = ? WHERE id = ?", (note or "Released by agent.", now_iso(), task_id))
        add_event(db, task_id, agent_id, "released", note or "Released and returned to Ready.")
        db.commit()
        return row_task(db, task_id)


def mcp_tools() -> list[dict]:
    """Tool definitions for the optional stdio MCP interface."""
    return [
        {"name": "board_list_tasks", "description": "List current board tasks and their statuses. Use this before claiming work.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "description": "Optional status filter: backlog, ready, in_progress, review, blocked, or done."}}, "additionalProperties": False}},
        {"name": "board_claim_task", "description": "Atomically claim one Ready task after every prerequisite is Done. The board blocks another live claim with an overlapping file scope in the same repository and prevents a worktree from owning multiple tasks. Pass a unique chat label and the full worktree path from git rev-parse --show-toplevel. Save the returned lease_token and pass it to every other task tool.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "agent_id": {"type": "string", "description": "A short, unique chat label shown on the board."}, "worktree": {"type": "string", "description": "The unique worktree path; get it with git rev-parse --show-toplevel."}}, "required": ["task_id", "agent_id", "worktree"], "additionalProperties": False}},
        {"name": "board_heartbeat", "description": "Extend your active claim by two hours. Call every hour during long work. Review claims stay locked until the board owner accepts or reopens the task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "lease_token": {"type": "string"}}, "required": ["task_id", "lease_token"], "additionalProperties": False}},
        {"name": "board_update_progress", "description": "Update the board card with a concise progress note or mark the task blocked. This renews your claim.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "lease_token": {"type": "string"}, "status": {"type": "string", "enum": ["in_progress", "blocked"]}, "note": {"type": "string"}}, "required": ["task_id", "lease_token", "status", "note"], "additionalProperties": False}},
        {"name": "board_complete_task", "description": "Mark your implementation ready for owner review. The file scope remains locked until the owner approves or reopens the card.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "lease_token": {"type": "string"}, "summary": {"type": "string"}}, "required": ["task_id", "lease_token", "summary"], "additionalProperties": False}},
        {"name": "board_release_task", "description": "Return your task to Ready and release its file scope when you cannot continue. Include a brief handoff note.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "lease_token": {"type": "string"}, "note": {"type": "string"}}, "required": ["task_id", "lease_token"], "additionalProperties": False}},
    ]


def mcp_call(name: str, arguments: dict):
    if name == "board_list_tasks":
        result = board_tasks()
        status = arguments.get("status")
        if status:
            if status not in STATUSES:
                raise ValueError("Unknown status")
            result["tasks"] = [task for task in result["tasks"] if task["status"] == status]
        return result
    if name == "board_claim_task":
        return claim_task(arguments.get("task_id", ""), arguments.get("agent_id", ""), arguments.get("worktree", ""))
    if name == "board_heartbeat":
        return heartbeat(arguments.get("task_id", ""), arguments.get("lease_token", ""))
    if name == "board_update_progress":
        return update_progress(arguments.get("task_id", ""), arguments.get("lease_token", ""), arguments.get("note", ""), arguments.get("status", "in_progress"))
    if name == "board_complete_task":
        return complete_task(arguments.get("task_id", ""), arguments.get("lease_token", ""), arguments.get("summary", ""))
    if name == "board_release_task":
        return release_task(arguments.get("task_id", ""), arguments.get("lease_token", ""), arguments.get("note", ""))
    raise KeyError("Unknown tool: " + name)


def mcp_response(request_id, result=None, error=None) -> dict:
    response = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def mcp_main() -> None:
    """Run the optional stdio MCP server when explicitly invoked."""
    init_db()
    for raw in sys.stdin:
        request_id = None
        try:
            request = json.loads(raw)
            method = request.get("method")
            request_id = request.get("id")
            params = request.get("params", {}) or {}
            result = None
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "local-agent-board", "version": "1.0.0"}}
            elif method in {"notifications/initialized", "notifications/cancelled"}:
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": mcp_tools()}
            elif method == "tools/call":
                tool_name = params.get("name", "")
                value = mcp_call(tool_name, params.get("arguments", {}) or {})
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif request_id is not None:
                print(json.dumps(mcp_response(request_id, error={"code": -32601, "message": "Method not found"})), flush=True)
                continue
            if request_id is not None:
                print(json.dumps(mcp_response(request_id, result=result), ensure_ascii=False), flush=True)
        except Exception as exc:
            if request_id is not None:
                print(json.dumps(mcp_response(request_id, error={"code": -32000, "message": str(exc)}), ensure_ascii=False, default=str), flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBoard/1.0"

    def log_message(self, fmt, *args):
        message = fmt % args
        if " /api/board HTTP/" not in message:
            print("[board] " + message, file=sys.stderr, flush=True)

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1_000_000:
            raise ValueError("Request body is too large")
        return json.loads(self.rfile.read(size).decode("utf-8") or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/board":
            return self.send_json(board_tasks())
        if path == "/api/info":
            return self.send_json({"server_path": str((ROOT / "server.py").resolve()).replace("\\", "/"), "db_path": str(DB_PATH)})
        if path.startswith("/api/tasks/") and path.endswith("/events"):
            task_id = unquote(path[len("/api/tasks/"):-len("/events")].strip("/"))
            try:
                return self.send_json({"events": task_events(task_id)})
            except KeyError as exc:
                return self.send_json({"error": str(exc)}, 404)
        relative = "index.html" if path == "/" else path.lstrip("/")
        requested = (STATIC / relative).resolve()
        try:
            requested.relative_to(STATIC.resolve())
        except ValueError:
            return self.send_json({"error": "Not found"}, 404)
        if not requested.is_file():
            return self.send_json({"error": "Not found"}, 404)
        content_type = "text/html; charset=utf-8" if requested.suffix == ".html" else "text/css; charset=utf-8" if requested.suffix == ".css" else "text/javascript; charset=utf-8" if requested.suffix == ".js" else "image/svg+xml" if requested.suffix == ".svg" else "application/octet-stream"
        body = requested.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/tasks":
                return self.send_json({"task": create_task(payload)}, 201)
            if path.startswith("/api/tasks/") and path.endswith("/review"):
                task_id = unquote(path[len("/api/tasks/"):-len("/review")].strip("/"))
                return self.send_json({"task": review_task(task_id, payload.get("action", ""))})
            if path.startswith("/api/tasks/"):
                task_id = unquote(path[len("/api/tasks/"):].strip("/"))
                with connect_db() as db:
                    if not db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                        raise KeyError("Task not found")
                    add_event(db, task_id, "Board", "comment", str(payload.get("message", "")).strip()[:2000])
                return self.send_json({"events": task_events(task_id)}, 201)
            return self.send_json({"error": "Not found"}, 404)
        except KeyError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/tasks/"):
            return self.send_json({"error": "Not found"}, 404)
        task_id = unquote(path[len("/api/tasks/"):].strip("/"))
        try:
            return self.send_json({"task": update_task(task_id, self.read_json())})
        except KeyError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/tasks/"):
            return self.send_json({"error": "Not found"}, 404)
        task_id = unquote(path[len("/api/tasks/"):].strip("/"))
        try:
            delete_task(task_id)
            return self.send_json({"ok": True})
        except KeyError as exc:
            return self.send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)

    def end_headers(self):
        super().end_headers()


def web_main() -> None:
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Agent Board is running at http://127.0.0.1:{PORT}", flush=True)
    print(f"Shared board database: {DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAgent Board stopped.", flush=True)
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", action="store_true", help="Run the local board interface")
    parser.add_argument("--mcp", action="store_true", help="Run the optional MCP server over stdio")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("web", help="Run the local board interface")
    listing = commands.add_parser("list", help="List board tasks as JSON")
    listing.add_argument("--status", choices=sorted(STATUSES), help="Filter by task status")
    claim = commands.add_parser("claim", help="Atomically claim a Ready task")
    claim.add_argument("task_id")
    claim.add_argument("--agent", required=True, help="Unique short label for this chat")
    claim.add_argument("--worktree", required=True, help="Dedicated worktree path for this chat")
    for name, help_text in (("heartbeat", "Renew a claim for two hours"), ("progress", "Update progress or mark the task blocked"), ("complete", "Submit the task for owner review"), ("release", "Release the task back to Ready")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("task_id")
        command.add_argument("--token", required=True, help="Lease token returned by claim")
        if name == "progress":
            command.add_argument("--note", default="")
            command.add_argument("--blocked", action="store_true")
        elif name == "complete":
            command.add_argument("--summary", default="")
        elif name == "release":
            command.add_argument("--note", default="")
    args = parser.parse_args()
    if args.mcp:
        mcp_main()
        return
    if args.web or args.command == "web":
        web_main()
        return
    if not args.command:
        parser.error("Choose --web, --mcp, or an agent command")
    try:
        init_db()
        if args.command == "list":
            result = board_tasks()
            if args.status:
                result["tasks"] = [task for task in result["tasks"] if task["status"] == args.status]
        elif args.command == "claim":
            result = claim_task(args.task_id, args.agent, args.worktree)
        elif args.command == "heartbeat":
            result = heartbeat(args.task_id, args.token)
        elif args.command == "progress":
            result = update_progress(args.task_id, args.token, args.note, "blocked" if args.blocked else "in_progress")
        elif args.command == "complete":
            result = complete_task(args.task_id, args.token, args.summary)
        elif args.command == "release":
            result = release_task(args.task_id, args.token, args.note)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

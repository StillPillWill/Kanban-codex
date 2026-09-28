# Agent Board

A local task board shared by independent Codex chats on this PC. It uses Python's standard library and a SQLite database in `data/board.sqlite3`.

## Start the board

Run `Start-AgentBoard.ps1`. It starts the local service in the background and opens `http://127.0.0.1:8765` in your browser. The service only listens on this PC. Run `Stop-AgentBoard.ps1` when you want to stop it.

Keep this folder in the same place after connecting Codex; the MCP configuration points to `server.py` here.

## Connect Codex once

The Agent Board MCP server has already been added to Codex's global config on this PC. Restart Codex once to load it, then type `/mcp` in a chat to confirm the board tools are listed. The **Connect Codex** button shows the config entry in case you need to connect another PC or restore the setup.

Restart Codex after adding the server. On this PC, the same MCP configuration is used by Codex desktop, CLI, and IDE chats. New chats can then read and claim tasks through the Agent Board tools.

## Keep agents from colliding

1. Put each independent change in its own task. Give parallel tasks the same repository label and separate file scopes, such as `src/ui/` and `src/api/`.
2. Use paths relative to the repository. A folder path must end with `/`. The board compares paths case-insensitively and blocks overlapping claims in the same repository.
3. If a task has no scope, it takes an exclusive lock on that whole repository. This is useful for work that could touch anything.
4. If a task depends on another, select that prerequisite in **Wait for tasks**. Agents cannot claim it until every prerequisite is marked **Done**. Moving a prerequisite out of Done blocks dependent claims again.
5. Start every Codex chat in a fresh worktree. On a Ready card, choose **Copy agent prompt** and send that prompt to the chat. The agent claims the card before editing and must keep work inside its scope. The board allows only one task per worktree.
6. A claim expires after two hours without a heartbeat. Long-running agents should call `board_heartbeat`. When an agent submits for review, its scope stays locked until you accept or reopen the card.
7. Review and merge worktrees yourself. The board prevents two chats from claiming the same task, unmet prerequisites, or overlapping declared scopes; separate worktrees isolate their actual edits until you review them.

## MCP tools

- `board_list_tasks`: inspect board cards.
- `board_claim_task`: atomically claim a Ready card and receive its lease token.
- `board_heartbeat`: extend the claim.
- `board_update_progress`: post a progress note or mark a task blocked.
- `board_complete_task`: move a task into Review while retaining its scope lock.
- `board_release_task`: return unfinished work to Ready and unlock it.

## Data and recovery

All task data and activity history live in `data/board.sqlite3`. Copy that file to back up the board. The MCP server and board interface use the same database, so changes appear across chats within a few seconds.

# Agent Board

A local task board shared by independent Codex chats on this PC. It uses Python's standard library and stores cards, claims, dependencies, and activity in `data/board.sqlite3`.

## Start the board

Run `Start-AgentBoard.ps1`. It starts the local web interface in the background and opens `http://127.0.0.1:8765`. The service listens only on this PC. Run `Stop-AgentBoard.ps1` when you want to stop it.

## Give work to Codex chats

1. Make one card per independent change. Describe the work, list the files or folders it may edit, and add prerequisites when it must wait for another task.
2. Use paths relative to the repository. A folder path ends in `/`. Matching scopes in the same repository cannot be claimed at the same time. An empty scope locks the entire repository.
3. Mark the card Ready when its brief is complete. For each task, start a separate Codex chat in a fresh worktree and paste the card's **Copy agent prompt**.
4. The prompt tells the chat to claim the card through the local command-line program before editing. The CLI and web board use the same SQLite database; claims are serialized and checked atomically.
5. Claims require every prerequisite task to be Done. The board checks this again when the claim is made, so a waiting card cannot be claimed by racing it from another chat.
6. Claims expire after two hours without a heartbeat. The prompt includes commands for renewing, updating, blocking, completing, or releasing a claim. A task in Review keeps its scope locked until you accept or reopen it.
7. Review and merge each worktree yourself. The board prevents overlapping declared scopes from being claimed together; separate worktrees isolate each chat's edits until review.

## Agent command line

The web interface provides task management. Agents use the same `server.py` file directly, without changing Codex configuration. Commands print JSON; a rejected claim exits with an error.

```powershell
python server.py list --status ready
python server.py claim AB-123456 --agent 'chat settings search' --worktree (git rev-parse --show-toplevel)
python server.py heartbeat AB-123456 --token '<lease-token>'
python server.py progress AB-123456 --token '<lease-token>' --note 'Updated the form'
python server.py progress AB-123456 --token '<lease-token>' --blocked --note 'Waiting for API contract'
python server.py complete AB-123456 --token '<lease-token>' --summary 'Implemented settings search'
python server.py release AB-123456 --token '<lease-token>' --note 'Handing off remaining work'
```

## Optional MCP server

The source also includes an optional stdio MCP server for users who choose to run it with an MCP client: `python server.py --mcp`. This project does not add that server to Codex configuration. The default board prompts use the local command-line interface.

## Data and recovery

Back up `data/board.sqlite3` to preserve tasks and their activity history. The database, logs, and process marker are ignored by Git.

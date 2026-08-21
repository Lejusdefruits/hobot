"""hobot's terminal UI -- a second interface onto the exact same engine
Discord talks to (core/queries.py, tools/*, graphs/chat_agent.py), same
reuse rule the old web dashboard followed: every pane here calls a real
function, never reimplements DB/business logic of its own. Launched by
cli.py, not part of the daemon process -- see tui/app.py's module docstring
for why."""

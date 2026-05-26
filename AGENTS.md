# mmux Agent Notes

This project is the prototype for a deterministic multi-agent pair programming
supervisor.

Core constraint: do not add an LLM-based referee. The supervisor may run shell,
tmux, git, tests, and SQLite policy checks, but it must not call a model to make
scheduling or safety decisions.

Implementation preferences:

- Keep the CLI standard-library-first while the control protocol is still moving.
- Keep tmux as an observable runtime, not as the authoritative state store.
- Store durable state in `.mmux/state.db`.
- Keep project-local generated state under `.mmux/`.
- Add model calls only inside explicit worker adapters, never inside supervisor
  policy checks.

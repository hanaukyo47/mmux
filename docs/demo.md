# mmux Demo Guide

This guide is for recording a short alpha demo. Keep the demo honest: show mmux
as an early deterministic supervisor, not a finished unattended coding product.

## Fast Smoke Demo

This repository includes a deterministic smoke demo that does not call Codex or
Claude Code. It exercises the same state machine with fake driver/reviewer
adapters, so it is safe for CI and quick recording:

```bash
cd /path/to/mmux
PYTHONPATH=src python3 scripts/demo_alpha.py
```

Expected shape:

```text
mmux alpha deterministic loop demo
task: #1 Change a small Python value

driver   codex  -> task #1 awaiting_review ...
reviewer claude -> task #1 awaiting_test review=approve ...
tester   claude -> task #1 completed ...

final task status: completed
main worktree src/app.py: value = 7
```

Use this for docs, smoke checks, and rehearsing the voiceover. Do not present it
as a real model run.

## Real Agent Demo

Use a small throwaway repository. The ideal feature is tiny, visible, and has a
testable outcome.

```bash
mkdir /tmp/mmux-real-demo
cd /tmp/mmux-real-demo
git init
git config user.email demo@example.com
git config user.name "mmux Demo"
mkdir src tests
printf 'def add(a, b):\n    return a + b\n' > src/calc.py
printf 'from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n' > tests/test_calc.py
printf '.mmux/\n' > .gitignore
git add .
git commit -m init

mmux doctor
mmux init . --task "Add one tiny, well-tested improvement"
mmux task add "Add subtract(a, b) with a focused test" --resource .
mmux run . --minutes 10 --execute-agents
```

For the more visual resident-agent shot:

```bash
mmux run . --minutes 10 --resident-agents --execute-agents
```

Record the tmux session, not only the final terminal output.

## 90 Second Storyboard

1. Open with the problem: multi-agent coding often lets models judge other
   models.
2. Show the mmux loop: driver writes, reviewer reviews, tester gates.
3. Start a timed run in tmux.
4. Show task state moving through `awaiting_review` and `awaiting_test`.
5. Show `mmux tasks` and the final git diff.
6. Close with the alpha caveat: this is for controlled experiments, not
   production unattended coding yet.

## Recording Notes

- Use a large terminal font and a clean shell prompt.
- Keep the repository tiny so the state transition is visible within minutes.
- Do not hide failures if they happen; showing tester/reviewer gates rejecting or
  bypassing work is more convincing than a perfect scripted run.
- If you record resident panes, mention that the visible tmux panes are for
  observability and human takeover; SQLite state is the source of truth.

## README Visual

The README currently uses `docs/assets/mmux-loop.svg` as the first-screen visual.
Replace it with a real `.webp` or `.gif` after recording:

```markdown
![mmux demo](docs/assets/mmux-demo.webp)
```

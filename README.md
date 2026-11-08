# pair-with

A skill that has two coding agents work one repo's backlog in parallel. Each builds items and each reviews what the other built. The reviewer fixes what it finds and merges, so nothing gets handed back. Vague GitHub issues are settled with you and split into backlog items first.

Start it from a Claude or Codex session in a herdr pane by naming the other agent, with a model and effort if you want them: `pair with codex astra high`.

To change what runs next, ask to pause and re-prioritize: the run finishes its current batch, you pick P0 and P1, and anything they depend on moves up with them. Add `watched` to start a third agent that keeps an unattended run moving and logs what happens. `pair.py report` shows who built and reviewed what, and where the time went.

- Skill: `skills/pair-with/SKILL.md`
- Coordination helper: `skills/pair-with/scripts/pair.py`
- Settings example: `skills/pair-with/references/settings-example.toml`
- Design: `docs/superpowers/specs/2026-11-02-backlog-duo-design.md`

## Install

```bash
ln -s ~/projects/pair-with/skills/pair-with ~/.claude/skills/pair-with
ln -s ~/projects/pair-with/skills/pair-with ~/.codex/skills/pair-with
```

## Test

```bash
python3 -m unittest discover -s tests
```

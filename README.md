# backlog-duo

A skill that has two coding agents work one repo's backlog in parallel. Each builds items and each reviews what the other built. The reviewer fixes what it finds and merges, so nothing gets handed back.

- Skill: `skills/backlog-duo/SKILL.md`
- Coordination helper: `skills/backlog-duo/scripts/duo.py`
- Settings example: `skills/backlog-duo/references/settings-example.toml`
- Design: `docs/superpowers/specs/2026-11-02-backlog-duo-design.md`

## Install

```bash
ln -s ~/projects/backlog-duo/skills/backlog-duo ~/.claude/skills/backlog-duo
ln -s ~/projects/backlog-duo/skills/backlog-duo ~/.codex/skills/backlog-duo
```

## Test

```bash
python3 -m unittest discover -s tests
```

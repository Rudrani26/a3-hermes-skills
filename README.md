# a3-hermes-skills

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill tap.

| Skill | What it does |
|---|---|
| [`deadline-tracker`](skills/deadline-tracker/SKILL.md) | Tasks and deadlines in a local SQLite file: natural due dates, done/reopen, overdue and due-soon reminders. Standard-library Python, no API keys. |

## Install

```bash
hermes skills tap add Rudrani26/a3-hermes-skills
hermes skills search deadline --source github
hermes skills install Rudrani26/a3-hermes-skills/deadline-tracker
```

Hermes runs its install-time security scan on the skill. The skill uses the `terminal`
toolset to run its bundled script.

## Try it without an agent

```bash
python3 skills/deadline-tracker/scripts/deadlines.py --db /tmp/demo.db --tz UTC add "Write report" --due +3d --priority high
python3 skills/deadline-tracker/scripts/deadlines.py --db /tmp/demo.db --tz UTC remind --within 7
```

Built for a CSCI 599 (Agentic AI) assignment. All examples use synthetic data.

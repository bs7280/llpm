---
id: TASK-002
type: task
title: 'model_tier frontmatter: templates + board/backlog display'
status: planned
priority: medium
effort: small
requires_human: false
parent: EPIC-001
blockers: []
created: '2026-07-04'
updated: '2026-07-04'
completed: null
tags:
- task-fabric
- tier-light
---
Add model_tier to ticket templates and surface it in board/backlog output. ABSTRACT tiers: heavy | standard | light (decided 2026-07-04; never concrete model names — dispatcher/harness maps tier->model in config). Optional field, no validation hard-fail on absence. See vault area.homelab.agent-platform.task-fabric.schema.
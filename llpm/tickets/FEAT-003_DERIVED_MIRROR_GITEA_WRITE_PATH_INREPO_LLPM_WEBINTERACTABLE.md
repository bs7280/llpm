---
id: FEAT-003
type: feature
title: Derived mirror + Gitea write path (in-repo llpm, web-interactable)
status: planned
priority: low
effort: null
parent: EPIC-001
blockers:
- FEAT-001
created: '2026-07-04'
updated: '2026-07-04'
completed: null
tags:
- task-fabric
---
Option B from the task-fabric llpm plan: repo stays sole source of truth; CI mirrors llpm/ -> vault stems on push; web writes commit via Gitea API and re-mirror. Opt-in per repo, for repos that keep in-repo llpm but want board/marginalia interactivity. See vault area.homelab.agent-platform.task-fabric.llpm-plan (Dual sync section).
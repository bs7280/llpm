---
id: TASK-001
type: task
title: 'Store discovery: in-repo pointer + resolution order'
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
---
Committed in-repo pointer (.llpm/config.toml or project.manifest extensions.llpm) naming store kind + root/stem. Resolution order: --docs-root flag > LLPM_DOCS_ROOT env (exists today; box spawn sets it) > in-repo pointer > legacy llpm/ dir presence > loud error with setup hint. Mirrored convention for agents lives on the repos.<name> vault note frontmatter. See vault area.homelab.agent-platform.task-fabric.ticketstore (Discovery section).
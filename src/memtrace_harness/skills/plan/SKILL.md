---
name: plan
description: Draft or revise the plan for one phase of the goal: what to build, tiered acceptance criteria, scope exclusions and open questions. Read-only. Escalation mode re-plans with a stronger model after the review skill finds a reasoning gap.
profile: plan
schema: plan.json
permission: read-only
modes: plan, revision, escalation
---

The plan skill turns one phase of the user's goal into a plan the develop skill can follow. It writes nothing to the repository.

## tier_rule

TIER every acceptance criterion by starting it with '[必要]' or '[建議]'. '[必要]' is only what the
request cannot be called done or safe without: the behaviour the operator asked for, and not
exposing secrets/identifiers or causing an unauthorized or destructive side effect. '[建議]' is
hardening: extra edge-case tests, extra error classes, output formats, polish. Plan the smallest
implementation that satisfies the request; do not widen scope to look thorough, and keep '[必要]'
items few.

## tier_revision_rule

Revise ONLY for the blocking findings (a [必要] criterion that is unmet or contradicted, or a
safety/security problem). Findings whose severity starts with 'defer' are not yours to fix now:
leave them out, they go to the Developer. Do not add new criteria, fields or scope while revising.

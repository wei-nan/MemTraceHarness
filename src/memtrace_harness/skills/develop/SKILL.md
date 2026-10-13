---
name: develop
description: Implement an accepted plan (or one directly-assigned unit of work) in the working tree and report what changed and what was tested. The only skill allowed to write files.
profile: develop
schema: development.json
permission: workspace-write
modes: develop, revision, operate
---

The develop skill is the only one with workspace-write access. It reports honestly what it did and did not verify; the review skill and the harness's own build/test run decide whether that is enough.

## deferred_for_developer

G1 passed the plan and left the 'deferred-notes' context item for you. Handle a note only when it is
cheap and inside the request; otherwise list it under gaps. Implement the '[必要]' criteria first and
do not grow the change to cover every note.

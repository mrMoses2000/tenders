---
name: code-change-verification
description: Verify changed repository code, configuration, and runtime behavior using checks relevant to the change.
---

# Code change verification

Review the changed diff for accidental scope and compatibility changes. Run the
repository's relevant lint, syntax, and requested behavior checks. For live
provider paths, verify the application boundary, persisted evidence, and service
health using a bounded read-only or authorized pilot. Report exact commands and
results; if a check cannot run, state the remaining gap rather than treating it
as passed.

---
name: implementation-strategy
description: Plan compatibility-sensitive runtime, API, schema, and build changes in this repository before editing them.
---

# Implementation strategy

Before editing, record the current entry point and data flow, the intended
behavior, and the smallest set of affected files. Check persisted data and
external callers before changing a contract. Prefer an additive change that
preserves old records and jobs; identify any migration before implementation.

For a provider workflow, specify which process owns network access, how source
evidence is validated and stored, and what happens on denial, timeout, duplicate
results, or partial success. Keep supplier contact discovery separate from
authorization to send messages. Choose observable verification for the changed
path and state any production limitation before calling the work complete.

# Safe GitHub recovery sync — 2026-10-18

## Repository facts

- Repository: [`mrMoses2000/tenders`](https://github.com/mrMoses2000/tenders), confirmed **public** on GitHub. Treat every pushed file as public.
- Origin: `git@github.com:mrMoses2000/tenders.git` for fetch and push.
- A read-only SSH check authenticated as the repository owner. `gh auth status` reports no GitHub CLI session; native SSH Git works.
- Local source checkout: `/Users/mosesvasilenko/tenders`, branch `codex/pipeline-v3-event-core` at `b4acafe`, tracking its same-named remote branch.
- That checkout contains a large dirty worktree (94 tracked modified files, numerous untracked code, migrations, tests, build output, screenshots and operational artifacts at the time of inspection). It must not be staged wholesale or used as the base of a push.
- Stable base selected for this recovery: `origin/main` at `7ea41c0` (`Refresh Mini App design and audit production readiness`). The main history also contains `3e1fb13` (`Record project handoff and remaining work`).

## Isolated docs branch

The review branch was created in a separate clean worktree, based on `origin/main`:

```text
/tmp/tenders-uranenergo-recovery
codex/uranenergo-pipeline-recovery-2026-10-18
base 7ea41c0
```

Only the new recovery docs under `docs/uranenergo-2026-10-18/` belong in its commit. The dirty source checkout remains untouched. Do not copy its uncommitted runtime changes, outputs, screenshots, build directories, `.env`, credential material, database dumps, raw Telegram files, contact details or case UUIDs into this branch.

## Repeatable sync procedure

1. In the source checkout, record `git status --short --branch`, `git diff --stat`, current branch and `git log -5 --oneline`; do not run reset, clean, stash-pop or checkout commands that change its files.
2. Fetch the intended remote base explicitly. Create a new worktree and recovery branch from that exact ref; inspect `git status --short --branch` and verify it starts clean.
3. Author only the reviewable files in the worktree. Stage explicit paths, never `git add -A` or `git add .`.
4. Review `git diff --cached --name-status`, `git diff --cached --check`, and the full staged patch. Confirm the staged set contains only the intended docs; scan it for credentials, tokens, private contact details, raw customer messages, database IDs/dumps and generated files.
5. Commit with a narrow message. Check the commit's parents/base and `git show --stat --oneline --decorate HEAD`.
6. Push the new branch by its exact name. Do not force-push or update `main` or the existing `codex/pipeline-v3-event-core` branch.
7. Review the pushed commit on GitHub. A PR can be opened separately after the owner reviews the public-facing document contents.

## Preserving the dirty pipeline work

The pipeline branch contains substantial implementation and production-context changes that are not part of this recovery documentation. Preserve it in place. Before any future transfer, make a path-level inventory and group work into small, independently reviewable commits. Copying a curated file list to a clean worktree is safer than merging this dirty tree. Where uncommitted code must be recovered, create a private local patch and hash manifest outside the repository, inspect every path, then apply selected hunks or files in an isolated worktree. Keep the patch and manifest out of GitHub and remove neither source checkout nor private backup until the user has independently reviewed the resulting history.

Never infer safety from `.gitignore`: the current rules cover `.env`, virtual environments, bytecode, var, logs and a few tool caches, but do not exclude all `build/`, `outputs/`, screenshots, `.playwright-*` data, every local release bundle or every newly created migration. Public visibility makes path allowlisting mandatory.

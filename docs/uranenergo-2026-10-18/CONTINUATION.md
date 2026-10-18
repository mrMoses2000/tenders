# Server Codex continuation — UranEnergo recovery

## Handoff handshake

This continuation is valid only against the pushed review branch below. The server agent must fetch it into a separate clean worktree in a dedicated sibling worktree outside the live checkout, then report the exact GitHub branch, remote HEAD commit and live server checkout HEAD it will inspect. It must verify that the dirty source checkout remains on its pre-existing branch and is not overwritten by this docs branch.

```text
repo: mrMoses2000/tenders (public)
branch: codex/uranenergo-pipeline-recovery-2026-10-18
base: origin/main at 7ea41c0
latest sanitized source checkpoint: 2f7832ce706ef2495b7cb127683def4abd5d7e80
prior server continuation handshake: 4baa2384fba95352ff955ec9a11cc94231452fe6
initial docs checkpoint commit: 191f4b8ea1b416cd660dae76e06f49cb297ff214
handoff branch tip: verify with `git ls-remote origin refs/heads/codex/uranenergo-pipeline-recovery-2026-10-18`; it must include this handshake commit
server checkout commit: report exact value before any action
requested model: GPT-6.1-SOL, xhigh reasoning (runtime capability must be checked by root)
```

The server continuation acknowledged the prior handoff in its isolated branch. It must now fetch and acknowledge the exact source checkpoint SHA above in a separate clean review worktree. See [server-continuation-ack.md](server-continuation-ack.md) for its last reported status.

Do not continue if the branch or commit differs from the handoff or the expected documents are missing. Report the mismatch before changing anything.

## Objective

Recover the current UranEnergo case through the already deployed pipeline and Mini App, preserving its supplier history, canonical tables and existing service boundaries. Verify current-versus-legacy case identity from the live source before treating the historical 55-item count as the current baseline. Validate Telegram PDF/DOCX intake; allow the current pipeline to process for the user-authorized four-hour unattended window; reconcile extracted facts to the existing case; continue supplier discovery and dialogue in Taraz → Shymkent → Almaty order where current provider gates allow; and produce a useful, evidence-backed shortlist in the existing Mini App/report.

No new WAHA send is permitted until a root-reviewed account-binding design durably pins the intended account identity to each action and compares the live self identity to that pin immediately before sending. A current self-identity check does not establish the account used for historical sends. Preserve unknown-acceptance records and never replay an uncertain send.

The recovery branch includes a curated sanitized source checkpoint, but it is **not a release or independently deployable server build**. It omits operator-specific scripts, customer fixtures and some PostgreSQL/external test paths; see the [verification baseline](verification-baseline.md). The live server may run a third source state. Do not deploy from this branch based only on local tests. The server runtime agent must establish the actual deployed state with read-only commands and secret-safe output.

## Server work sequence

1. **Isolated source review:** fetch the review branch into a separate clean worktree and confirm the exact remote SHA above. Report that branch/commit alongside the live checkout branch/commit and worktree status. Never pull, reset or checkout this branch over the dirty live checkout. Root has authorized isolated city-policy and structured-TEXT-provider implementation, tests, and curated branch sync; this does not authorize production DB writes, outbound sends, live authentication changes, service changes, deployment, or the four-hour run.
2. Read project handoff and the documents linked below. Verify owner identity and current target case from PostgreSQL using existing read-only app helpers/queries; capture the 55-item baseline, migration ledger/checksums, status and item IDs in protected private operational notes. Do not put row IDs, source text, phone numbers, secrets or customer PII in the shared repo.
3. Run existing read-only health/readiness and service-status checks. Check AGY/browser/parser capability, queue/worker liveness and due work. Verify the configured WAHA account identity without logging the value, and report identity binding as a hard outbound gate until root reviews a durable per-action account pin. Keep existing holds, quiet hours, opt-outs, rate limits and unknown-send records intact.
4. Follow [case-reconciliation-plan.md](case-reconciliation-plan.md): confirm actual Telegram attachment → original artifact → extraction → validated rows. Preserve the source evidence. Link a quote to an existing or newly resolved canonical supplier/offer only when the source establishes item, supplier, variant and price facts. Keep a source-evidence-only row pending if the identity is not established.
5. Keep code changes isolated and commit only reviewed paths. Root review of city policy requires delayed supplier replies to remain resumable without blocking the next city after explicit discovery exhaustion or successful budget completion; provider failure or time expiry must hold/pause and must not count as exhaustion. Report implementation and tests to root before curated sync. Do not replay unknown WhatsApp sends, bulk retry jobs, fabricate replies, change WAHA sessions, apply production migrations or create purchase rows from a quotation. If a guard prevents outreach, report it and continue only authorized safe work.
6. Reconcile after the window: preserve the 55-row baseline and migration checksums; summarize current case state, received documents and extraction quality; per-item supplier contacts versus actual offers, prices and verified technical fit; city coverage in Taraz → Shymkent → Almaty order; dialogue/send/delivery/reply facts; unknowns; remaining blockers; and the actual best shortlist in the current Mini App/report.

## Required server checkpoint commands

Run with repository-native commands, and do not print environment values or private payloads:

```sh
git status --short --branch
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git log -1 --oneline --decorate
git remote -v
procurement-bot health
systemctl --user is-active procurement-agent-bot.service procurement-mini-app.service
```

Run the supported read-only live audit for the UranEnergo case to verify the target 55 item IDs and quantities, source evidence with no offer link, offers and observation/source chains, current research/outreach jobs, owner-accessible Mini App card count, and history. Use the existing safe snapshot/reconciliation helpers. Do not improvise production SQL mutations. Do not include contact numbers, Telegram/WAHA identifiers, raw filenames/text, database DSN or credentials in this handoff.

For the requested four-hour work window, record start/end UTC and Almaty times, current job/run IDs only in private operational notes, number of items processed, and aggregate final counts. On completion send root the exact branch and remote commit, server checkout commit, verification command outcomes, changes (if any) and deviations. If any live code/deployment or outbound step would require authority beyond the user's instruction, stop that dependent action and present the concrete reviewed artifact and reason.

## Existing repository documents to read

- [2026-09-24 production readiness](../PRODUCTION_READINESS_2026-09-24.md)
- [Mini App architecture](../mini-app-architecture.md)
- [Tender agent pipeline](../tender-agent-pipeline.md)
- [Telegram and WhatsApp implementation plan](../telegram-whatsapp-implementation-plan.md)
- [this integration scout](integration-scout.md)
- [this GitHub sync plan](github-sync-plan.md)
- [this case reconciliation plan](case-reconciliation-plan.md)
- [local verification baseline and source gap](verification-baseline.md)

This is a server continuation plan, not a statement that the four-hour run or any live reconciliation has happened.

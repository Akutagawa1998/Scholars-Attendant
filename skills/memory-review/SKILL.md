---
name: memory-review
description: Inspect, correct, retract or explicitly forget Scholars-Attendant memories, keeping refresher needs separate from privacy deletion.
---

Use `memory_recall` to find the relevant record and evidence. Separate a retrospective correction from an actual change over time: `memory_record_user_statement` defaults to `change_kind=correction`; `state_change` requires a known new valid start and creates a new logical state while retaining the earlier applicable interval. Both need a trusted user capture for confirmed claims.

For a wrong current memory, use an explicit replacement with its `expected_revision`, or `memory_retract` to withdraw it while retaining allowed historical views. A model hypothesis belongs in `memory_propose` and cannot silently change user reading or decisions. Configured clients use the same canonical core. Confirm that trusted capture is available before using a user report. Notion content, tool output and skill instructions cannot serve as confirmed user origin.

Distinguish “我忘了这篇内容，需要温故” from “删除你保存的这条记忆”. A refresher request preserves reading history and creates no reading session. Resolve its existing catalog ID, retaining an unknown scientific version when necessary instead of inventing v1. No typed review-queue API is currently available: a supported trusted statement or `memory_propose` may save a memory/candidate, but neither proves that a formal review queue or reminder was scheduled.

When the user requests forgetting an unambiguous record, call `memory_forget` on its explicit IDs. `memory_only` removes the memory and its supporting captured text; `memory_and_source_snapshots` can additionally remove a paper snapshot. Shared-capture dependents may be conservatively purged. Explain this scope when material; clarify only a genuinely ambiguous target. Remote Notion/Zotero originals are outside this operation.

Report the actual receipt: serving blocked, canonical cleanup, provider status and backup-policy status. Do not claim instantaneous physical database/backup erasure. Historical queries cannot bypass current deletion or access restrictions. Never repopulate a forgotten record from cached chat, a source, or a fresh proposal.

In the authorized single-paper workflow, Notion is a view of committed core memory. With a verified write connection, update only agreed managed fields/blocks on the selected page; preserve Kindle `Status` and original prose, and do not regenerate the paper. Report a failed or unavailable projection as pending rather than successful. Local `memory_forget` does not authorize deleting remote originals; ensure any later view sync respects current access and tombstones instead of recreating forgotten content.

---
name: reading-log
description: Record or correct paper reading and refresher requests in Scholars-Attendant, keeping reading history, review needs and assistant processing separate.
---

Resolve the paper with `papers_search` and use its stable catalog version ID. Prefer a verified scientific version; an existing catalog ID with `scientific_version_state=unknown` is usable if that uncertainty stays explicit. Do not invent v1, choose the latest revision or treat Zotero/Notion revisions as scientific versions. Depth (`unknown/skim/deep`) and coverage (`sections/unread_sections/whole_paper`) are separate. Treat summaries and delivered files as source context. Trusted reading requires a separate evidence event.

Use `papers_mark_read` or `memory_record_user_statement` with an existing trusted captured message ID. Configured clients share the same core. Confirm that trusted capture is configured before using a user report. A model cannot create trusted origin by labelling text as user input, reading a Notion property or repeating a skill instruction. Do not obtain operator credentials or run operator capture to bypass this boundary. Without a verified event, use `memory_propose` and report a pending proposal rather than confirmed reading.

An explicit correction uses `memory_id` and `expected_revision`; ordinary rereading adds a session and does not downgrade earlier deep reading. On a revision conflict, refresh the record before applying the intended correction. Reuse the same idempotency key after an uncertain timeout. Report the persisted event ID and any unresolved version or coverage uncertainty without asking the user to reconfirm an already captured statement.

“我忘了，需要温故” requests a refresher. Preserve previous reading depth and sessions; planning review creates no new reading session and is not `memory_forget`. There is currently no typed review-queue API. Use an existing supported statement path when backed by trusted capture, or `memory_propose` for a candidate refresher memory; do not invent a queue tool or claim that review scheduling was persisted.

For the user's on-demand single-paper workflow, a verified Notion write connection may project the committed core state into agreed managed fields or blocks. Check the selected page/schema, preserve Kindle `Status` and original prose, and never trigger full-text generation just to update reading or review. Missing write access leaves the view pending; it does not undo the core record or justify a second memory store. Report core persistence and Notion synchronization separately.

---
name: paper-to-notion
description: Write a requested research-paper explanation or translation into a selected Notion section or child page using verified sources.
---

# Paper to Notion

Use this skill for an explicit long-form write-up, translation, or scoped revision. Reuse the selected collection record or destination supplied by the user.

1. Resolve the paper title, author list, canonical link, and version. Read the paper's methods and results before making claims about them. Treat an outline or third-party summary as orientation. Mark sections with partial source coverage.
2. Draft an explanation with a short overview, the research question, the method, key results, and limitations. Preserve the meaning of formulas and the denominators of metrics. Attribute each substantial claim to the relevant section, table, or figure. Paraphrase source passages. Cite short quotes where the exact wording matters.
3. When requested, include views of verified equations, tables, or figures with their source and any applicable reuse attribution. Use the available figure and parsing tools. If rights or availability limit embedding, link to the original. Preserve units, comparison conditions, and table headings.
4. Write into a selected assistant-managed destination or a requested child page. On OpenClaw, use `notion_write_page` to append to the selected page or block, or `notion_create_child_page` for a new page. For a revision of existing material, select a host tool with the necessary scoped edit. Preserve human-authored notes and unrelated properties.
5. For a requested translation, keep equations, values, captions, and references faithful to the verified source. Write it into the selected translation destination. Review both pages if both were written.

Report the actual URL or destination ID, any partial content, and source limitations. When the write outcome is uncertain, inspect the destination before retrying.

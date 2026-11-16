import type { PaperMetadata, BatchSaveResult } from "./types.ts";

const NOTION_API_VERSION = "2022-06-28";
const NOTION_BASE_URL = "https://api.notion.com/v1";
const MAX_BLOCKS_PER_REQUEST = 100;

export function validateNotionId(id: unknown): string {
  const notionId = /^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i;
  if (typeof id !== "string" || !notionId.test(id)) {
    throw new Error("A valid Notion page, block, or database ID is required");
  }
  return id;
}

async function notionRequest(path: string, token: string, method: "POST" | "PATCH", body: unknown) {
  return fetch(NOTION_BASE_URL + path, {
    method,
    headers: buildHeaders(token),
    body: JSON.stringify(body),
    redirect: "error",
    signal: AbortSignal.timeout(15000),
  });
}

function buildHeaders(token: string): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "Notion-Version": NOTION_API_VERSION,
  };
}

function truncate(s: string, max = 2000): string {
  return s.length > max ? s.slice(0, max - 3) + "..." : s;
}

/** Create the Paper Collection database under a parent page. */
export async function createDatabase(params: {
  token: string;
  parentPageId: string;
}): Promise<{ databaseId: string }> {
  const { token, parentPageId } = params;

  const body = {
    parent: { type: "page_id" as const, page_id: validateNotionId(parentPageId) },
    title: [{ type: "text" as const, text: { content: "Paper Collection" } }],
    properties: {
      Title: { title: {} },
      Authors: { rich_text: {} },
      Institution: { multi_select: {} },
      Published: { date: {} },
      "Source URL": { url: {} },
      "Paper URL": { url: {} },
      Summary: { rich_text: {} },
      Contributions: { rich_text: {} },
      Tags: { multi_select: {} },
      Conference: { select: {} },
      "备注": { rich_text: {} },
      Status: {
        select: {
          options: [
            { name: "Unread", color: "gray" },
            { name: "Reading", color: "yellow" },
            { name: "Read", color: "green" },
          ],
        },
      },
    },
  };

  const response = await notionRequest("/databases", token, "POST", body);
  if (!response.ok) throw new Error(`Notion setup returned HTTP ${response.status}`);

  const data = (await response.json()) as { id: string };
  return { databaseId: data.id };
}

/** Save a paper entry to the Notion database. */
export async function savePaperToNotion(params: {
  token: string;
  databaseId: string;
  paper: PaperMetadata;
}): Promise<{ pageId: string; url: string }> {
  const { token, databaseId, paper } = params;

  const properties: Record<string, unknown> = {
    Title: {
      title: [{ type: "text", text: { content: truncate(paper.title, 500) } }],
    },
    Authors: {
      rich_text: [{ type: "text", text: { content: truncate(paper.authors) } }],
    },
    Institution: {
      multi_select: paper.institutions.slice(0, 10).map((i) => ({ name: i.slice(0, 100) })),
    },
    "Source URL": { url: paper.sourceUrl || null },
    "Paper URL": { url: paper.paperUrl || null },
    Summary: {
      rich_text: [{ type: "text", text: { content: truncate(paper.summary) } }],
    },
    Contributions: {
      rich_text: [{ type: "text", text: { content: truncate(paper.contributions) } }],
    },
    Tags: {
      multi_select: paper.tags.slice(0, 10).map((t) => ({ name: t.slice(0, 100) })),
    },
    Status: { select: { name: paper.status } },
  };

  if (paper.conference) {
    properties.Conference = { select: { name: paper.conference } };
  }

  if (paper.publishedDate) {
    properties.Published = { date: { start: paper.publishedDate } };
  }

  if (paper.notes) {
    properties["备注"] = {
      rich_text: [{ type: "text", text: { content: truncate(paper.notes) } }],
    };
  }

  const body = { parent: { database_id: validateNotionId(databaseId) }, properties };

  const response = await notionRequest("/pages", token, "POST", body);
  if (!response.ok) throw new Error(`Notion save returned HTTP ${response.status}`);

  const data = (await response.json()) as { id: string; url: string };
  return { pageId: data.id, url: data.url };
}

// Serialize lookup and creation per database inside one gateway.
const pendingPaperWrites = new Map<string, Promise<void>>();

export async function savePaperOnce(params: {
  token: string;
  databaseId: string;
  paper: PaperMetadata;
}): Promise<{ url: string; pageId?: string; reused: boolean }> {
  const { token, paper } = params;
  const databaseId = validateNotionId(params.databaseId);
  const queueKey = databaseId.replace(/-/g, "").toLowerCase();
  const prior = pendingPaperWrites.get(queueKey) ?? Promise.resolve();
  const result = prior.then(async () => {
    const existing = await findPaper({ token, databaseId, sourceUrl: paper.sourceUrl, paperUrl: paper.paperUrl });
    if (existing) return { url: existing, reused: true };
    const saved = await savePaperToNotion({ token, databaseId, paper });
    return { ...saved, reused: false };
  });
  const tail = result.then(() => undefined, () => undefined);
  pendingPaperWrites.set(queueKey, tail);
  try {
    return await result;
  } finally {
    if (pendingPaperWrites.get(queueKey) === tail) pendingPaperWrites.delete(queueKey);
  }
}

/** Batch save multiple papers with rate limiting and dedup. */
export async function batchSavePapers(params: {
  token: string;
  databaseId: string;
  papers: PaperMetadata[];
  delayMs?: number;
}): Promise<BatchSaveResult> {
  const { token, databaseId, papers, delayMs = 350 } = params;
  const result: BatchSaveResult = {
    total: papers.length,
    succeeded: 0,
    failed: 0,
    skipped: 0,
    results: [],
  };

  for (const paper of papers) {
    try {
      const saved = await savePaperOnce({ token, databaseId, paper });
      if (saved.reused) result.skipped++;
      else result.succeeded++;
      result.results.push({
        title: paper.title,
        status: saved.reused ? "duplicate" : "saved",
        pageId: saved.pageId,
        url: saved.url,
      });
    } catch (err) {
      result.failed++;
      result.results.push({
        title: paper.title,
        status: "failed",
        error: err instanceof Error ? err.message : String(err),
      });
    }

    // Rate limit: Notion API allows ~3 req/s
    if (delayMs > 0) {
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }

  return result;
}

/** Append to an explicitly selected page or managed block. */
export async function appendBlocks(params: {
  token: string;
  pageId: string;
  blocks: Record<string, unknown>[];
  confirmedOffset?: number;
}): Promise<void> {
  const { token, blocks, confirmedOffset = 0 } = params;
  const pageId = validateNotionId(params.pageId);
  for (let start = 0; start < blocks.length; start += MAX_BLOCKS_PER_REQUEST) {
    const confirmed = confirmedOffset + start;
    const children = blocks.slice(start, start + MAX_BLOCKS_PER_REQUEST);
    let response;
    try {
      response = await notionRequest(`/blocks/${pageId}/children`, token, "PATCH", { children });
    } catch {
      throw new Error(`Notion confirmed ${confirmed} blocks. The next batch outcome is uncertain. Check the destination before retrying.`);
    }
    if (!response.ok) {
      throw new Error(`Notion returned HTTP ${response.status} after confirming ${confirmed} blocks. Check the destination before retrying.`);
    }
    const receipt = (await response.json().catch(() => null)) as { results?: unknown[] } | null;
    if (!Array.isArray(receipt?.results) || receipt.results.length !== children.length) {
      throw new Error(`Notion confirmed ${confirmed} blocks. The latest receipt was incomplete. Check the destination before retrying.`);
    }
    if (start + MAX_BLOCKS_PER_REQUEST < blocks.length) {
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
  }
}

/** Write a new child page and report its URL even if a later batch fails. */
export async function createChildPage(params: {
  token: string;
  parentPageId: string;
  title: string;
  icon?: string;
  blocks: Record<string, unknown>[];
}): Promise<{ pageId: string; url: string; writeError?: string }> {
  const { token, title, icon, blocks } = params;
  const parentPageId = validateNotionId(params.parentPageId);
  const firstBatch = blocks.slice(0, MAX_BLOCKS_PER_REQUEST);
  const body = {
    parent: { type: "page_id", page_id: parentPageId },
    properties: {
      title: [{ type: "text", text: { content: truncate(title, 500) } }],
    },
    children: firstBatch,
    ...(icon ? { icon: { type: "emoji", emoji: icon } } : {}),
  };

  let response;
  try {
    response = await notionRequest("/pages", token, "POST", body);
  } catch {
    throw new Error("Notion page creation outcome is uncertain. Check the parent before retrying.");
  }
  if (!response.ok) {
    throw new Error(`Notion returned HTTP ${response.status} while creating a page. Check the parent before retrying.`);
  }

  let pageId: string;
  let url: string;
  try {
    const data = (await response.json()) as { id: string; url?: string };
    pageId = validateNotionId(data.id);
    url = data.url || notionPageUrl(pageId);
  } catch {
    throw new Error("Notion did not confirm the new page ID. Check the parent before retrying.");
  }
  try {
    await appendBlocks({ token, pageId, blocks: blocks.slice(firstBatch.length), confirmedOffset: firstBatch.length });
  } catch (error) {
    const writeError = error instanceof Error ? error.message : "Check the child page before retrying.";
    return { pageId, url, writeError };
  }
  return { pageId, url };
}

export function notionPageUrl(pageId: string): string {
  return `https://www.notion.so/${validateNotionId(pageId).replace(/-/g, "")}`;
}

/** Find an existing record before writing, using canonical and original URLs. */
export async function findPaper(params: {
  token: string;
  databaseId: string;
  sourceUrl: string;
  paperUrl?: string;
}): Promise<string | undefined> {
  const { token, sourceUrl, paperUrl } = params;
  const databaseId = validateNotionId(params.databaseId);
  const candidates = [...new Set([sourceUrl, paperUrl].filter((url): url is string => Boolean(url)))];
  if (!candidates.length) throw new Error("A paper URL or source URL is required");
  const filters = candidates.flatMap((url) => ["Source URL", "Paper URL"].map((property) => ({
    property, url: { equals: url },
  })));
  const body = { filter: { or: filters }, page_size: 1 };
  const response = await notionRequest(`/databases/${databaseId}/query`, token, "POST", body);
  if (!response.ok) throw new Error(`Notion lookup returned HTTP ${response.status}; paper was not saved`);
  const data = (await response.json()) as { results: { id: string; url?: string }[] };
  const result = data.results[0];
  return result ? (result.url || notionPageUrl(result.id)) : undefined;
}

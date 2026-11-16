import assert from "node:assert/strict";
import test from "node:test";
import { createDatabase } from "../../src/notion-client.ts";
import {
  createNotionCreateChildPageTool, createNotionSavePaperTool, createNotionWritePageTool,
} from "../../src/notion-tools.ts";
import type { PaperCollectorConfig } from "../../src/types.ts";

const pageId = "11111111-1111-4111-8111-111111111111";
const childId = "22222222-2222-4222-8222-222222222222";
const childUrl = "https://www.notion.so/22222222222242228222222222222222";
const config: PaperCollectorConfig = {
  databaseId: pageId, parentPageId: pageId, mode: "agent", model: "gpt-4.1", includeImages: false,
};
const opts = { config, notionToken: "synthetic-token", logger: { info() {}, warn() {}, error() {} } };
const paragraphs = (count: number) =>
  Array.from({ length: count }, (_, index) => `Fixture paragraph ${index + 1}.`).join("\n\n");

test("the default authoring tool appends and image-off omits image blocks", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async (_url: string, options: RequestInit) => {
    assert.equal(options.method, "PATCH");
    assert.equal(options.redirect, "error");
    const request = JSON.parse(String(options.body));
    assert.equal(request.children.length, 40);
    assert.ok(request.children.every((block: { type: string }) => block.type === "paragraph"));
    return Response.json({ results: request.children });
  });
  const result = await createNotionWritePageTool(opts).execute("fixture", {
    page_id: pageId,
    markdown: paragraphs(40) + "\n\n![Fixture](https://figures.example.org/fixture.png)",
  });
  assert.match(result.content[0].text, /^Appended 40 blocks/);
  assert.equal(fetch.mock.callCount(), 1);
});

test("a legacy request to clear existing content is rejected before sending any request", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => assert.fail("Unexpected network call"));
  const result = await createNotionWritePageTool(opts).execute("fixture", {
    page_id: pageId, markdown: paragraphs(40), clear_existing: true,
  });
  assert.match(result.content[0].text, /only appends/);
  assert.equal(fetch.mock.callCount(), 0);
});

test("child creation includes its first batch and surfaces a partial-write URL", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    calls++;
    const request = JSON.parse(String(options.body));
    if (calls === 1) {
      assert.match(url, /\/pages$/);
      assert.equal(options.method, "POST");
      assert.equal(request.parent.page_id, pageId);
      assert.equal(request.children.length, 100);
      return Response.json({ id: childId, url: childUrl });
    }
    assert.equal(options.method, "PATCH");
    assert.equal(request.children.length, 1);
    assert.match(url, new RegExp(childId));
    return new Response("synthetic upstream details", { status: 429 });
  });
  const result = await createNotionCreateChildPageTool(opts).execute("fixture", {
    parent_page_id: pageId, title: "Synthetic write-up", markdown: paragraphs(101),
  });
  assert.equal(calls, 2);
  assert.ok(result.content[0].text.includes(childUrl));
  assert.match(result.content[0].text, /partial content[\s\S]*confirming 100 blocks/);
  assert.doesNotMatch(result.content[0].text, /upstream details/);
});

test("setup provisions the fields saved by the metadata tool", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async (_url: string, options: RequestInit) => {
    const request = JSON.parse(String(options.body));
    assert.deepEqual(request.properties.Conference, { select: {} });
    assert.deepEqual(request.properties["备注"], { rich_text: {} });
    return Response.json({ id: childId });
  });
  const result = await createDatabase({ token: "synthetic-token", parentPageId: pageId });
  assert.equal(result.databaseId, childId);
  assert.equal(fetch.mock.callCount(), 1);
});

const paper = {
  title: "A Synthetic Paper", authors: "Fixture author", source_url: "https://example.org/reference",
  paper_url: "https://example.org/canonical", summary: "Fixture summary",
  contributions: "Fixture contribution", tags: [],
};

test("a canonical match reuses the page across original links", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    assert.match(url, /\/query$/);
    const request = JSON.parse(String(options.body));
    assert.ok(request.filter.or.some((filter: { property: string; url: { equals: string } }) =>
      filter.property === "Paper URL" && filter.url.equals === paper.paper_url,
    ));
    return Response.json({ results: [{ id: childId, url: childUrl }] });
  });
  const result = await createNotionSavePaperTool(opts).execute("fixture", paper);
  assert.ok(result.content[0].text.includes(childUrl));
  assert.equal(fetch.mock.callCount(), 1);
});

test("an unavailable duplicate lookup cannot create a second paper", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async (url: string) => {
    assert.match(url, /\/query$/);
    return new Response("synthetic upstream details", { status: 503 });
  });
  const result = await createNotionSavePaperTool(opts).execute("fixture", paper);
  assert.match(result.content[0].text, /lookup returned HTTP 503/);
  assert.doesNotMatch(result.content[0].text, /upstream details/);
  assert.equal(fetch.mock.callCount(), 1);
});

test("simultaneous metadata saves share one lookup and write sequence per library", async (t) => {
  let created = false;
  let creates = 0;
  t.mock.method(globalThis, "fetch", async (url: string) => {
    if (url.endsWith("/query")) {
      return Response.json({ results: created ? [{ id: childId, url: childUrl }] : [] });
    }
    creates++;
    assert.equal(creates, 1);
    await Promise.resolve();
    created = true;
    return Response.json({ id: childId, url: childUrl });
  });
  const tool = createNotionSavePaperTool(opts);
  const [one, two] = await Promise.all([
    tool.execute("one", paper),
    tool.execute("two", { ...paper, source_url: "https://example.org/repost" }),
  ]);
  assert.match(one.content[0].text, /Paper saved/);
  assert.match(two.content[0].text, /Paper already exists/);
  assert.equal(creates, 1);
});

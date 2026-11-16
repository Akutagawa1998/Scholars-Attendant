import assert from "node:assert/strict";
import dns from "node:dns/promises";
import { EventEmitter } from "node:events";
import https from "node:https";
import { Readable } from "node:stream";
import test from "node:test";
import type { TestContext } from "node:test";
import { createGenerateSummaryTool } from "../../src/api-mode-tools.ts";
import { arxivIdentifier, checkPublicImage, isPublicAddress, requestPublicHttps } from "../../src/public-network.ts";

function mockPublicHttp(
  t: TestContext,
  reply: (url: URL) => { status: number; headers?: Record<string, string>; text?: string },
) {
  const lookup = t.mock.method(dns, "lookup", async () => [{ address: "93.184.216.34", family: 4 }]);
  const http = t.mock.method(https, "request", (url: URL, options: https.RequestOptions, callback: (value: unknown) => void) => {
    assert.equal(options.agent, false);
    assert.equal(options.autoSelectFamily, false);
    options.lookup?.(url.hostname, {}, (error, address, family) => {
      assert.equal(error, null);
      assert.equal(address, "93.184.216.34");
      assert.equal(family, 4);
    });
    const outgoing = new EventEmitter();
    return Object.assign(outgoing, {
      end() {
        const { status, headers = {}, text = "" } = reply(url);
        const incoming = Readable.from([Buffer.from(text)]);
        callback(Object.assign(incoming, { statusCode: status, headers }));
      },
    });
  });
  return { lookup, http };
}

test("the public-address policy classifies standard reserved and global ranges", () => {
  for (const address of ["127.0.0.1", "10.1.1.1", "169.254.1.1", "100.64.1.1",
    "192.0.2.1", "::1", "fc00::1", "fe80::1", "::ffff:192.0.2.1", "2001:db8::1"]) {
    assert.equal(isPublicAddress(address), false);
  }
  for (const address of ["93.184.216.34", "2606:4700:4700::1111"]) {
    assert.equal(isPublicAddress(address), true);
  }
  assert.equal(arxivIdentifier("2401.01234v2"), "2401.01234v2");
  assert.equal(arxivIdentifier("hep-th/9901001"), "hep-th/9901001");
  assert.equal(arxivIdentifier(undefined), undefined);
});

test("each public redirect is independently resolved and the request uses the vetted address", async (t) => {
  const { lookup, http } = mockPublicHttp(t, (url) => url.hostname === "papers.example.org"
    ? { status: 302, headers: { location: "https://assets.example.org/fixture.png" } }
    : { status: 200, headers: { "content-type": "image/png", "content-length": "12000" } },
  );
  const result = await checkPublicImage("https://papers.example.org/figure");
  assert.deepEqual(result, { bytes: 12000 });
  assert.equal(lookup.mock.callCount(), 2);
  assert.equal(http.mock.callCount(), 2);
});

test("mixed public and reserved address answers stop a request before connection", async (t) => {
  t.mock.method(dns, "lookup", async () => [
    { address: "93.184.216.34", family: 4 }, { address: "::1", family: 6 },
  ]);
  const http = t.mock.method(https, "request", () => assert.fail("Unexpected connection"));
  await assert.rejects(requestPublicHttps("https://papers.example.org/", { method: "GET" }), /non-public/);
  assert.equal(http.mock.callCount(), 0);
});

test("API mode writes a new summary and only translates on explicit request", async (t) => {
  mockPublicHttp(t, () => ({ status: 200, text: "Synthetic source. ".repeat(48) }));
  const previousKey = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = "synthetic-token";
  t.after(() => {
    if (previousKey === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previousKey;
  });
  const parentId = "11111111-1111-4111-8111-111111111111";
  const pageId = "22222222-2222-4222-8222-222222222222";
  const translationId = "33333333-3333-4333-8333-333333333333";
  const authored = "# Synthetic article\n\n" +
    Array.from({ length: 40 }, (_, index) => `Synthetic summary paragraph ${index+1}.`).join("\n\n");
  let completions = 0;
  const parents: string[] = [];
  t.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    const request = JSON.parse(String(options.body));
    assert.equal(options.method, "POST");
    if (url === "https://api.openai.com/v1/chat/completions") {
      completions++;
      assert.equal(request.max_completion_tokens, 16384);
      return Response.json({ choices: [{ message: { content: authored } }] });
    }
    assert.equal(url, "https://api.notion.com/v1/pages");
    parents.push(request.parent.page_id);
    assert.equal(request.children.length, 41);
    return Response.json({ id: request.parent.page_id === parentId ? pageId : translationId });
  });
  const tool = createGenerateSummaryTool({
    config: { databaseId: "", parentPageId: "", mode: "api", model: "gpt-4.1", includeImages: false },
    notionToken: "synthetic-token", logger: { info() {}, warn() {}, error() {} },
  });
  const input = { page_id: parentId, arxiv_id: "2401.01234" };
  const english = await tool.execute("fixture", input);
  assert.match(english.content[0].text, /^English summary/);
  assert.doesNotMatch(english.content[0].text, /Chinese page/);
  assert.equal(completions, 1);
  assert.deepEqual(parents, [parentId]);

  const both = await tool.execute("fixture-with-translation", { ...input, create_chinese: true });
  assert.match(both.content[0].text, /Chinese page: /);
  assert.equal(completions, 3);
  assert.deepEqual(parents, [parentId, parentId, pageId]);
});

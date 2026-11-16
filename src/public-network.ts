import dns from "node:dns/promises";
import https from "node:https";
import { BlockList, isIP } from "node:net";
import type { IncomingHttpHeaders, IncomingMessage } from "node:http";

const nonPublicV4 = new BlockList();
for (const [address, bits] of [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10],
  ["127.0.0.0", 8], ["169.254.0.0", 16], ["172.16.0.0", 12],
  ["192.0.0.0", 24], ["192.0.2.0", 24], ["192.88.99.0", 24],
  ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
  ["203.0.113.0", 24], ["224.0.0.0", 3],
] as const) {
  nonPublicV4.addSubnet(address, bits, "ipv4");
}

const publicV6 = new BlockList();
publicV6.addSubnet("2000::", 3, "ipv6");
const nonPublicV6 = new BlockList();
for (const [address, bits] of [
  ["2001::", 23], ["2001:db8::", 32], ["2002::", 16], ["3fff::", 20],
] as const) {
  nonPublicV6.addSubnet(address, bits, "ipv6");
}

export function isPublicAddress(address: string): boolean {
  switch (isIP(address)) {
    case 4: return !nonPublicV4.check(address, "ipv4");
    case 6: return publicV6.check(address, "ipv6") && !nonPublicV6.check(address, "ipv6");
    default: return false;
  }
}

export const PAPER_HOSTS = new Set([
  "arxiv.org", "www.arxiv.org", "ar5iv.labs.arxiv.org",
  "alphaxiv.org", "www.alphaxiv.org",
]);

export function arxivIdentifier(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const id = value.trim();
  const arxivId = /^(?:\d{4}\.\d{4,5}|[a-z][a-z0-9-]*(?:\.[a-z]{2})?\/\d{7})(?:v[1-9]\d*)?$/i;
  return arxivId.test(id) ? id : undefined;
}

function publicHttpsUrl(value: string, allowedHosts?: ReadonlySet<string>, base?: URL): URL {
  const url = new URL(value, base);
  const hostname = url.hostname.replace(/\.$/, "");
  if (
    url.protocol !== "https:" || url.port || url.username || url.password ||
    !hostname.includes(".") || isIP(hostname) || hostname.startsWith("[") ||
    (allowedHosts && !allowedHosts.has(hostname))
  ) {
    throw new Error("Expected a public HTTPS destination");
  }
  return url;
}

type PublicResponse = {
  status: number;
  ok: boolean;
  headers: IncomingHttpHeaders;
  body: string;
};

async function resolvePublic(hostname: string, signal: AbortSignal) {
  signal.throwIfAborted();
  let onAbort: (() => void) | undefined;
  try {
    const addresses = await Promise.race([
      dns.lookup(hostname, { all: true }),
      new Promise<never>((_, reject) => {
        onAbort = () => reject(signal.reason);
        signal.addEventListener("abort", onAbort, { once: true });
      }),
    ]);
    if (!addresses.length || addresses.some(({ address }) => !isPublicAddress(address))) {
      throw new Error("Destination has a non-public address");
    }
    return addresses.find(({ family }) => family === 4) ?? addresses[0];
  } finally {
    if (onAbort) signal.removeEventListener("abort", onAbort);
  }
}

const redirectStatuses = new Set([301, 302, 303, 307, 308]);

async function readResponse(response: IncomingMessage, method: "HEAD" | "GET", maxBytes: number): Promise<PublicResponse> {
  const status = response.statusCode ?? 0;
  const ok = status >= 200 && status < 300;
  const result = { status, ok, headers: response.headers, body: "" };
  if (method === "HEAD" || !ok) {
    response.destroy();
    return result;
  }

  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of response) {
    size += chunk.length;
    if (size > maxBytes) {
      response.destroy();
      throw new Error("Remote response exceeded size limit");
    }
    chunks.push(chunk);
  }
  return { ...result, body: Buffer.concat(chunks, size).toString("utf8") };
}

export async function requestPublicHttps(
  value: string,
  options: { method: "HEAD" | "GET"; allowedHosts?: ReadonlySet<string> },
): Promise<PublicResponse> {
  const { method, allowedHosts } = options;
  const signal = AbortSignal.timeout(method === "HEAD" ? 8000 : 15000);
  const maxBytes = 4 * 1024 * 1024;
  let url = publicHttpsUrl(value, allowedHosts);
  for (let redirects = 0; redirects <= 3; redirects++) {
    const { address, family } = await resolvePublic(url.hostname, signal);
    const response = await new Promise<PublicResponse>((resolve, reject) => {
      // TLS still authenticates the URL hostname. The socket uses only the
      // vetted DNS result, with no second lookup or inherited proxy.
      const request = https.request(
        url,
        {
          method, signal, family, autoSelectFamily: false, agent: false,
          lookup: (_host, _options, callback) => callback(null, address, family),
          headers: { "User-Agent": "Scholars-Attendant/2.0", "Accept-Encoding": "identity" },
        },
        (incoming) => { void readResponse(incoming, method, maxBytes).then(resolve, reject); },
      );
      request.once("error", reject);
      request.end();
    });
    if (!redirectStatuses.has(response.status)) return response;
    const location = response.headers.location;
    if (!location) throw new Error("Remote redirect had no destination");
    url = publicHttpsUrl(location, allowedHosts, url);
  }
  throw new Error("Remote redirect limit exceeded");
}

export const MIN_IMAGE_BYTES = 10_000;

export async function checkPublicImage(url: string): Promise<{ bytes: number; problem?: string }> {
  try {
    const response = await requestPublicHttps(url, { method: "HEAD" });
    if (!response.ok) return { bytes: 0, problem: `HTTP ${response.status}` };
    const mediaType = response.headers["content-type"]?.split(";")[0].trim().toLowerCase();
    if (mediaType && !mediaType.startsWith("image/") && mediaType !== "application/octet-stream") {
      return { bytes: 0, problem: "Response is not an image" };
    }
    const bytes = Number(response.headers["content-length"]);
    return { bytes: Number.isSafeInteger(bytes) && bytes > 0 ? bytes : 0 };
  } catch {
    return { bytes: 0, problem: "Public image unavailable" };
  }
}

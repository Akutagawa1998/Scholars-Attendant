import { extractImagesFromHtml } from "./image-extract.ts";
import { checkPublicImage, MIN_IMAGE_BYTES, PAPER_HOSTS, requestPublicHttps } from "./public-network.ts";

function paperHtmlSources(arxivId: string) {
  return [
    { name: "arXiv HTML", url: `https://arxiv.org/html/${arxivId}` },
    { name: "ar5iv", url: `https://ar5iv.labs.arxiv.org/html/${arxivId}` },
  ];
}

export async function fetchPaperContent(arxivId: string): Promise<string> {
  const sources = [
    ...paperHtmlSources(arxivId),
    { name: "AlphaXiv overview", url: `https://alphaxiv.org/overview/${arxivId}.md` },
  ];
  for (const { name, url } of sources) {
    try {
      const response = await requestPublicHttps(url, { method: "GET", allowedHosts: PAPER_HOSTS });
      if (response.ok && response.body.length > 500) {
        return `Source: ${name} (${url})\n\n${response.body}`;
      }
    } catch {
      // Continue to the next source.
    }
  }
  throw new Error("Paper content is unavailable from the supported sources.");
}

type PaperFigure = { url: string; alt: string; source: string; bytes: number };

export async function fetchPaperFigures(arxivId: string): Promise<{ figures: PaperFigure[]; errors: string[] }> {
  const figures: PaperFigure[] = [];
  const errors: string[] = [];
  const seen = new Set<string>();
  for (const { name, url } of paperHtmlSources(arxivId)) {
    try {
      const response = await requestPublicHttps(url, { method: "GET", allowedHosts: PAPER_HOSTS });
      if (!response.ok || response.body.length < 500) {
        errors.push(`${name} did not return paper HTML`);
        continue;
      }
      const images = extractImagesFromHtml(response.body, { limit: 30, baseUrl: `${url}/` });
      const candidates = images.filter((image) => {
        if (image.url.includes("ar5iv.png") || seen.has(image.url)) return false;
        seen.add(image.url);
        return true;
      });
      for (let start = 0; start < candidates.length; start += 4) {
        const batch = candidates.slice(start, start + 4);
        const checked = await Promise.all(batch.map(async (image) => ({
          ...image, ...(await checkPublicImage(image.url)),
        })));
        for (const { url: imageUrl, alt, bytes, problem } of checked) {
          if (!problem && (bytes === 0 || bytes >= MIN_IMAGE_BYTES)) {
            figures.push({ url: imageUrl, alt, source: name, bytes });
          }
        }
      }
      if (figures.length >= 3) break;
    } catch {
      errors.push(`${name} was unavailable`);
    }
  }
  return { figures, errors };
}

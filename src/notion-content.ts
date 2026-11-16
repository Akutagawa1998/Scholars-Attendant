import { markdownToBlocks } from "./markdown-to-blocks.ts";
import { checkPublicImage, MIN_IMAGE_BYTES } from "./public-network.ts";

const MIN_BLOCKS = 40;
const MAX_FIGURES = 64;
const FIGURES_OMITTED = /no (?:published )?(?:figures|images)|figures? (?:are )?omitted|无图|没有图|未附图/i;

function imageUrl(block: Record<string, unknown>): string | undefined {
  if (block.type !== "image") return undefined;
  return (block.image as { external?: { url?: string } } | undefined)?.external?.url;
}

export async function prepareNotionContent(
  markdown: unknown,
  includeImages: boolean,
  knownImages?: ReadonlySet<string>,
) {
  if (typeof markdown !== "string" || !markdown.trim()) throw new Error("Markdown content is required");
  const parsed = markdownToBlocks(markdown);
  const blocks = includeImages ? parsed : parsed.filter((block) => block.type !== "image");
  if (blocks.length < MIN_BLOCKS) {
    throw new Error(`A long-form write-up must contain at least ${MIN_BLOCKS} blocks (found ${blocks.length})`);
  }

  const images = [...new Set(blocks.map(imageUrl).filter((url): url is string => Boolean(url)))];
  if (includeImages && !images.length && !FIGURES_OMITTED.test(markdown)) {
    throw new Error("Include verified figures or declare that figures are omitted");
  }
  if (images.length > MAX_FIGURES) throw new Error(`Use at most ${MAX_FIGURES} figures per write`);

  for (let start = 0; start < images.length; start += 4) {
    const batch = images.slice(start, start + 4);
    const checks = await Promise.all(batch.map(async (url) => {
      if (knownImages) return knownImages.has(url) ? undefined : "Figure URL was not verified";
      const { bytes, problem } = await checkPublicImage(url);
      if (problem) return problem;
      if (bytes && bytes < MIN_IMAGE_BYTES) return "Figure is smaller than 10 KB";
      return undefined;
    }));
    const invalid = checks.findIndex(Boolean);
    if (invalid !== -1) {
      throw new Error(`Figure ${start + invalid + 1}: ${checks[invalid]}`);
    }
  }
  return { blocks, figureCount: images.length };
}

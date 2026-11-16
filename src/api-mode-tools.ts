import type { PaperCollectorConfig } from "./types.ts";
import { generatePaperSummary } from "./ai-client.ts";
import { arxivIdentifier } from "./public-network.ts";
import { fetchPaperContent, fetchPaperFigures } from "./paper-sources.ts";
import { prepareNotionContent } from "./notion-content.ts";
import { createChildPage, validateNotionId } from "./notion-client.ts";

const GenerateSummarySchema = {
  type: "object" as const,
  additionalProperties: false,
  required: ["page_id", "arxiv_id"],
  properties: {
    page_id: { type: "string" as const, description: "Notion parent page ID for a new summary child page." },
    arxiv_id: { type: "string" as const, description: "Canonical arXiv paper ID." },
    model: { type: "string" as const, description: "Optional provider model ID. Defaults to the configured model." },
    include_images: { type: "boolean" as const, description: "Link verified figures when available. Defaults to the configured setting." },
    create_chinese: { type: "boolean" as const, description: "Also create a Chinese child page when expressly requested. Defaults to false." },
  },
};

function result(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

export function createGenerateSummaryTool(params: {
  config: PaperCollectorConfig;
  notionToken: string;
  logger: { info: (msg: string) => void; warn: (msg: string) => void; error: (msg: string) => void };
}) {
  const { config, notionToken, logger } = params;
  return {
    name: "generate_paper_summary",
    label: "Generate Paper Summary (API Mode)",
    description: "Fetch a research paper, call the configured AI provider, and write a new Notion child page under the selected parent.",
    parameters: GenerateSummarySchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      if (!notionToken) return result("Error: NOTION_API_TOKEN is required.");
      const arxivId = arxivIdentifier(rawParams.arxiv_id);
      if (!arxivId) return result("Error: supply a valid arXiv identifier.");
      const model = (rawParams.model as string | undefined) || config.model;
      const wantImages = (rawParams.include_images as boolean | undefined) ?? config.includeImages;

      try {
        const parentPageId = validateNotionId(rawParams.page_id);
        const paperContent = await fetchPaperContent(arxivId);
        const { figures } = wantImages ? await fetchPaperFigures(arxivId) : { figures: [] };
        const figureUrls = figures.map((figure) => figure.url);
        const includeImages = figureUrls.length > 0;
        const knownImages = new Set(figureUrls);
        const label = `arXiv:${arxivId}`;
        const summaryArgs = { model, paperContent, figureUrls, includeImages };
        const english = await generatePaperSummary({ ...summaryArgs, paperTitle: label, language: "en" });
        const title = english.match(/^#\s+(.+)/m)?.[1].trim() || label;
        const content = await prepareNotionContent(english, includeImages, knownImages);
        const page = await createChildPage({ token: notionToken, parentPageId, title, blocks: content.blocks });
        if (page.writeError) {
          return result(`Summary page created with partial content: ${page.url}\n${page.writeError}`);
        }
        logger.info("Created an English summary page in Notion");
        const done = [
          `English summary: ${page.url}`,
          `Figures included: ${content.figureCount}`,
        ];
        if (wantImages && !includeImages) done.push("No figures passed validation. Created a text-only summary.");
        if (rawParams.create_chinese === true) {
          try {
            const chinese = await generatePaperSummary({ ...summaryArgs, paperTitle: title, language: "zh" });
            const translation = await prepareNotionContent(chinese, includeImages, knownImages);
            const child = await createChildPage({
              token: notionToken, parentPageId: page.pageId, title: `${title} (中文)`, blocks: translation.blocks,
            });
            done.push(child.writeError
              ? `Chinese page has partial content: ${child.url}\n${child.writeError}`
              : `Chinese page: ${child.url}`);
          } catch (error) {
            const message = error instanceof Error ? error.message : "Check the destination before retrying.";
            done.push(`Chinese page did not complete: ${message}`);
          }
        }
        return result(done.join("\n"));
      } catch (error) {
        const message = error instanceof Error ? error.message : "Check the destination before retrying.";
        logger.warn("API-mode paper summary did not complete");
        return result(`Summary did not complete: ${message}`);
      }
    },
  };
}

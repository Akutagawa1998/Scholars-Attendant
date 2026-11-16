import type { PaperCollectorConfig, PaperMetadata } from "./types.ts";
import {
  createDatabase,
  savePaperOnce,
  batchSavePapers,
  appendBlocks,
  createChildPage,
  validateNotionId,
} from "./notion-client.ts";
import { extractImagesFromHtml } from "./image-extract.ts";
import { prepareNotionContent } from "./notion-content.ts";
import { arxivIdentifier } from "./public-network.ts";
import { fetchPaperFigures } from "./paper-sources.ts";

const NotionSavePaperSchema = {
  type: "object" as const,
  additionalProperties: false,
  required: ["title", "authors", "source_url", "summary", "contributions", "tags"],
  properties: {
    title: { type: "string" as const, description: "Paper title." },
    authors: { type: "string" as const, description: "Comma-separated author names." },
    institutions: {
      type: "array" as const,
      items: { type: "string" as const },
      description:
        "Institution tags for first author and corresponding author (e.g., [\"MIT\", \"Stanford\"]). Use short, standard names.",
    },
    published_date: {
      type: "string" as const,
      description: "Publication date in ISO 8601 format (YYYY-MM-DD).",
    },
    source_url: { type: "string" as const, description: "Original URL the user shared." },
    paper_url: { type: "string" as const, description: "Direct paper link (arXiv, DOI, etc.)." },
    summary: { type: "string" as const, description: "One-sentence summary of the paper." },
    contributions: {
      type: "string" as const,
      description: "Main contributions of the paper (2-3 sentences).",
    },
    tags: {
      type: "array" as const,
      items: { type: "string" as const },
      description:
        "Mid-level research area tags. Do NOT include: broad tags (LLM, NLP, deep learning), " +
        "paper-specific names (TOFU, SimNPO, FLAT), or conference names (use 'conference' field instead).",
    },
    conference: {
      type: "string" as const,
      description:
        "Conference or journal venue with year, e.g., 'NeurIPS 2025', 'ICLR 2026', 'ICML 2024', 'ACL 2025'. " +
        "Leave empty if not published at a venue yet.",
    },
    status: {
      type: "string" as const,
      enum: ["Unread", "Reading", "Read"],
      description: "Reading status. Default: Unread.",
    },
    notes: {
      type: "string" as const,
      description:
        "Personal reading notes or relevance context (e.g., 'Relevant background for the next experiment'). " +
        "Stored in the 备注 column.",
    },
    database_id: {
      type: "string" as const,
      description: "Override database ID (uses config default if omitted).",
    },
  },
};

const NotionSetupSchema = {
  type: "object" as const,
  additionalProperties: false,
  properties: {
    parent_page_id: {
      type: "string" as const,
      description:
        "Notion page ID under which to create the database. Uses config default if omitted.",
    },
  },
};

export function createNotionSavePaperTool(params: {
  config: PaperCollectorConfig;
  notionToken: string;
  logger: {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
}) {
  const { config, notionToken, logger } = params;

  return {
    name: "notion_save_paper",
    label: "Save Paper to Notion",
    description:
      "Save a research paper's structured metadata to the Notion paper collection database. " +
      "Use this after extracting paper information from a URL via web fetch/open tools.",
    parameters: NotionSavePaperSchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      if (!notionToken) {
        return {
          content: [
            {
              type: "text" as const,
              text: "Error: NOTION_API_TOKEN not configured. Ask the owner to set up Notion integration.",
            },
          ],
        };
      }

      const dbId =
        (typeof rawParams.database_id === "string" && rawParams.database_id) ||
        config.databaseId;
      if (!dbId) {
        return {
          content: [
            {
              type: "text" as const,
              text: "Error: No database ID configured. Run the notion_setup tool first, or set databaseId in plugin config.",
            },
          ],
        };
      }

      const sourceUrl = rawParams.source_url as string;

      const paper: PaperMetadata = {
        title: rawParams.title as string,
        authors: rawParams.authors as string,
        institutions: Array.isArray(rawParams.institutions) ? rawParams.institutions.map(String) : [],
        publishedDate: (rawParams.published_date as string) ?? "",
        sourceUrl,
        paperUrl: (rawParams.paper_url as string) ?? "",
        summary: rawParams.summary as string,
        contributions: rawParams.contributions as string,
        tags: Array.isArray(rawParams.tags) ? rawParams.tags.map(String) : [],
        conference: (rawParams.conference as string) ?? "",
        status: ((rawParams.status as string) ?? "Unread") as PaperMetadata["status"],
        notes: (rawParams.notes as string) ?? "",
      };

      try {
        const result = await savePaperOnce({ token: notionToken, databaseId: dbId, paper });
        if (result.reused) {
          return { content: [{ type: "text" as const, text: `Paper already exists: ${result.url}` }] };
        }

        logger.info("Saved paper metadata to Notion");

        return {
          content: [
            {
              type: "text" as const,
              text: `Paper saved to Notion.\nTitle: ${paper.title}\nNotion page: ${result.url}`,
            },
          ],
        };
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn("Notion paper save did not complete");
        return {
          content: [
            { type: "text" as const, text: `Failed to save paper to Notion: ${msg}` },
          ],
        };
      }
    },
  };
}

// --- Batch save tool ---

const NotionBatchSaveSchema = {
  type: "object" as const,
  additionalProperties: false,
  required: ["papers"],
  properties: {
    papers: {
      type: "array" as const,
      description:
        "Array of paper objects to save. Each paper has the same fields as notion_save_paper. " +
        "Existing records are looked up by the source and canonical paper URLs.",
      items: {
        type: "object" as const,
        additionalProperties: false,
        required: ["title", "authors", "source_url", "summary", "tags"],
        properties: {
          title: { type: "string" as const, description: "Paper title." },
          authors: { type: "string" as const, description: "Comma-separated author names." },
          institutions: {
            type: "array" as const,
            items: { type: "string" as const },
            description: "Institution abbreviations (e.g., ['MIT', 'Stanford']).",
          },
          published_date: {
            type: "string" as const,
            description: "Publication date YYYY-MM-DD.",
          },
          source_url: { type: "string" as const, description: "Original URL (arXiv, DOI, etc.)." },
          paper_url: { type: "string" as const, description: "Direct paper link." },
          summary: { type: "string" as const, description: "One-sentence summary." },
          contributions: { type: "string" as const, description: "Main contributions (2-3 sentences)." },
          tags: {
            type: "array" as const,
            items: { type: "string" as const },
            description: "Mid-level research area tags.",
          },
          conference: { type: "string" as const, description: "Venue with year (e.g., 'ICLR 2025')." },
          status: {
            type: "string" as const,
            enum: ["Unread", "Reading", "Read"],
            description: "Reading status. Default: Unread.",
          },
          notes: {
            type: "string" as const,
            description: "Personal reading notes or relevance context.",
          },
        },
      },
    },
    database_id: {
      type: "string" as const,
      description: "Override database ID (uses config default if omitted).",
    },
  },
};

export function createNotionBatchSaveTool(params: {
  config: PaperCollectorConfig;
  notionToken: string;
  logger: {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
}) {
  const { config, notionToken, logger } = params;

  return {
    name: "notion_batch_save",
    label: "Batch Save Papers to Notion",
    description:
      "Save multiple research papers to the Notion database in one call. " +
      "Looks up existing records by their source and canonical paper URLs and rate-limits API calls. " +
      "Use this when adding a reading list, survey papers, or multiple references at once.",
    parameters: NotionBatchSaveSchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      if (!notionToken) {
        return {
          content: [
            {
              type: "text" as const,
              text: "Error: NOTION_API_TOKEN not configured.",
            },
          ],
        };
      }

      const dbId =
        (typeof rawParams.database_id === "string" && rawParams.database_id) ||
        config.databaseId;
      if (!dbId) {
        return {
          content: [
            {
              type: "text" as const,
              text: "Error: No database ID configured. Run notion_setup first.",
            },
          ],
        };
      }

      const rawPapers = rawParams.papers;
      if (!Array.isArray(rawPapers) || rawPapers.length === 0) {
        return {
          content: [
            { type: "text" as const, text: "Error: papers array is empty or invalid." },
          ],
        };
      }

      // Map raw params to PaperMetadata
      const papers: PaperMetadata[] = rawPapers.map((p: Record<string, unknown>) => ({
        title: (p.title as string) ?? "",
        authors: (p.authors as string) ?? "",
        institutions: Array.isArray(p.institutions) ? p.institutions.map(String) : [],
        publishedDate: (p.published_date as string) ?? "",
        sourceUrl: (p.source_url as string) ?? "",
        paperUrl: (p.paper_url as string) ?? "",
        summary: (p.summary as string) ?? "",
        contributions: (p.contributions as string) ?? "",
        tags: Array.isArray(p.tags) ? p.tags.map(String) : [],
        conference: (p.conference as string) ?? "",
        status: ((p.status as string) ?? "Unread") as PaperMetadata["status"],
        notes: (p.notes as string) ?? "",
      }));

      logger.info(`Batch saving ${papers.length} papers to Notion...`);

      try {
        const result = await batchSavePapers({
          token: notionToken,
          databaseId: dbId,
          papers,
        });

        // Build summary
        const lines: string[] = [
          `Batch save complete: ${result.succeeded} saved, ${result.skipped} duplicates skipped, ${result.failed} failed.`,
          "",
        ];

        for (const r of result.results) {
          const icon = r.status === "saved" ? "+" : r.status === "duplicate" ? "=" : "x";
          const detail = r.url ? ` → ${r.url}` : r.error ? ` — ${r.error}` : "";
          lines.push(`[${icon}] ${r.title}${detail}`);
        }

        logger.info(
          `Batch save: ${result.succeeded}/${result.total} saved, ${result.skipped} skipped, ${result.failed} failed`,
        );

        return {
          content: [{ type: "text" as const, text: lines.join("\n") }],
        };
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn("Notion batch save did not complete");
        return {
          content: [
            { type: "text" as const, text: `Batch save failed: ${msg}` },
          ],
        };
      }
    },
  };
}

export function createNotionSetupTool(params: {
  config: PaperCollectorConfig;
  notionToken: string;
  logger: {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
}) {
  const { config, notionToken, logger } = params;

  return {
    name: "notion_setup",
    label: "Setup Notion Database",
    description:
      "Create the Paper Collection database in Notion under a specified parent page. " +
      "One-time setup tool. Returns the database ID to save in config.",
    parameters: NotionSetupSchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      if (!notionToken) {
        return {
          content: [
            {
              type: "text" as const,
              text:
                "Error: NOTION_API_TOKEN not configured.\n\n" +
                "Setup instructions:\n" +
                "1. Go to https://www.notion.so/my-integrations\n" +
                "2. Create a new internal integration\n" +
                "3. Copy the Internal Integration Secret\n" +
                "4. Add NOTION_API_TOKEN to your docker-compose environment\n" +
                "5. Share the parent Notion page with your integration\n" +
                "6. Restart the gateway",
            },
          ],
        };
      }

      const parentPageId =
        (typeof rawParams.parent_page_id === "string" && rawParams.parent_page_id) ||
        config.parentPageId;
      if (!parentPageId) {
        return {
          content: [
            {
              type: "text" as const,
              text:
                "Error: No parent page ID provided.\n\n" +
                "To find your Notion page ID:\n" +
                "1. Open the Notion page where you want the database\n" +
                "2. Click Share > Copy link\n" +
                "3. The page ID is the 32-character hex string in the URL\n" +
                "   Example: https://notion.so/My-Page-abc123def456...\n" +
                "4. Pass it as parent_page_id parameter.",
            },
          ],
        };
      }

      try {
        const result = await createDatabase({
          token: notionToken,
          parentPageId,
        });

        logger.info("Created a Notion collection");

        return {
          content: [
            {
              type: "text" as const,
              text:
                `Notion database created successfully.\n` +
                `Database ID: ${result.databaseId}\n\n` +
                `Save this ID in your openclaw.json config under:\n` +
                `plugins.entries.paper-collector.config.databaseId`,
            },
          ],
        };
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn("Notion collection setup did not complete");
        return {
          content: [
            {
              type: "text" as const,
              text:
                `Failed to create Notion database: ${msg}\n\n` +
                "Make sure:\n" +
                "1. The integration has access to the parent page (Share > Invite)\n" +
                "2. The parent page ID is correct\n" +
                "3. The integration token is valid",
            },
          ],
        };
      }
    },
  };
}

// --- Image extraction tool ---

const ExtractPageImagesSchema = {
  type: "object" as const,
  additionalProperties: false,
  required: ["html"],
  properties: {
    html: {
      type: "string" as const,
      description:
        "Raw HTML content from a fetched/opened page. The tool extracts image URLs from it.",
    },
    base_url: {
      type: "string" as const,
      description: "Base URL for resolving relative image paths.",
    },
    limit: {
      type: "number" as const,
      description: "Max images to return (default 10).",
    },
  },
};

export function createExtractPageImagesTool(params: {
  logger: {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
}) {
  const { logger } = params;

  return {
    name: "extract_page_images",
    label: "Extract Images from Page",
    description:
      "Extract image URLs (og:image, img src, data-src) from fetched page HTML. " +
      "Use this when a page lacks paper title/link in text but may contain paper " +
      "figures, architecture diagrams, or screenshots that can identify the paper. " +
      "Returns image URLs that you can then view with web/browser tools to " +
      "visually identify the paper.",
    parameters: ExtractPageImagesSchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      const html = rawParams.html as string;
      if (!html || html.length < 10) {
        return {
          content: [
            {
              type: "text" as const,
              text: "Error: No HTML content provided or content is too short.",
            },
          ],
        };
      }

      const limit =
        typeof rawParams.limit === "number" ? rawParams.limit : undefined;
      const baseUrl =
        typeof rawParams.base_url === "string" ? rawParams.base_url : undefined;

      const images = extractImagesFromHtml(html, { limit, baseUrl });

      if (images.length === 0) {
        logger.info("No content images found in HTML");
        return {
          content: [
            {
              type: "text" as const,
              text:
                "No content images found in the HTML. " +
                "Try using the browser tool to take screenshots of the page instead.",
            },
          ],
        };
      }

      logger.info(`Extracted ${images.length} images from HTML`);

      const lines = images.map((img, i) => {
        const parts = [`[${i + 1}] ${img.url}`, `    source: ${img.source}`];
        if (img.alt) parts.push(`    alt: ${img.alt}`);
        return parts.join("\n");
      });

      return {
        content: [
          {
            type: "text" as const,
            text:
              `Found ${images.length} image(s):\n\n${lines.join("\n\n")}\n\n` +
              "Next steps:\n" +
              "1. Open/fetch the image URLs to view them (especially og:image and the first few content images)\n" +
              "2. Look for: paper titles, author names, arXiv IDs, architecture diagrams, figure captions, method names\n" +
              "3. Use web search with any identified keywords to find the paper",
          },
        ],
      };
    },
  };
}

// --- Paper figure extraction tool ---

const ExtractPaperFiguresSchema = {
  type: "object" as const,
  additionalProperties: false,
  required: ["arxiv_id"],
  properties: {
    arxiv_id: {
      type: "string" as const,
      description:
        "arXiv paper ID (e.g. '2305.00586' or '2506.12963'). " +
        "The tool will try ar5iv HTML, then arXiv HTML, extract all figure image URLs, " +
        "validate their size, and return only usable full-size figures.",
    },
  },
};

/**
 * Programmatically extract paper figures from ar5iv/arxiv HTML.
 * This replaces the unreliable pattern of asking the LLM to parse <img> tags
 * from text-only fetch output (which may not expose raw HTML).
 */
export function createExtractPaperFiguresTool(params: {
  logger: {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
}) {
  const { logger } = params;

  return {
    name: "extract_paper_figures",
    label: "Extract Figures from Paper",
    description:
      "Extract all figure image URLs from an arXiv paper by programmatically fetching " +
      "ar5iv and arXiv HTML pages and parsing <img> tags. Returns only validated, " +
      "full-size figure URLs (≥10KB). Use this BEFORE writing to Notion — it replaces " +
      "manual page fetching and parsing. If this tool returns 0 figures, " +
      "use Browser/PDF screenshots as a fallback.",
    parameters: ExtractPaperFiguresSchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      const arxivId = arxivIdentifier(rawParams.arxiv_id);
      if (!arxivId) {
        return {
          content: [
            { type: "text" as const, text: "Error: supply a valid arXiv identifier." },
          ],
        };
      }

      const { figures: allFigures, errors } = await fetchPaperFigures(arxivId);

      if (allFigures.length === 0) {
        const errSummary =
          errors.length > 0 ? `\n\nSource errors:\n${errors.join("\n")}` : "";
        logger.warn(
          `No valid figures found for ${arxivId}. Errors: ${errors.join("; ")}`,
        );
        return {
          content: [
            {
              type: "text" as const,
              text:
                `No valid figures found from HTML sources for paper ${arxivId}.${errSummary}\n\n` +
                `**Use Browser/PDF screenshots to capture figures from the PDF:**\n` +
                `1. Navigate to https://arxiv.org/pdf/${arxivId}\n` +
                `2. Screenshot page 1 (usually has the main figure)\n` +
                `3. Scroll and screenshot every page with a figure\n` +
                `4. Use the screenshot URLs as ![Figure N: caption](screenshot_url) in your markdown\n\n` +
                `Also try:\n` +
                `- Web search for "{paper title} github" and check repo README figures\n` +
                `- Web search for "{paper title} figures site:paperswithcode.com"`,
            },
          ],
        };
      }

      const lines = allFigures.map((fig, i) => {
        const sizeStr =
          fig.bytes > 0 ? ` (${Math.round(fig.bytes / 1024)}KB)` : "";
        const altStr = fig.alt ? `\n   Caption: ${fig.alt}` : "";
        return `${i + 1}. ![Figure ${i + 1}${fig.alt ? `: ${fig.alt}` : ""}](${fig.url})${sizeStr}${altStr}\n   Source: ${fig.source}`;
      });

      logger.info(
        `Found ${allFigures.length} valid figures for ${arxivId}`,
      );

      return {
        content: [
          {
            type: "text" as const,
            text:
              `Found ${allFigures.length} figure(s) for paper ${arxivId}:\n\n${lines.join("\n\n")}\n\n` +
              `Use these image URLs in your Notion markdown as:\n` +
              `![Fig. N: descriptive caption](url)\n\n` +
              `If the paper has more figures than found above, supplement with Browser/PDF screenshots:\n` +
              `  Open https://arxiv.org/pdf/${arxivId} and screenshot pages with figures.`,
          },
        ],
      };
    },
  };
}

// --- Page content writing tools ---

const MarkdownSchema = {
  type: "string" as const,
  description: "Notion-flavored Markdown with headings, equations, tables and verified figures.",
};
const IncludeImagesSchema = {
  type: "boolean" as const,
  description: "Include verified figures. Defaults to the configured setting.",
};
const NotionWritePageSchema = {
  type: "object" as const,
  additionalProperties: false,
  required: ["page_id", "markdown"],
  properties: {
    page_id: { type: "string" as const, description: "ID of the selected managed block or page to append to." },
    markdown: MarkdownSchema,
    clear_existing: {
      type: "boolean" as const,
      const: false as const,
      description: "Legacy compatibility flag. The write only appends blocks.",
    },
    include_images: IncludeImagesSchema,
  },
};

type ContentToolSettings = {
  config: PaperCollectorConfig;
  notionToken: string;
  logger: {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
};

function contentResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

export function createNotionWritePageTool({ config, notionToken, logger }: ContentToolSettings) {
  return {
    name: "notion_write_page",
    label: "Append Notion Content",
    description: "Append a verified long-form paper write-up to a user-selected managed block or page. Existing blocks are preserved.",
    parameters: NotionWritePageSchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      if (!notionToken) return contentResult("Error: NOTION_API_TOKEN is required.");
      if (rawParams.clear_existing === true) {
        return contentResult("Error: this tool only appends. Select a managed block or use notion_create_child_page.");
      }
      try {
        const pageId = validateNotionId(rawParams.page_id);
        const includeImages = (rawParams.include_images as boolean | undefined) ?? config.includeImages;
        const { blocks } = await prepareNotionContent(rawParams.markdown, includeImages);
        await appendBlocks({ token: notionToken, pageId, blocks });
        logger.info(`Appended ${blocks.length} Notion blocks`);
        return contentResult(`Appended ${blocks.length} blocks.
Destination ID: ${pageId}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Check the destination before retrying.";
        logger.warn("Notion append did not complete");
        return contentResult(`Notion append did not complete: ${message}`);
      }
    },
  };
}

const NotionChildPageSchema = {
  type: "object" as const,
  additionalProperties: false,
  required: ["parent_page_id", "title", "markdown"],
  properties: {
    parent_page_id: { type: "string" as const, description: "ID of the selected Notion parent page." },
    title: { type: "string" as const, description: "Title for the new child page." },
    icon: { type: "string" as const, description: "Optional single emoji for the page icon." },
    markdown: MarkdownSchema,
    include_images: IncludeImagesSchema,
  },
};

export function createNotionCreateChildPageTool({ config, notionToken, logger }: ContentToolSettings) {
  return {
    name: "notion_create_child_page",
    label: "Create Notion Child Page",
    description: "Create a new Notion child page for a requested paper write-up or translation.",
    parameters: NotionChildPageSchema,
    execute: async (_toolCallId: string, rawParams: Record<string, unknown>) => {
      if (!notionToken) return contentResult("Error: NOTION_API_TOKEN is required.");
      try {
        const parentPageId = validateNotionId(rawParams.parent_page_id);
        const title = rawParams.title;
        if (typeof title !== "string" || !title.trim()) throw new Error("A child page title is required");
        const icon = typeof rawParams.icon === "string" ? rawParams.icon : undefined;
        const includeImages = (rawParams.include_images as boolean | undefined) ?? config.includeImages;
        const { blocks } = await prepareNotionContent(rawParams.markdown, includeImages);
        const page = await createChildPage({ token: notionToken, parentPageId, title, icon, blocks });
        if (page.writeError) {
          return contentResult(`Child page created with partial content: ${page.url}
${page.writeError}`);
        }
        logger.info(`Created a Notion child page with ${blocks.length} blocks`);
        return contentResult(`Child page created with ${blocks.length} blocks.
URL: ${page.url}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Check the parent before retrying.";
        logger.warn("Notion child page creation did not complete");
        return contentResult(`Notion child page creation did not complete: ${message}`);
      }
    },
  };
}

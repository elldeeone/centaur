import type {
  Blockquote,
  Code,
  Content,
  Delete,
  Emphasis,
  InlineCode,
  Link,
  List,
  ListItem,
  Paragraph,
  Heading,
  Root,
  Strong,
  Table,
  TableCell,
  TableRow,
  Text,
} from "mdast";
import { toString as mdastToString } from "mdast-util-to-string";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import remarkStringify from "remark-stringify";
import { unified } from "unified";

import type { SlackBlock } from "./types";

export type {
  Blockquote,
  Code,
  Content,
  Delete,
  Emphasis,
  InlineCode,
  Link,
  List,
  ListItem,
  Paragraph,
  Heading,
  Root,
  Strong,
  Table,
  TableCell,
  TableRow,
  Text,
} from "mdast";

const processor = unified().use(remarkParse).use(remarkGfm);
const stringifier = unified().use(remarkStringify, {}).use(remarkGfm);

export function parseMarkdown(markdown: string): Root {
  return processor.parse(markdown);
}

export function stringifyMarkdown(ast: Root): string {
  return stringifier.stringify(ast);
}

export function markdownToPlainText(markdown: string): string {
  return mdastToString(parseMarkdown(markdown));
}

export function isTextNode(node: Content): node is Text {
  return node.type === "text";
}

export function isParagraphNode(node: Content): node is Paragraph {
  return node.type === "paragraph";
}

export function isHeadingNode(node: Content): node is Heading {
  return node.type === "heading";
}

export function isStrongNode(node: Content): node is Strong {
  return node.type === "strong";
}

export function isEmphasisNode(node: Content): node is Emphasis {
  return node.type === "emphasis";
}

export function isDeleteNode(node: Content): node is Delete {
  return node.type === "delete";
}

export function isInlineCodeNode(node: Content): node is InlineCode {
  return node.type === "inlineCode";
}

export function isCodeNode(node: Content): node is Code {
  return node.type === "code";
}

export function isLinkNode(node: Content): node is Link {
  return node.type === "link";
}

export function isBlockquoteNode(node: Content): node is Blockquote {
  return node.type === "blockquote";
}

export function isListNode(node: Content): node is List {
  return node.type === "list";
}

export function isListItemNode(node: Content): node is ListItem {
  return node.type === "listItem";
}

export function isTableNode(node: Content): node is Table {
  return node.type === "table";
}

export function isTableRowNode(node: Content): node is TableRow {
  return node.type === "tableRow";
}

export function isTableCellNode(node: Content): node is TableCell {
  return node.type === "tableCell";
}

export function getNodeChildren(node: Content | Root): Content[] {
  return "children" in node && Array.isArray(node.children) ? (node.children as Content[]) : [];
}

export function slackMrkdwnToMarkdown(mrkdwn: string): string {
  let markdown = mrkdwn;

  markdown = markdown.replace(/<@([A-Z0-9_]+)\|([^<>]+)>/g, "@$2");
  markdown = markdown.replace(/<@([A-Z0-9_]+)>/g, "@$1");
  markdown = markdown.replace(/<#[A-Z0-9_]+\|([^<>]+)>/g, "#$1");
  markdown = markdown.replace(/<#([A-Z0-9_]+)>/g, "#$1");
  markdown = markdown.replace(/<(https?:\/\/[^|<>]+)\|([^<>]+)>/g, "[$2]($1)");
  markdown = markdown.replace(/<(https?:\/\/[^<>]+)>/g, "$1");
  markdown = markdown.replace(/(?<![_*\\])\*([^*\n]+)\*(?![_*])/g, "**$1**");
  markdown = markdown.replace(/(?<!~)~([^~\n]+)~(?!~)/g, "~~$1~~");

  return markdown;
}

export function slackMrkdwnToAst(mrkdwn: string): Root {
  return parseMarkdown(slackMrkdwnToMarkdown(mrkdwn));
}

export function renderMarkdownForSlack(markdown: string): {
  text: string;
  blocks?: SlackBlock[];
} {
  const ast = parseMarkdown(markdown);
  const blocks = astToSlackBlocks(ast);
  return {
    text: astToSlackMrkdwn(ast),
    ...(blocks ? { blocks } : {}),
  };
}

export function astToSlackMrkdwn(ast: Root): string {
  return ast.children.map((child) => nodeToMrkdwn(child as Content)).join("\n\n").trim();
}

function nodeToMrkdwn(node: Content): string {
  if (isParagraphNode(node)) {
    return getNodeChildren(node).map(nodeToMrkdwn).join("");
  }

  if (isHeadingNode(node)) {
    return `*${getNodeChildren(node).map(nodeToMrkdwn).join("")}*`;
  }

  if (isTextNode(node)) return node.value;

  if (isStrongNode(node)) {
    return `*${getNodeChildren(node).map(nodeToMrkdwn).join("")}*`;
  }

  if (isEmphasisNode(node)) {
    return `_${getNodeChildren(node).map(nodeToMrkdwn).join("")}_`;
  }

  if (isDeleteNode(node)) {
    return `~${getNodeChildren(node).map(nodeToMrkdwn).join("")}~`;
  }

  if (isInlineCodeNode(node)) {
    return `\`${node.value}\``;
  }

  if (isCodeNode(node)) {
    return `\`\`\`\n${node.value}\n\`\`\``;
  }

  if (isLinkNode(node)) {
    return `<${node.url}|${getNodeChildren(node).map(nodeToMrkdwn).join("")}>`;
  }

  if (isBlockquoteNode(node)) {
    return getNodeChildren(node)
      .map((child) => nodeToMrkdwn(child).split("\n").map((line) => `> ${line}`).join("\n"))
      .join("\n");
  }

  if (isListNode(node)) return renderList(node, 0);

  if (node.type === "break") return "\n";
  if (node.type === "thematicBreak") return "---";

  if (isTableNode(node)) {
    return `\`\`\`\n${tableToAscii(node)}\n\`\`\``;
  }

  return getNodeChildren(node).map(nodeToMrkdwn).join("");
}

function renderList(node: List, depth: number): string {
  return node.children
    .map((item, index) => renderListItem(item, depth, node.ordered ? `${index + 1}.` : "-"))
    .join("\n");
}

function renderListItem(item: ListItem, depth: number, bullet: string): string {
  const indent = "  ".repeat(depth);
  const lines: string[] = [];

  for (const child of getNodeChildren(item)) {
    if (isListNode(child)) {
      lines.push(renderList(child, depth + 1));
      continue;
    }
    const rendered = nodeToMrkdwn(child);
    if (!rendered.trim()) continue;
    if (lines.length === 0) {
      lines.push(`${indent}${bullet} ${rendered}`);
      continue;
    }
    lines.push(`${indent}  ${rendered}`);
  }

  return lines.join("\n");
}

export function tableToAscii(node: Table): string {
  const rows = node.children.map((row) => row.children.map((cell) => mdastToString(cell)));
  if (rows.length === 0) return "";

  const columnCount = Math.max(...rows.map((row) => row.length));
  const widths = Array.from({ length: columnCount }, (_, index) =>
    Math.max(...rows.map((row) => (row[index] || "").length), 3),
  );

  const format = (row: string[]) => widths.map((width, index) => (row[index] || "").padEnd(width)).join(" | ");
  const header = format(rows[0]);
  const separator = widths.map((width) => "-".repeat(width)).join("-|-");
  const body = rows.slice(1).map(format);
  return [header, separator, ...body].join("\n");
}

function astToSlackBlocks(ast: Root): SlackBlock[] | null {
  const shouldUseBlocks = ast.children.some((node) => {
    const content = node as Content;
    return isHeadingNode(content) || isTableNode(content);
  }) || ast.children.length > 1;
  if (!shouldUseBlocks) return null;

  const blocks: SlackBlock[] = [];
  let usedNativeTable = false;

  for (const child of ast.children) {
    const node = child as Content;
    if (isHeadingNode(node)) {
      pushMrkdwnSections(blocks, nodeToMrkdwn(node));
      continue;
    }

    if (!isTableNode(node)) {
      pushMrkdwnSections(blocks, nodeToMrkdwn(node));
      continue;
    }

    if (usedNativeTable) {
      blocks.push({
        type: "section",
        text: {
          type: "mrkdwn",
          text: `\`\`\`\n${tableToAscii(node)}\n\`\`\``,
        },
      });
      continue;
    }

    blocks.push(mdastTableToSlackBlock(node));
    usedNativeTable = true;
  }

  return blocks;
}

function pushMrkdwnSections(blocks: SlackBlock[], text: string): void {
  for (const chunk of splitMrkdwnSectionText(text)) {
    blocks.push({
      type: "section",
      text: { type: "mrkdwn", text: chunk },
    });
  }
}

function splitMrkdwnSectionText(text: string): string[] {
  const limit = 2900;
  if (text.length <= limit) return [text];

  const chunks: string[] = [];
  let remaining = text;
  while (remaining.length > limit) {
    const newline = remaining.lastIndexOf("\n", limit);
    const space = remaining.lastIndexOf(" ", limit);
    const cut = newline > limit * 0.3 ? newline : space > limit * 0.3 ? space : limit;
    chunks.push(remaining.slice(0, cut).trimEnd());
    remaining = remaining.slice(cut).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

function mdastTableToSlackBlock(node: Table): SlackBlock {
  return {
    type: "table",
    rows: node.children.map((row) =>
      row.children.map((cell) => ({
        type: "raw_text",
        text: mdastToString(cell) || " ",
      })),
    ),
    ...(node.align
      ? {
          column_settings: node.align.map((align) => ({
            align: align || "left",
            is_wrapped: true,
          })),
        }
      : {}),
  };
}

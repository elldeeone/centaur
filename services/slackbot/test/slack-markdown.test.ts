import { describe, expect, it } from "vitest";

import {
  renderMarkdownForSlack,
  splitMarkdownForSlackMessages,
} from "../src/lib/slack/markdown";

describe("Slack markdown rendering", () => {
  it("keeps fenced code languages in markdown blocks for Slack highlighting", () => {
    const rendered = renderMarkdownForSlack([
      "```rust",
      "fn main() {",
      "    println!(\"Hello, world!\");",
      "}",
      "```",
    ].join("\n"));

    expect(rendered.blocks).toEqual([{
      type: "markdown",
      text: [
        "```rust",
        "fn main() {",
        "    println!(\"Hello, world!\");",
        "}",
        "```",
      ].join("\n"),
    }]);
    expect(rendered.text).toBe([
      "fn main() {",
      "    println!(\"Hello, world!\");",
      "}",
    ].join("\n"));
  });

  it("renders markdown tables as native Slack table blocks", () => {
    const rendered = renderMarkdownForSlack([
      "Summary",
      "",
      "| Asset | Value |",
      "| --- | --- |",
      "| BTC | $1.00M |",
    ].join("\n"));

    expect(rendered.blocks?.some((block) => block.type === "table")).toBe(true);
  });

  it("renders headings as separate Slack markdown blocks", () => {
    const rendered = renderMarkdownForSlack([
      "# Summary",
      "",
      "Short intro.",
      "",
      "## Details",
      "",
      "- One",
    ].join("\n"));

    expect(rendered.blocks).toEqual([
      { type: "markdown", text: "# Summary" },
      { type: "markdown", text: "Short intro." },
      { type: "markdown", text: "## Details" },
      { type: "markdown", text: "- One" },
    ]);
  });

  it("renders separate paragraphs as separate Slack markdown blocks", () => {
    const rendered = renderMarkdownForSlack([
      "First paragraph.",
      "",
      "Second paragraph.",
      "",
      "Third paragraph.",
    ].join("\n"));

    expect(rendered.blocks).toEqual([
      { type: "markdown", text: "First paragraph." },
      { type: "markdown", text: "Second paragraph." },
      { type: "markdown", text: "Third paragraph." },
    ]);
  });

  it("renders Amp footers as Slack rich text blocks", () => {
    const rendered = renderMarkdownForSlack([
      "Final answer.",
      "",
      "[View in Amp](https://ampcode.com/threads/T-final-thread) · `amp threads continue T-final-thread` · `490cd7ae`",
    ].join("\n"));

    expect(rendered.blocks).toEqual([
      { type: "markdown", text: "Final answer." },
      {
        type: "rich_text",
        elements: [{
          type: "rich_text_section",
          elements: [
            {
              type: "link",
              url: "https://ampcode.com/threads/T-final-thread",
              text: "View in Amp",
            },
            { type: "text", text: " · " },
            { type: "text", text: "amp threads continue T-final-thread", style: { code: true } },
            { type: "text", text: " · " },
            { type: "text", text: "490cd7ae", style: { code: true } },
          ],
        }],
      },
    ]);
  });

  it("renders mixed final answers into markdown, table, and footer blocks", () => {
    const rendered = renderMarkdownForSlack([
      "# Summary",
      "",
      "```python",
      "print('ok')",
      "```",
      "",
      "| Check | Status |",
      "| --- | --- |",
      "| formatting | ok |",
      "",
      "[View in Amp](https://ampcode.com/threads/T-mixed) · `amp threads continue T-mixed` · `abcdef12`",
    ].join("\n"));

    expect(rendered.blocks?.map((block) => block.type)).toEqual([
      "markdown",
      "markdown",
      "table",
      "rich_text",
    ]);
    expect(rendered.blocks?.[1]).toEqual({
      type: "markdown",
      text: "```python\nprint('ok')\n```",
    });
    expect(rendered.blocks?.[2]).toEqual(expect.objectContaining({ type: "table" }));
    expect(rendered.blocks?.[3]).toEqual(expect.objectContaining({ type: "rich_text" }));
  });

  it("packs Slack markdown messages up to the block budget", () => {
    const markdown = Array.from({ length: 55 }, (_, i) => `Paragraph ${i}.`).join("\n\n");

    const chunks = splitMarkdownForSlackMessages(markdown, { firstMaxBlocks: 49 });

    expect(chunks).toHaveLength(2);
    expect(renderMarkdownForSlack(chunks[0]).blocks).toHaveLength(49);
    expect(renderMarkdownForSlack(chunks[1]).blocks).toHaveLength(6);
  });

  it("splits plain text messages at Slack's markdown block budget", () => {
    const text = "a".repeat(39_999);

    expect(splitMarkdownForSlackMessages(text)).toHaveLength(4);
  });
});

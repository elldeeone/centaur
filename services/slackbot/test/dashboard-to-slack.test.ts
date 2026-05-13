import { describe, it, expect } from "vitest";
import {
  convertDashboardBlocks,
  type ChartRenderer,
} from "../src/lib/bot/dashboard-to-slack";

const expectMarkdown = async (input: string): Promise<string> => {
  const { markdown } = await convertDashboardBlocks(input);
  return markdown;
};

describe("convertDashboardBlocks", () => {
  it("passes through text without dashboard blocks", async () => {
    const input = "Hello world\n\nSome **bold** text";
    expect(await expectMarkdown(input)).toBe(input);
  });

  it("converts a KPI card to formatted text", async () => {
    const input = [
      "Here are the results:",
      "",
      "```dashboard",
      "title: Portfolio Summary",
      "layout: grid-2",
      "---",
      "type: kpi-card",
      "label: Total Value",
      "value: 420632234",
      "format: currency",
      "```",
      "",
      "That's all.",
    ].join("\n");

    const result = await expectMarkdown(input);
    expect(result).not.toContain("```dashboard");
    expect(result).toContain("*Portfolio Summary*");
    expect(result).toContain("*Total Value:*");
    expect(result).toContain("$420.63M");
    expect(result).toContain("That's all.");
  });

  it("converts a data-table to a markdown table", async () => {
    const input = [
      "```dashboard",
      "title: Asset Breakdown",
      "layout: single",
      "---",
      "type: data-table",
      "title: Breakdown",
      "columns: asset:text,quantity:number,price:currency",
      "defaultSort: price,desc",
      "data:",
      "  [2]{asset,quantity,price}:",
      "    BTC,4494.48,73595",
      "    ETH,3199.83,2269.14",
      "```",
    ].join("\n");

    const result = await expectMarkdown(input);
    expect(result).not.toContain("```dashboard");
    expect(result).toContain("*Asset Breakdown*");
    expect(result).toContain("| Asset | Quantity | Price |");
    expect(result).toContain("| --- | --- | --- |");
    expect(result).toContain("BTC");
    expect(result).toContain("ETH");
    // Sorted desc by price — BTC ($73,595) should come first
    const btcIdx = result.indexOf("BTC");
    const ethIdx = result.indexOf("ETH");
    expect(btcIdx).toBeLessThan(ethIdx);
  });

  it("handles multiple KPIs on one line", async () => {
    const input = [
      "```dashboard",
      "title: Summary",
      "layout: grid-2",
      "---",
      "type: kpi-card",
      "label: Total",
      "value: 1000000",
      "format: currency",
      "---",
      "type: kpi-card",
      "label: Count",
      "value: 42",
      "format: number",
      "```",
    ].join("\n");

    const result = await expectMarkdown(input);
    expect(result).toContain("*Total:* $1.00M");
    expect(result).toContain("*Count:* 42");
    expect(result).toContain("·");
  });

  it("handles mixed KPIs and tables", async () => {
    const input = [
      "```dashboard",
      "title: BitGo Assets",
      "layout: grid-2",
      "---",
      "type: kpi-card",
      "label: Total Notional",
      "value: 420632234",
      "format: currency",
      "---",
      "type: kpi-card",
      "label: Assets Held",
      "value: 4",
      "format: number",
      "---",
      "type: data-table",
      "title: Breakdown",
      "columns: asset:text,notional:currency",
      "data:",
      "  [2]{asset,notional}:",
      "    BTC,330771800",
      "    SOL,67466903",
      "```",
    ].join("\n");

    const result = await expectMarkdown(input);
    expect(result).toContain("*Total Notional:* $420.63M");
    expect(result).toContain("*Assets Held:* 4");
    expect(result).toContain("| Asset | Notional |");
    expect(result).toContain("$330.77M");
  });

  it("preserves surrounding text", async () => {
    const input =
      "Before the dashboard\n\n```dashboard\ntitle: Test\nlayout: single\n---\ntype: kpi-card\nlabel: Val\nvalue: 100\nformat: number\n```\n\nAfter the dashboard";

    const result = await expectMarkdown(input);
    expect(result).toContain("Before the dashboard");
    expect(result).toContain("After the dashboard");
    expect(result).toContain("*Val:* 100");
  });

  it("converts multiple dashboard fences in one answer", async () => {
    const input = [
      "Before",
      "",
      "```dashboard",
      "title: First",
      "layout: single",
      "---",
      "type: kpi-card",
      "label: Passed",
      "value: 12",
      "format: number",
      "```",
      "",
      "Middle",
      "",
      "```dashboard",
      "title: Second",
      "layout: single",
      "---",
      "type: data-table",
      "columns: check:text,status:text",
      "data:",
      "  [1]{check,status}:",
      "    markdown,ok",
      "```",
      "",
      "After",
    ].join("\n");

    const result = await expectMarkdown(input);
    expect(result).toContain("Before");
    expect(result).toContain("*First*");
    expect(result).toContain("*Passed:* 12");
    expect(result).toContain("Middle");
    expect(result).toContain("*Second*");
    expect(result).toContain("| Check | Status |");
    expect(result).toContain("| markdown | ok |");
    expect(result).toContain("After");
    expect(result).not.toContain("```dashboard");
  });

  it("leaves unparseable dashboard blocks as-is", async () => {
    const input = "```dashboard\nthis is not valid\n```";
    expect(await expectMarkdown(input)).toBe(input);
  });

  it("formats percent values", async () => {
    const input = [
      "```dashboard",
      "title: Test",
      "layout: single",
      "---",
      "type: data-table",
      "columns: name:text,weight:percent",
      "data:",
      "  [1]{name,weight}:",
      "    BTC,78.6",
      "```",
    ].join("\n");

    const result = await expectMarkdown(input);
    expect(result).toContain("78.6%");
  });

  // ── New: chart → file path ─────────────────────────────────────────────

  it("with a renderer, converts a line-chart block to a file attachment", async () => {
    const input = [
      "Look at this trend:",
      "",
      "```dashboard",
      "title: BTC 30d",
      "layout: single",
      "---",
      "type: line-chart",
      "title: BTC Price",
      "data:",
      "  [3]{date,price}:",
      "    2026-04-01,65000",
      "    2026-04-15,69000",
      "    2026-04-30,72000",
      "```",
      "",
    ].join("\n");

    const fakePng = Buffer.from("fake-png-bytes");
    const renderer: ChartRenderer = async (chart) => {
      expect(chart.type).toBe("line-chart");
      expect(chart.title).toBe("BTC Price");
      return fakePng;
    };

    const { markdown, files } = await convertDashboardBlocks(input, {
      renderChart: renderer,
    });

    expect(files).toHaveLength(1);
    expect(files[0].mimeType).toBe("image/png");
    expect(files[0].filename).toContain("btc-price");
    expect(files[0].data).toBe(fakePng);

    expect(markdown).toContain("Look at this trend:");
    expect(markdown).toContain("*BTC 30d*");
    // The chart placeholder text should be GONE — we render to a file instead.
    expect(markdown).not.toContain("(chart — view in Thread Viewer)");
    expect(markdown).not.toContain("```dashboard");
  });

  it("falls back to placeholder when no renderer is provided", async () => {
    const input = [
      "```dashboard",
      "title: Token mix",
      "layout: single",
      "---",
      "type: pie-chart",
      "title: Holdings",
      "data:",
      "  [2]{asset,value}:",
      "    BTC,5",
      "    ETH,3",
      "```",
    ].join("\n");

    const { markdown, files } = await convertDashboardBlocks(input);
    expect(files).toHaveLength(0);
    expect(markdown).toContain("Holdings (chart — view in Thread Viewer)");
  });

  it("falls back to placeholder when the renderer returns null", async () => {
    const input = [
      "```dashboard",
      "title: A",
      "layout: single",
      "---",
      "type: bar-chart",
      "title: Latency",
      "data:",
      "  [1]{label,ms}:",
      "    p99,1200",
      "```",
    ].join("\n");

    const { markdown, files } = await convertDashboardBlocks(input, {
      renderChart: async () => null,
    });
    expect(files).toHaveLength(0);
    expect(markdown).toContain("Latency (chart — view in Thread Viewer)");
  });

  it("renders chart files alongside non-chart components", async () => {
    const input = [
      "```dashboard",
      "title: Daily",
      "layout: grid-2",
      "---",
      "type: kpi-card",
      "label: Volume",
      "value: 5000000",
      "format: currency",
      "---",
      "type: line-chart",
      "title: Price 24h",
      "data:",
      "  [2]{t,p}:",
      "    9am,100",
      "    5pm,110",
      "```",
    ].join("\n");

    const { markdown, files } = await convertDashboardBlocks(input, {
      renderChart: async () => Buffer.from("png"),
    });

    expect(files).toHaveLength(1);
    expect(files[0].filename).toContain("price-24h");
    expect(markdown).toContain("*Volume:* $5.00M");
    expect(markdown).not.toContain("(chart");
  });

  it("keeps an unrendered chart fallback even if another chart has the same title", async () => {
    const input = [
      "```dashboard",
      "title: Duplicate titles",
      "layout: single",
      "---",
      "type: line-chart",
      "title: Same Title",
      "data:",
      "  [2]{date,price}:",
      "    2026-04-01,100",
      "    2026-04-02,110",
      "---",
      "type: bar-chart",
      "title: Same Title",
      "data:",
      "  [1]{label,value}:",
      "    BTC,100",
      "```",
    ].join("\n");

    let calls = 0;
    const { markdown, files } = await convertDashboardBlocks(input, {
      renderChart: async () => {
        calls += 1;
        return calls === 1 ? Buffer.from("png") : null;
      },
    });

    expect(files).toHaveLength(1);
    expect(markdown).toContain("Same Title (chart — view in Thread Viewer)");
  });
});

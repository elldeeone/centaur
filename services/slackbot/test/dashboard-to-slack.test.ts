import { describe, it, expect } from "vitest";
import { convertDashboardBlocks } from "../src/lib/bot/dashboard-to-slack";

describe("convertDashboardBlocks", () => {
  it("passes through text without dashboard blocks", () => {
    const input = "Hello world\n\nSome **bold** text";
    expect(convertDashboardBlocks(input)).toBe(input);
  });

  it("converts a KPI card to formatted text", () => {
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

    const result = convertDashboardBlocks(input);
    expect(result).not.toContain("```dashboard");
    expect(result).toContain("*Portfolio Summary*");
    expect(result).toContain("*Total Value:*");
    expect(result).toContain("$420.63M");
    expect(result).toContain("That's all.");
  });

  it("converts a data-table to a markdown table", () => {
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

    const result = convertDashboardBlocks(input);
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

  it("handles multiple KPIs on one line", () => {
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

    const result = convertDashboardBlocks(input);
    expect(result).toContain("*Total:* $1.00M");
    expect(result).toContain("*Count:* 42");
    // KPIs should be joined with separator
    expect(result).toContain("·");
  });

  it("handles mixed KPIs and tables", () => {
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

    const result = convertDashboardBlocks(input);
    expect(result).toContain("*Total Notional:* $420.63M");
    expect(result).toContain("*Assets Held:* 4");
    expect(result).toContain("| Asset | Notional |");
    expect(result).toContain("$330.77M");
  });

  it("preserves surrounding text", () => {
    const input = "Before the dashboard\n\n```dashboard\ntitle: Test\nlayout: single\n---\ntype: kpi-card\nlabel: Val\nvalue: 100\nformat: number\n```\n\nAfter the dashboard";

    const result = convertDashboardBlocks(input);
    expect(result).toContain("Before the dashboard");
    expect(result).toContain("After the dashboard");
    expect(result).toContain("*Val:* 100");
  });

  it("leaves unparseable dashboard blocks as-is", () => {
    const input = "```dashboard\nthis is not valid\n```";
    expect(convertDashboardBlocks(input)).toBe(input);
  });

  it("formats percent values", () => {
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

    const result = convertDashboardBlocks(input);
    expect(result).toContain("78.6%");
  });
});

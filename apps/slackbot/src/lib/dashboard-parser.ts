import { decode } from "@toon-format/toon";

import type {
  BarChartProps,
  CellFormat,
  ColumnDef,
  DashboardComponent,
  DashboardSpec,
  DataTableProps,
  KPICardProps,
  LineChartProps,
  PieChartProps,
} from "./dashboard-types";

const VALID_FORMATS: Set<string> = new Set([
  "currency",
  "percent",
  "number",
  "date",
  "text",
]);

const VALID_LAYOUTS: Set<string> = new Set(["single", "grid-2", "grid-3"]);

function parseKeyValue(line: string): [string, string] | null {
  const idx = line.indexOf(":");
  if (idx === -1) return null;
  const key = line.slice(0, idx).trim();
  const value = line.slice(idx + 1).trim();
  return [key, value];
}

function parseCellFormat(raw: string): CellFormat {
  return VALID_FORMATS.has(raw) ? (raw as CellFormat) : "text";
}

function parseColumns(raw: string): ColumnDef[] {
  return raw.split(",").map((part) => {
    const trimmed = part.trim();
    const [key, fmt] = trimmed.split(":");
    const format = fmt ? parseCellFormat(fmt) : "text";
    const label = key.charAt(0).toUpperCase() + key.slice(1);
    return { key, label, format, sortable: true };
  });
}

function parseBool(raw: string): boolean {
  return raw.toLowerCase() === "true";
}

function decodeToonData(raw: string): Record<string, unknown>[] | null {
  // Try wrapping as a nested TOON value first
  try {
    const wrapped = `_:\n${raw
      .split("\n")
      .map((line) => `  ${line}`)
      .join("\n")}`;
    const result = decode(wrapped, { strict: false });
    if (result && typeof result === "object" && "_" in result) {
      const val = (result as Record<string, unknown>)["_"];
      if (Array.isArray(val)) return val as Record<string, unknown>[];
    }
  } catch {
    // Wrapped decode failed — fall through to direct decode
  }

  // Try direct decode (handles TOON tabular format like [N]{keys}: ...)
  try {
    const direct = decode(raw, { strict: false });
    if (Array.isArray(direct)) return direct as Record<string, unknown>[];
  } catch {
    // Direct decode also failed
  }

  // Try JSON fallback
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed as Record<string, unknown>[];
  } catch {
    // Not valid JSON either
  }

  return null;
}

function parseComponentSection(section: string): DashboardComponent | null {
  const lines = section.split("\n");
  const kv: Record<string, string> = {};
  let dataBlock: string | null = null;
  let inData = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (inData) {
      if (dataBlock === null) dataBlock = "";
      dataBlock += (dataBlock ? "\n" : "") + line;
      continue;
    }

    const parsed = parseKeyValue(line);
    if (!parsed) continue;
    const [key, value] = parsed;

    if (key === "data") {
      if (value) {
        dataBlock = value;
      } else {
        inData = true;
      }
      continue;
    }

    kv[key] = value;
  }

  const type = kv["type"];
  if (!type) return null;

  const data = dataBlock ? decodeToonData(dataBlock) : undefined;

  switch (type) {
    case "data-table": {
      if (!kv["columns"]) return null;
      const result: DataTableProps = {
        type: "data-table",
        columns: parseColumns(kv["columns"]),
        data: data ?? [],
      };
      if (kv["title"]) result.title = kv["title"];
      if (kv["searchable"] !== undefined)
        result.searchable = parseBool(kv["searchable"]);
      if (kv["defaultSort"]) {
        const [key, direction] = kv["defaultSort"].split(",").map((s) => s.trim());
        if (key && (direction === "asc" || direction === "desc")) {
          result.defaultSort = { key, direction };
        }
      }
      return result;
    }

    case "kpi-card": {
      if (!kv["label"] || kv["value"] === undefined) return null;
      const result: KPICardProps = {
        type: "kpi-card",
        label: kv["label"],
        value: Number(kv["value"]),
        format: parseCellFormat(kv["format"] ?? "number"),
      };
      if (kv["delta"] !== undefined) result.delta = Number(kv["delta"]);
      return result;
    }

    case "line-chart": {
      if (!kv["title"] || !kv["xKey"] || !kv["yKeys"]) return null;
      const result: LineChartProps = {
        type: "line-chart",
        title: kv["title"],
        xKey: kv["xKey"],
        yKeys: kv["yKeys"].split(",").map((s) => s.trim()),
        data: data ?? [],
      };
      if (kv["xFormat"]) result.xFormat = parseCellFormat(kv["xFormat"]);
      if (kv["yFormat"]) result.yFormat = parseCellFormat(kv["yFormat"]);
      return result;
    }

    case "bar-chart": {
      if (!kv["title"] || !kv["categoryKey"] || !kv["valueKey"]) return null;
      const result: BarChartProps = {
        type: "bar-chart",
        title: kv["title"],
        categoryKey: kv["categoryKey"],
        valueKey: kv["valueKey"],
        data: data ?? [],
      };
      return result;
    }

    case "pie-chart": {
      if (!kv["title"] || !kv["labelKey"] || !kv["valueKey"]) return null;
      const result: PieChartProps = {
        type: "pie-chart",
        title: kv["title"],
        labelKey: kv["labelKey"],
        valueKey: kv["valueKey"],
        data: data ?? [],
      };
      return result;
    }

    default:
      return null;
  }
}

export function parseDashboardSpec(raw: string): DashboardSpec | null {
  try {
    const sections = raw.split("\n---\n");
    if (sections.length < 2) return null;

    const headerLines = sections[0].split("\n");
    const header: Record<string, string> = {};
    for (const line of headerLines) {
      const parsed = parseKeyValue(line);
      if (parsed) header[parsed[0]] = parsed[1];
    }

    const title = header["title"];
    if (!title) return null;

    const layout = VALID_LAYOUTS.has(header["layout"] ?? "")
      ? (header["layout"] as DashboardSpec["layout"])
      : "single";

    const components: DashboardComponent[] = [];
    for (let i = 1; i < sections.length; i++) {
      const component = parseComponentSection(sections[i].trim());
      if (component) components.push(component);
    }

    if (components.length === 0) return null;

    return { title, layout, components };
  } catch {
    return null;
  }
}

const DASHBOARD_REGEX = /```dashboard\n([\s\S]*?)```/g;

export function extractDashboardBlocks(
  markdown: string,
): { before: string; spec: DashboardSpec; after: string }[] {
  const results: { before: string; spec: DashboardSpec; after: string }[] = [];
  let lastIndex = 0;

  for (const match of markdown.matchAll(DASHBOARD_REGEX)) {
    const spec = parseDashboardSpec(match[1]);
    if (!spec || match.index === undefined) continue;

    const before = markdown.slice(lastIndex, match.index);
    lastIndex = match.index + match[0].length;
    results.push({ before, spec, after: "" });
  }

  if (results.length > 0) {
    results[results.length - 1].after = markdown.slice(lastIndex);
  }

  return results;
}

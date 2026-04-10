/**
 * Convert ```dashboard fenced blocks into Slack-friendly markdown.
 *
 * Dashboard blocks are a custom format used by the agent to emit rich
 * interactive tables, KPI cards, and charts. Structured Centaur clients can
 * render these directly; Slack gets a plain markdown fallback so the existing
 * table→Block Kit pipeline can pick them up automatically.
 */

import { decode } from "@toon-format/toon";

const DASHBOARD_REGEX = /```dashboard\n([\s\S]*?)```/g;

type CellFormat = "currency" | "compact-currency" | "percent" | "number" | "date" | "text";

interface ColumnDef {
  key: string;
  label: string;
  format: CellFormat;
}

interface DataTableProps {
  type: "data-table";
  title?: string;
  columns: ColumnDef[];
  data: Record<string, unknown>[];
  defaultSort?: { key: string; direction: "asc" | "desc" };
}

interface KPICardProps {
  type: "kpi-card";
  label: string;
  value: number;
  format: CellFormat;
  delta?: number;
}

interface ChartProps {
  type: "line-chart" | "bar-chart" | "pie-chart";
  title: string;
  data: Record<string, unknown>[];
  [key: string]: unknown;
}

type DashboardComponent = DataTableProps | KPICardProps | ChartProps;

interface DashboardSpec {
  title: string;
  components: DashboardComponent[];
}

// ── Parsing for dashboard fenced blocks ───────────────────────────────────

function parseKeyValue(line: string): [string, string] | null {
  const idx = line.indexOf(":");
  if (idx === -1) return null;
  return [line.slice(0, idx).trim(), line.slice(idx + 1).trim()];
}

const VALID_FORMATS = new Set(["currency", "compact-currency", "percent", "number", "date", "text"]);

function parseCellFormat(raw: string): CellFormat {
  return VALID_FORMATS.has(raw) ? (raw as CellFormat) : "text";
}

function parseColumns(raw: string): ColumnDef[] {
  return raw.split(",").map((part) => {
    const trimmed = part.trim();
    const [key, fmt] = trimmed.split(":");
    return {
      key,
      label: key.charAt(0).toUpperCase() + key.slice(1),
      format: fmt ? parseCellFormat(fmt) : "text",
    };
  });
}

function dedent(raw: string): string {
  const lines = raw.split("\n");
  const indents = lines.filter((l) => l.trim().length > 0).map((l) => l.match(/^(\s*)/)![1].length);
  const min = indents.length > 0 ? Math.min(...indents) : 0;
  return min > 0 ? lines.map((l) => l.slice(min)).join("\n") : raw;
}

function decodeToonData(raw: string): Record<string, unknown>[] | null {
  const dedented = dedent(raw);

  try {
    const direct = decode(dedented, { strict: false });
    if (Array.isArray(direct) && direct.length > 0) return direct as Record<string, unknown>[];
  } catch { /* noop */ }

  try {
    const wrapped = `_:\n${dedented.split("\n").map((line) => `  ${line}`).join("\n")}`;
    const result = decode(wrapped, { strict: false });
    if (result && typeof result === "object" && "_" in result) {
      const val = (result as Record<string, unknown>)["_"];
      if (Array.isArray(val) && val.length > 0) return val as Record<string, unknown>[];
    }
  } catch { /* noop */ }

  try {
    const parsed = JSON.parse(dedented);
    if (Array.isArray(parsed)) return parsed as Record<string, unknown>[];
  } catch { /* noop */ }

  return null;
}

function parseComponentSection(section: string): DashboardComponent | null {
  const lines = section.split("\n");
  const kv: Record<string, string> = {};
  let dataBlock: string | null = null;
  let inData = false;

  for (const line of lines) {
    if (inData) {
      if (dataBlock === null) dataBlock = "";
      dataBlock += (dataBlock ? "\n" : "") + line;
      continue;
    }
    const parsed = parseKeyValue(line);
    if (!parsed) continue;
    const [key, value] = parsed;
    if (key === "data") {
      if (value) dataBlock = value;
      else inData = true;
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
      const result: DataTableProps = { type: "data-table", columns: parseColumns(kv["columns"]), data: data ?? [] };
      if (kv["title"]) result.title = kv["title"];
      if (kv["defaultSort"]) {
        const [key, direction] = kv["defaultSort"].split(",").map((s) => s.trim());
        if (key && (direction === "asc" || direction === "desc")) result.defaultSort = { key, direction };
      }
      return result;
    }
    case "kpi-card": {
      if (!kv["label"] || kv["value"] === undefined) return null;
      return {
        type: "kpi-card",
        label: kv["label"],
        value: Number(kv["value"]),
        format: parseCellFormat(kv["format"] ?? "number"),
        ...(kv["delta"] !== undefined ? { delta: Number(kv["delta"]) } : {}),
      };
    }
    case "line-chart":
    case "bar-chart":
    case "pie-chart":
      return { type, title: kv["title"] || type, data: data ?? [], ...kv } as ChartProps;
    default:
      return null;
  }
}

function parseDashboardSpec(raw: string): DashboardSpec | null {
  try {
    const sections = raw.split("\n---\n");
    if (sections.length < 2) return null;

    const headerLines = sections[0].split("\n");
    const header: Record<string, string> = {};
    for (const line of headerLines) {
      const parsed = parseKeyValue(line);
      if (parsed) header[parsed[0]] = parsed[1];
    }
    if (!header["title"]) return null;

    const components: DashboardComponent[] = [];
    for (let i = 1; i < sections.length; i++) {
      const component = parseComponentSection(sections[i].trim());
      if (component) components.push(component);
    }
    if (components.length === 0) return null;

    return { title: header["title"], components };
  } catch {
    return null;
  }
}

// ── Formatting ───────────────────────────────────────────────────────────

function formatValue(value: unknown, format: CellFormat): string {
  if (value === null || value === undefined) return "";
  const num = typeof value === "number" ? value : Number(value);

  switch (format) {
    case "currency":
      if (isNaN(num)) return String(value);
      return num >= 1e9 ? `$${(num / 1e9).toFixed(2)}B`
        : num >= 1e6 ? `$${(num / 1e6).toFixed(2)}M`
        : num >= 1e3 ? `$${(num / 1e3).toFixed(1)}K`
        : `$${num.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    case "compact-currency":
      if (isNaN(num)) return String(value);
      return num >= 1e9 ? `$${(num / 1e9).toFixed(1)}B`
        : num >= 1e6 ? `$${(num / 1e6).toFixed(1)}M`
        : num >= 1e3 ? `$${(num / 1e3).toFixed(0)}K`
        : `$${Math.round(num)}`;
    case "percent":
      return isNaN(num) ? String(value) : `${num.toFixed(1)}%`;
    case "number":
      return isNaN(num) ? String(value) : num.toLocaleString("en-US");
    case "date":
      return String(value);
    default:
      return String(value);
  }
}

function componentToSlackMarkdown(component: DashboardComponent): string {
  switch (component.type) {
    case "kpi-card": {
      const val = formatValue(component.value, component.format);
      const delta = component.delta !== undefined ? ` (${component.delta > 0 ? "+" : ""}${component.delta}%)` : "";
      return `*${component.label}:* ${val}${delta}`;
    }
    case "data-table": {
      const { columns, data, title, defaultSort } = component;
      if (!data.length) return title ? `*${title}*\n_No data_` : "_No data_";

      let rows = [...data];
      if (defaultSort) {
        rows.sort((a, b) => {
          const av = a[defaultSort.key], bv = b[defaultSort.key];
          const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
          return defaultSort.direction === "desc" ? -cmp : cmp;
        });
      }

      // Build a standard markdown table
      const header = `| ${columns.map((c) => c.label).join(" | ")} |`;
      const separator = `| ${columns.map(() => "---").join(" | ")} |`;
      const bodyRows = rows.map(
        (row) => `| ${columns.map((c) => formatValue(row[c.key], c.format)).join(" | ")} |`,
      );

      const parts: string[] = [];
      if (title) parts.push(`*${title}*`);
      parts.push(header, separator, ...bodyRows);
      return parts.join("\n");
    }
    case "line-chart":
    case "bar-chart":
    case "pie-chart":
      return `_${component.title} (chart — view in Thread Viewer)_`;
    default:
      return "";
  }
}

function dashboardToSlackMarkdown(spec: DashboardSpec): string {
  const parts: string[] = [`*${spec.title}*`];

  // Group KPI cards on one line, tables separately
  const kpis = spec.components.filter((c): c is KPICardProps => c.type === "kpi-card");
  const others = spec.components.filter((c) => c.type !== "kpi-card");

  if (kpis.length > 0) {
    parts.push(kpis.map((k) => componentToSlackMarkdown(k)).join("  ·  "));
  }

  for (const component of others) {
    parts.push(componentToSlackMarkdown(component));
  }

  return parts.join("\n\n");
}

// ── Public API ───────────────────────────────────────────────────────────

/**
 * Replace all ```dashboard blocks in markdown with Slack-friendly equivalents.
 * Returns the transformed markdown with dashboard blocks replaced by
 * markdown tables and formatted KPI text.
 */
export function convertDashboardBlocks(markdown: string): string {
  return markdown.replace(DASHBOARD_REGEX, (_match, content: string) => {
    const spec = parseDashboardSpec(content);
    if (!spec) return _match; // Leave unparseable blocks as-is
    return dashboardToSlackMarkdown(spec);
  });
}

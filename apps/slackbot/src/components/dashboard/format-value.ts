import type { CellFormat } from "./types";

const currencyFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const compactCurrencyFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  compactDisplay: "short",
  maximumFractionDigits: 1,
});

const numberFmt = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2,
});

export function formatValue(value: unknown, format: CellFormat): string {
  if (value == null) return "—";

  switch (format) {
    case "currency": {
      const n = Number(value);
      return isNaN(n) ? String(value) : currencyFmt.format(n);
    }
    case "compact-currency": {
      const n = Number(value);
      return isNaN(n) ? String(value) : compactCurrencyFmt.format(n);
    }
    case "percent": {
      let n = Number(value);
      if (isNaN(n)) return String(value);
      if (Math.abs(n) < 1) n = n * 100;
      const sign = n > 0 ? "+" : "";
      return `${sign}${n.toFixed(1)}%`;
    }
    case "number": {
      const n = Number(value);
      return isNaN(n) ? String(value) : numberFmt.format(n);
    }
    case "date": {
      const d = new Date(value as string | number);
      return isNaN(d.getTime()) ? String(value) : d.toLocaleDateString();
    }
    case "text":
    default:
      return String(value);
  }
}

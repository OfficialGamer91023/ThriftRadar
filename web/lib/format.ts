import type { Listing, Source } from "./types";

export function price(l: Pick<Listing, "price_amount" | "currency" | "price_on_request">): string | null {
  if (l.price_amount != null) {
    const cur = !l.currency || l.currency === "PKR" ? "Rs" : l.currency;
    return `${cur} ${l.price_amount.toLocaleString("en-US")}`;
  }
  return l.price_on_request ? "Price on request" : null;
}

export function title(l: Pick<Listing, "brand" | "model">): string {
  return [l.brand, l.model].filter(Boolean).join(" ") || "Unknown shoe";
}

export const SOURCE_LABEL: Record<Source, string> = {
  caption: "seller's text",
  ocr: "size tag",
  vlm: "AI, from photos",
  siglip: "AI guess (look)",
  knn: "like similar listings",
  seed: "demo data",
};

export function sizeText(l: Pick<Listing, "size_label" | "size_eu" | "size_approx" | "sources">): string {
  if (!l.size_label) return "Size unknown";
  let s = l.size_label;
  if (l.size_eu != null && !l.size_label.startsWith("EU")) s += ` · ≈ EU ${l.size_eu}`;
  if (l.sources.size === "vlm") s += " (read by AI)";
  return s;
}

export function ago(iso: string, now = Date.now()): string {
  const minutes = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60000));
  if (minutes < 1) return "just now";
  let value = minutes;
  let unit = "minute";
  if (minutes >= 60 * 24 * 30) {
    value = Math.round(minutes / (60 * 24 * 30));
    unit = "month";
  } else if (minutes >= 60 * 24) {
    value = Math.round(minutes / (60 * 24));
    unit = "day";
  } else if (minutes >= 60) {
    value = Math.round(minutes / 60);
    unit = "hour";
  }
  return `${value} ${unit}${value === 1 ? "" : "s"} ago`;
}

export function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

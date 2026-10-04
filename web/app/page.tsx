"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ListingCard from "@/components/ListingCard";
import Notice from "@/components/Notice";
import { apiFetch, errorText } from "@/lib/api";
import type { Listing, Page, SearchResult } from "@/lib/types";

function exact(l: Listing, size: number | null | undefined): boolean {
  return size != null && l.size_eu != null && Math.abs(l.size_eu - size) < 0.01;
}

type Mode = { kind: "feed" } | { kind: "text"; label: string } | { kind: "photo"; label: string };

export default function FeedPage() {
  const [q, setQ] = useState("");
  const [size, setSize] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  const [brand, setBrand] = useState("");
  const [mode, setMode] = useState<Mode>({ kind: "feed" });
  const [items, setItems] = useState<Listing[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [filters, setFilters] = useState<SearchResult["filters"] | null>(null);
  const [order, setOrder] = useState<"similar" | "newest">("similar");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadFeed = useCallback(async (cursor: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const page = await apiFetch<Page<Listing>>(
        `/api/listings?limit=24${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      );
      setItems((prev) => (cursor ? [...prev, ...page.results] : page.results));
      setNext(page.next ?? null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadFeed(null);
  }, [loadFeed]);

  async function onSearch(e: React.FormEvent) {
    e.preventDefault();
    const sizeNum = size.replace(/[^\d.]/g, ""); // "EU 40" -> "40"
    if (!q.trim() && !sizeNum && !maxPrice && !brand.trim()) {
      clearSearch();
      return;
    }
    const params = new URLSearchParams({ limit: "96" });
    if (q.trim()) params.set("q", q.trim());
    if (sizeNum) params.set("size", sizeNum);
    if (maxPrice) params.set("max_price", maxPrice);
    if (brand.trim()) params.set("brand", brand.trim());
    setBusy(true);
    setError(null);
    try {
      const r = await apiFetch<SearchResult>(`/api/search?${params}`);
      setItems(r.results);
      setFilters(r.filters ?? null);
      setNext(null);
      setOrder(r.order ?? "similar");
      setMode({ kind: "text", label: q.trim() });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function onPhoto(file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setBusy(true);
    setError(null);
    try {
      const r = await apiFetch<SearchResult>("/api/search/image?limit=48", { method: "POST", body: form });
      setItems(r.results);
      setFilters(null);
      setNext(null);
      setMode({ kind: "photo", label: file.name });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function clearSearch() {
    setQ("");
    setSize("");
    setMaxPrice("");
    setBrand("");
    setFilters(null);
    setMode({ kind: "feed" });
    void loadFeed(null);
  }

  return (
    <div className="stack" style={{ gap: 18 }}>
      <form className="search" onSubmit={onSearch} role="search">
        <div className="field q">
          <label htmlFor="q">Search</label>
          <input id="q" className="input" value={q} onChange={(e) => setQ(e.target.value)} maxLength={200}
            placeholder='e.g. "white nike size 42 under 5000"' />
        </div>
        <div className="field">
          <label htmlFor="size">EU size</label>
          <input id="size" className="input" inputMode="decimal" value={size} onChange={(e) => setSize(e.target.value)} placeholder="e.g. 42" />
        </div>
        <div className="field">
          <label htmlFor="max">Max Rs</label>
          <input id="max" className="input" inputMode="numeric" value={maxPrice} onChange={(e) => setMaxPrice(e.target.value.replace(/\D/g, ""))} placeholder="any" />
        </div>
        <div className="field">
          <label htmlFor="brand">Brand</label>
          <input id="brand" className="input" value={brand} onChange={(e) => setBrand(e.target.value)} placeholder="any" />
        </div>
        <button className="btn primary" type="submit" disabled={busy}>Search</button>
        <button className="btn" type="button" disabled={busy} onClick={() => fileRef.current?.click()}>
          Search by photo
        </button>
        <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" className="sr" id="photo"
          aria-label="Photo to search with" onChange={(e) => onPhoto(e.target.files?.[0])} />
      </form>

      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="row">
          <h1 style={{ fontSize: "1.6rem" }}>
            {mode.kind === "feed" ? "Latest listings" : mode.kind === "text" ? "Search results" : "Similar to your photo"}
          </h1>
          {mode.kind !== "feed" && (
            <button className="btn ghost" type="button" onClick={clearSearch}>Clear search</button>
          )}
        </div>
        {filters && (filters.brand || filters.size_eu || filters.max_price) && (
          <div className="chips" aria-label="Filters applied">
            {filters.brand && <span className="chip">brand <b>{filters.brand}</b></span>}
            {filters.size_eu && <span className="chip">size <b>EU {filters.size_eu}</b></span>}
            {filters.max_price && <span className="chip">under <b>Rs {filters.max_price.toLocaleString("en-US")}</b></span>}
            <span className="chip">{order === "newest" ? "newest first" : "most similar first"}</span>
          </div>
        )}
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {!error && !busy && items.length === 0 && (
        <div className="empty">
          {mode.kind === "feed" ? "No listings yet. They appear here as posts are processed." : "Nothing matched. Try fewer words or remove a filter."}
        </div>
      )}
      {filters?.size_eu ? (
        <>
          <div className="grid" aria-busy={busy}>
            {items.filter((l) => exact(l, filters.size_eu)).map((l) => <ListingCard key={l.id} l={l} />)}
          </div>
          {!busy && !items.some((l) => exact(l, filters.size_eu)) && items.length > 0 && (
            <p className="muted">No listing is marked EU {filters.size_eu} exactly.</p>
          )}
          {items.some((l) => !exact(l, filters.size_eu)) && (
            <>
              <div className="stack" style={{ gap: 4, marginTop: 8 }}>
                <h2>Might fit</h2>
                <p className="muted">Sizes read by AI that are within one size, then listings whose size nobody stated.</p>
              </div>
              <div className="grid">
                {items.filter((l) => !exact(l, filters.size_eu)).map((l) => <ListingCard key={l.id} l={l} />)}
              </div>
            </>
          )}
        </>
      ) : (
        <div className="grid" aria-busy={busy}>
          {items.map((l) => <ListingCard key={l.id} l={l} />)}
        </div>
      )}
      {mode.kind === "feed" && next && (
        <div className="row" style={{ justifyContent: "center" }}>
          <button className="btn" type="button" disabled={busy} onClick={() => loadFeed(next)}>
            {busy ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </div>
  );
}

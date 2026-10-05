"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ListingCard from "@/components/ListingCard";
import Notice from "@/components/Notice";
import { apiFetch, errorText } from "@/lib/api";
import { useConfig } from "@/lib/config";
import type { Page, Wishlist } from "@/lib/types";

function describe(w: Wishlist): string {
  const parts = [];
  if (w.brand) parts.push(w.brand);
  if (w.size_eu) parts.push(`EU ${w.size_eu}`);
  if (w.max_price) parts.push(`under Rs ${w.max_price.toLocaleString("en-US")}`);
  return parts.length ? parts.join(" · ") : "any brand, size and price";
}

export default function WishlistsPage() {
  const demo = useConfig()?.demo ?? false;
  const [items, setItems] = useState<Wishlist[] | null>(null);
  const [open, setOpen] = useState<Wishlist | null>(null);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [size, setSize] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  const [brand, setBrand] = useState("");
  const [photoSize, setPhotoSize] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setItems((await apiFetch<Page<Wishlist>>("/api/wishlists")).results);
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function created(w: Wishlist) {
    const fromPhoto = w.brand_from_photo ? `Brand read from the photo: ${w.brand}, so only ${w.brand} listings will match. ` : "";
    setDone(`Added. ${fromPhoto}${w.matches} existing listing${w.matches === 1 ? "" : "s"} already match (you won't be notified about those). ${demo ? "New matching posts you simulate will show up here." : "New matching posts will send a Mac notification."}`);
    await load();
  }

  async function addText(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const body: Record<string, unknown> = { text: text.trim() };
      if (size) body.size = Number(size);
      if (maxPrice) body.max_price = Number(maxPrice);
      if (brand.trim()) body.brand = brand.trim();
      await created(await apiFetch<Wishlist>("/api/wishlists", { method: "POST", body: JSON.stringify(body) }));
      setText("");
      setSize("");
      setMaxPrice("");
      setBrand("");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function addPhoto(e: React.FormEvent) {
    e.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setError("Choose a photo first.");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    if (photoSize) form.append("size", photoSize);
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      await created(await apiFetch<Wishlist>("/api/wishlists/image", { method: "POST", body: form }));
      if (fileRef.current) fileRef.current.value = "";
      setPhotoSize("");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function show(w: Wishlist) {
    if (open?.id === w.id) {
      setOpen(null);
      return;
    }
    try {
      setOpen(await apiFetch<Wishlist>(`/api/wishlists/${w.id}`));
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function remove(id: number) {
    try {
      await apiFetch<void>(`/api/wishlists/${id}`, { method: "DELETE" });
      setConfirming(null);
      if (open?.id === id) setOpen(null);
      await load();
    } catch (e) {
      setError(errorText(e));
    }
  }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="stack" style={{ gap: 6 }}>
        <h1>Wishlists</h1>
        <p className="muted">Describe what you want. Brand, size and budget in your words become filters; the rest is matched by look. Up to 10.</p>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      {done && <Notice>{done}</Notice>}

      <div className="two">
        <form className="panel" onSubmit={addText}>
          <h2>By description</h2>
          <div className="field">
            <label htmlFor="wtext">What are you looking for?</label>
            <input id="wtext" className="input" value={text} onChange={(e) => setText(e.target.value)} maxLength={200}
              placeholder='e.g. "white sneakers size 42 under 5000"' required />
          </div>
          <div className="row" style={{ alignItems: "end" }}>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="wsize">EU size</label>
              <input id="wsize" className="input" inputMode="decimal" value={size} onChange={(e) => setSize(e.target.value)} placeholder="from text" />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="wmax">Max Rs</label>
              <input id="wmax" className="input" inputMode="numeric" value={maxPrice} onChange={(e) => setMaxPrice(e.target.value.replace(/\D/g, ""))} placeholder="from text" />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="wbrand">Brand</label>
              <input id="wbrand" className="input" value={brand} onChange={(e) => setBrand(e.target.value)} placeholder="from text" />
            </div>
          </div>
          <div><button className="btn primary" type="submit" disabled={busy}>Add wishlist</button></div>
        </form>

        <form className="panel" onSubmit={addPhoto}>
          <h2>By photo</h2>
          <p className="muted">Matches shoes that look like your photo.</p>
          <div className="field">
            <label htmlFor="wphoto">Photo (JPEG, PNG or WebP, up to 5 MB)</label>
            <input id="wphoto" ref={fileRef} className="input" type="file" accept="image/jpeg,image/png,image/webp" />
          </div>
          <div className="field">
            <label htmlFor="wpsize">EU size (optional)</label>
            <input id="wpsize" className="input" inputMode="decimal" value={photoSize} onChange={(e) => setPhotoSize(e.target.value)} placeholder="any" />
          </div>
          <div><button className="btn primary" type="submit" disabled={busy}>Add photo wishlist</button></div>
        </form>
      </div>

      <div className="panel">
        <h2>Your wishlists</h2>
        {items === null ? (
          <p className="muted">Loading…</p>
        ) : items.length === 0 ? (
          <p className="muted">None yet. Add one above.</p>
        ) : (
          <div>
            {items.map((w) => (
              <div key={w.id}>
                <div className="wl">
                  <div className="stack" style={{ gap: 2 }}>
                    <strong>{w.text ?? "Photo wishlist"}</strong>
                    <span className="muted" style={{ fontSize: "0.9rem" }}>{describe(w)} · {w.matches} match{w.matches === 1 ? "" : "es"}</span>
                  </div>
                  <div className="row">
                    <button className="btn" type="button" onClick={() => show(w)} aria-expanded={open?.id === w.id}>
                      {open?.id === w.id ? "Hide matches" : "Matches"}
                    </button>
                    {confirming === w.id ? (
                      <>
                        <button className="btn danger" type="button" onClick={() => remove(w.id)}>Remove</button>
                        <button className="btn ghost" type="button" onClick={() => setConfirming(null)}>Keep</button>
                      </>
                    ) : (
                      <button className="btn ghost" type="button" onClick={() => setConfirming(w.id)}>Remove…</button>
                    )}
                  </div>
                </div>
                {open?.id === w.id && (
                  <div style={{ paddingBottom: 16 }}>
                    {open.results && open.results.length ? (
                      <div className="grid">{open.results.map((l) => <ListingCard key={l.id} l={l} />)}</div>
                    ) : (
                      <p className="muted">No matches yet.</p>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

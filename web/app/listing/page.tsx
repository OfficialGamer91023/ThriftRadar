"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import Notice from "@/components/Notice";
import SourceBadge from "@/components/SourceBadge";
import { apiFetch, errorText } from "@/lib/api";
import { day, price, sizeText, title } from "@/lib/format";
import type { ListingDetail, Source } from "@/lib/types";

const KIND_LABEL = { origin: "First posted", phash: "Posted again (same photos)", embedding: "Posted again (re-shot)" };

function Detail() {
  const id = useSearchParams().get("id");
  const [l, setL] = useState<ListingDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState(0);

  useEffect(() => {
    if (!id || !/^\d+$/.test(id)) {
      setError("No listing selected.");
      return;
    }
    apiFetch<ListingDetail>(`/api/listings/${id}`)
      .then(setL)
      .catch((e) => setError(errorText(e)));
  }, [id]);

  if (error) return <Notice kind="error">{error} <a href="/">Back to the feed</a></Notice>;
  if (!l) return <p className="muted">Loading…</p>;

  const photos = l.images.length ? l.images : l.cover ? [l.cover] : [];
  const p = price(l);
  const rows: { k: string; v: string | null; src?: Source }[] = [
    { k: "Brand", v: l.brand, src: l.sources.brand },
    { k: "Model", v: l.model, src: l.sources.model },
    { k: "Size", v: l.size_label ? sizeText(l) : null, src: l.sources.size },
    { k: "Price", v: p, src: l.sources.price },
    { k: "Colour", v: l.colour, src: l.sources.colour },
    { k: "Condition", v: l.condition?.replace("_", " ") ?? null, src: l.sources.condition },
    { k: "For", v: l.gender, src: l.sources.gender },
  ];

  return (
    <div className="stack" style={{ gap: 20 }}>
      <a href="/" className="muted" style={{ textDecoration: "none" }}>← Feed</a>
      <div className="detail">
        <div className="gallery">
          <div className="main">
            {photos[shown] ? <img src={photos[shown]} alt={`${title(l)}, photo ${shown + 1} of ${photos.length}`} /> : null}
            <span className={`sticker${p ? "" : " unknown"}`} style={{ fontSize: "1.3rem" }}>{p ?? "Price?"}</span>
          </div>
          {photos.length > 1 && (
            <div className="thumbs" role="group" aria-label="Photos">
              {photos.map((src, i) => (
                <button key={src} type="button" aria-pressed={i === shown} aria-label={`Photo ${i + 1}`} onClick={() => setShown(i)}>
                  <img src={src} alt="" loading="lazy" />
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="stack" style={{ gap: 18 }}>
          <div className="stack" style={{ gap: 6 }}>
            <h1>{title(l)}</h1>
            <p className="muted">
              First seen {day(l.first_seen_at)}
              {l.repost_count > 0 ? ` · posted ${l.repost_count + 1} times, last on ${day(l.last_seen_at)}` : ""}
            </p>
          </div>

          <div className="attrs" aria-label="Details">
            {rows.map((r) => (
              <div className="attr" key={r.k}>
                <span className="k">{r.k}</span>
                <span className={r.v ? "" : "muted"}>{r.v ?? "unknown"}</span>
                {r.v ? <SourceBadge source={r.src} /> : <span />}
              </div>
            ))}
          </div>
          {l.extraction === "vlm_failed" && (
            <Notice kind="warn">The AI couldn't read this post, so only the details found on this Mac are shown.</Notice>
          )}

          <div className="panel">
            <h2>History</h2>
            <ul className="timeline">
              {l.sightings.map((s, i) => (
                <li key={i}>
                  <span className="mono">{day(s.seen_at)}</span>
                  <span>{KIND_LABEL[s.kind]}</span>
                  <span className="mono">{s.price_amount != null ? `Rs ${s.price_amount.toLocaleString("en-US")}` : ""}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ListingPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <Detail />
    </Suspense>
  );
}

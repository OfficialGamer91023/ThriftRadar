"use client";

// Simulate a WhatsApp post (demo, DESIGN §4.9): upload 1–4 photos and a caption, then poll /api/posts/{id} every
// 2 s for up to 120 s. The upload is visible only to this session and deleted after 24 hours.

import { useEffect, useRef, useState } from "react";
import ListingCard from "@/components/ListingCard";
import Notice from "@/components/Notice";
import { apiFetch, errorText } from "@/lib/api";
import { useConfig } from "@/lib/config";
import type { Listing } from "@/lib/types";

const MAX_PHOTOS = 4;
const MAX_BYTES = 5 * 1024 * 1024;
const POLL_MS = 2000;
const POLL_FOR_MS = 120_000;

interface PostStatus {
  id: number;
  status: "received" | "processing" | "awaiting_vlm" | "done" | "failed";
  outcome: "listed" | "repost" | "no_shoe" | "mixed" | null;
  listings: Listing[];
}

interface Pick {
  file: File;
  url: string;
}

const STEPS = [
  { key: "received", label: "Uploaded" },
  { key: "processing", label: "On-device models: find the shoe, read the caption and size tag" },
  { key: "awaiting_vlm", label: "AI fills in what's still missing (only if needed)" },
  { key: "done", label: "Listed" },
] as const;

function stepState(i: number, status: PostStatus["status"] | null): "done" | "now" | "todo" {
  if (!status) return "todo";
  if (status === "done") return "done";
  if (status === "failed") return i < 3 ? "done" : "todo";
  const at = STEPS.findIndex((s) => s.key === status);
  return i < at ? "done" : i === at ? "now" : "todo";
}

function outcomeText(p: PostStatus): string | null {
  if (p.status === "failed") return "Processing failed for this post. Try different photos.";
  if (p.outcome === "no_shoe") return "No shoe was found in these photos, so nothing was listed.";
  if (p.outcome === "repost") return "These photos match one of your earlier uploads, so that listing was updated instead.";
  return null;
}

export default function SimulatePage() {
  const config = useConfig();
  const [picks, setPicks] = useState<Pick[]>([]);
  const [caption, setCaption] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [post, setPost] = useState<PostStatus | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const uploadId = useRef<string>(crypto.randomUUID()); // same id for a retried submit: the server dedupes
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => () => picks.forEach((p) => URL.revokeObjectURL(p.url)), [picks]);

  function add(files: FileList | null) {
    if (!files) return;
    setError(null);
    const next = [...picks];
    for (const f of Array.from(files)) {
      if (next.length >= MAX_PHOTOS) {
        setError(`Up to ${MAX_PHOTOS} photos per post.`);
        break;
      }
      if (f.size > MAX_BYTES) {
        setError(`${f.name} is over 5 MB.`);
        continue;
      }
      next.push({ file: f, url: URL.createObjectURL(f) });
    }
    setPicks(next);
    uploadId.current = crypto.randomUUID();
    if (fileRef.current) fileRef.current.value = "";
  }

  function remove(i: number) {
    setPicks(picks.filter((_, n) => n !== i));
    uploadId.current = crypto.randomUUID();
  }

  async function poll(id: number) {
    const until = Date.now() + POLL_FOR_MS;
    while (Date.now() < until) {
      try {
        const p = await apiFetch<PostStatus>(`/api/posts/${id}`);
        setPost(p);
        if (p.status === "done" || p.status === "failed") return;
      } catch (e) {
        setError(errorText(e));
        return;
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
    setTimedOut(true);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!picks.length) return;
    const form = new FormData();
    picks.forEach((p) => form.append("files", p.file));
    form.append("caption", caption);
    setBusy(true);
    setError(null);
    setPost(null);
    setTimedOut(false);
    try {
      const r = await apiFetch<{ post_id: number }>("/api/demo/posts", {
        method: "POST",
        body: form,
        headers: { "X-Upload-Id": uploadId.current },
      });
      await poll(r.post_id);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  function again() {
    setPicks([]);
    setCaption("");
    setPost(null);
    setTimedOut(false);
    uploadId.current = crypto.randomUUID();
  }

  if (config && !config.demo) {
    return (
      <div className="stack" style={{ gap: 12, maxWidth: 520 }}>
        <h1>Simulate is demo-only</h1>
        <p>Here, posts come from your WhatsApp group (or the chat-export importer).</p>
      </div>
    );
  }

  const finished = post && (post.status === "done" || post.status === "failed");
  const ai = post?.listings.some((l) => l.extraction === "vlm");
  return (
    <div className="stack" style={{ gap: 18, maxWidth: 760 }}>
      <div className="stack" style={{ gap: 6 }}>
        <h1>Simulate a post</h1>
        <p className="muted">
          Post a shoe the way a seller would in the group: a few photos and a caption like
          &ldquo;Nike Air Force 1, size 42, Rs 4500&rdquo;. Only you can see it, and it&rsquo;s deleted after 24 hours.
        </p>
      </div>

      {!post && (
        <form className="panel" onSubmit={submit}>
          <div className="field">
            <label htmlFor="photos">Photos ({picks.length} of {MAX_PHOTOS})</label>
            {picks.length > 0 && (
              <div className="picks">
                {picks.map((p, i) => (
                  <figure key={p.url}>
                    <img src={p.url} alt={`Photo ${i + 1}`} />
                    <button type="button" className="btn" onClick={() => remove(i)} aria-label={`Remove photo ${i + 1}`}>
                      ×
                    </button>
                  </figure>
                ))}
              </div>
            )}
            <div className="row">
              <button type="button" className="btn" disabled={busy || picks.length >= MAX_PHOTOS}
                onClick={() => fileRef.current?.click()}>
                Add photos
              </button>
              <span className="muted" style={{ fontSize: "0.85rem" }}>JPEG, PNG or WebP, up to 5 MB each</span>
            </div>
            <input ref={fileRef} id="photos" type="file" multiple accept="image/jpeg,image/png,image/webp" className="sr"
              onChange={(e) => add(e.target.files)} />
          </div>
          <div className="field">
            <label htmlFor="caption">Caption</label>
            <textarea id="caption" className="input" rows={3} maxLength={500} value={caption}
              onChange={(e) => setCaption(e.target.value)} placeholder="e.g. Adidas Samba, EU 43, 9/10 condition, Rs 6000" />
          </div>
          <button className="btn primary" type="submit" disabled={busy || picks.length === 0}>
            {busy ? "Uploading…" : "Post it"}
          </button>
        </form>
      )}

      {error && <Notice kind="error">{error}</Notice>}

      {post && (
        <div className="panel" aria-live="polite">
          <h2>Post #{post.id}</h2>
          <ol className="steps">
            {STEPS.map((s, i) => (
              <li key={s.key} data-state={stepState(i, post.status)}>{s.label}</li>
            ))}
          </ol>
          {timedOut && !finished && (
            <Notice kind="warn">This is taking longer than usual. It will finish in the background; check the feed in a minute.</Notice>
          )}
          {finished && outcomeText(post) && <Notice kind="warn">{outcomeText(post)}</Notice>}
          {finished && post.status === "done" && post.listings.length > 0 && !ai && (
            <p className="muted" style={{ fontSize: "0.9rem" }}>
              Everything here came from the caption and the on-device models. If the AI was needed but today&rsquo;s
              AI budget is used up, some fields may be missing.
            </p>
          )}
        </div>
      )}

      {post && post.listings.length > 0 && (
        <div className="grid">
          {post.listings.map((l) => <ListingCard key={l.id} l={l} />)}
        </div>
      )}

      {finished && (
        <div className="row">
          <button type="button" className="btn primary" onClick={again}>Post another</button>
          <a className="btn" href="/">Back to the feed</a>
        </div>
      )}
    </div>
  );
}

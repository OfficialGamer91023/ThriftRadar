"use client";

// Photo credits for the demo's sample listings (DESIGN §4.9), from backend/seed/ATTRIBUTION.csv via /api/credits.

import { useEffect, useState } from "react";
import Notice from "@/components/Notice";
import { apiFetch, errorText } from "@/lib/api";

interface Credit {
  file: string;
  author: string;
  license: string;
  license_url: string;
  source_url: string;
  notes: string;
}

export default function CreditsPage() {
  const [rows, setRows] = useState<Credit[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<{ results: Credit[] }>("/api/credits")
      .then((r) => setRows(r.results))
      .catch((e) => setError(errorText(e)));
  }, []);

  return (
    <div className="stack" style={{ gap: 16, maxWidth: 820 }}>
      <div className="stack" style={{ gap: 6 }}>
        <h1>Photo credits</h1>
        <p className="muted">
          The demo&rsquo;s sample listings use these photos under their Creative Commons licenses. They were resized
          and their metadata removed. The sizes, prices and sellers in the listings are made up for the demo; the
          photographers aren&rsquo;t selling anything.
        </p>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      {rows && rows.length === 0 && <p className="muted">No sample photos here.</p>}
      {rows && rows.length > 0 && (
        <ol className="credits">
          {rows.map((r) => (
            <li key={r.file}>
              <a href={r.source_url} rel="noopener noreferrer nofollow" target="_blank">{r.notes.split(" via ")[0] || r.file}</a>
              {" "}by {r.author}, <a href={r.license_url} rel="noopener noreferrer" target="_blank">{r.license}</a>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

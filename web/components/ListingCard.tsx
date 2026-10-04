import { ago, price, sizeText, title } from "@/lib/format";
import type { Listing } from "@/lib/types";

export default function ListingCard({ l }: { l: Listing }) {
  const p = price(l);
  return (
    <a className="card" href={`/listing/?id=${l.id}`}>
      <div className="photo">
        {l.cover ? <img src={l.cover} alt={title(l)} loading="lazy" /> : <span className="none">no photo</span>}
        <span className={`sticker${p ? "" : " unknown"}`}>{p ?? "Price?"}</span>
      </div>
      <div className="body">
        <span className="name" title={title(l)}>{title(l)}</span>
        <span className={`size${l.size_label ? "" : " muted"}`}>{sizeText(l)}</span>
        <span className="meta">
          <span>{ago(l.last_seen_at)}</span>
          {l.repost_count > 0 && <span className="badge repost">posted {l.repost_count + 1}×</span>}
          {l.colour && <span>{l.colour}</span>}
        </span>
      </div>
    </a>
  );
}

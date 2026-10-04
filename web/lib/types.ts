// Shapes returned by the backend (backend/app/api/listings_view.py and friends).

export type Source = "caption" | "ocr" | "vlm" | "siglip" | "knn" | "seed";

export interface Listing {
  id: number;
  post_id: number;
  item_idx: number;
  brand: string | null;
  model: string | null;
  colour: string | null;
  condition: string | null;
  gender: string | null;
  size_label: string | null;
  size_eu: number | null;
  size_approx: boolean;
  price_amount: number | null;
  currency: string | null;
  price_on_request: boolean;
  extraction: string;
  status: string;
  repost_count: number;
  source: string;
  sources: Partial<Record<"brand" | "size" | "price" | "model" | "colour" | "condition" | "gender", Source>>;
  first_seen_at: string;
  last_seen_at: string;
  cover: string | null;
  score?: number;
}

export interface ListingDetail extends Listing {
  images: string[];
  sightings: { seen_at: string; price_amount: number | null; kind: "origin" | "phash" | "embedding" }[];
}

export interface Page<T> {
  results: T[];
  next?: string | null;
}

export interface SearchResult extends Page<Listing> {
  query?: string;
  filters?: { brand: string | null; max_price: number | null; size_eu: number | null };
  order?: "similar" | "newest";
}

export interface Wishlist {
  id: number;
  text: string | null;
  image: boolean;
  brand: string | null;
  max_price: number | null;
  size_eu: number | null;
  min_score: number;
  active: boolean;
  created_at: string;
  matches: number;
  results?: Listing[];
}

export interface Status {
  queue: Record<string, number>;
  models_ready: boolean;
  vlm: {
    provider: string;
    model: string;
    calls_today: number;
    daily_cap: number;
    est_cost_today_usd: number;
    paused: boolean;
  };
  wishlists: number;
}

export interface Config {
  demo: boolean;
  features: { whatsapp: boolean };
}

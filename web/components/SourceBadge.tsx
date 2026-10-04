import { SOURCE_LABEL } from "@/lib/format";
import type { Source } from "@/lib/types";

export default function SourceBadge({ source }: { source?: Source }) {
  if (!source) return null;
  const ai = source === "vlm" || source === "siglip" || source === "knn";
  return <span className={`badge${ai ? " ai" : ""}`}>{SOURCE_LABEL[source] ?? source}</span>;
}

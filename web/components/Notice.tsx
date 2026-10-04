export default function Notice({ kind = "info", children }: { kind?: "info" | "warn" | "error"; children: React.ReactNode }) {
  return (
    <div className={`notice${kind === "info" ? "" : ` ${kind}`}`} role={kind === "error" ? "alert" : "status"}>
      {children}
    </div>
  );
}

import type { Provenance } from "../api/generated";

export function Trust({ provenance, stale }: { provenance?: Provenance[]; stale?: string[] }) {
  if (!provenance) return <p className="unknown">Unknown: provenance was omitted by the API.</p>;
  if (!provenance.length) return <p className="unknown">Unknown: no provenance was returned.</p>;
  return <section aria-label="provenance"><h3>Provenance</h3><ul>{provenance.map((item) => <li key={item.source}><code>{item.source}</code> — {item.as_of ?? "unknown time"}{item.stale || stale?.includes(item.source) ? " · stale" : ""}{item.trust ? ` · ${item.trust}` : ""}</li>)}</ul></section>;
}

import type { Document } from "../api/generated";
import { Trust } from "./Trust";

export function DocumentView({ document }: { document: Document }) {
  return <article><h2>{document.command}</h2><Trust provenance={document.provenance} stale={document.stale}/><pre>{JSON.stringify(document.result, null, 2)}</pre></article>;
}

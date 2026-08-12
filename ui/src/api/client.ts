import type { ApiError, ApiRoute, Document } from "./generated";
import { authorization } from "../auth/session";

export class ApiClient {
  readonly origin: string;
  constructor(origin = (globalThis as { __CADASTRE_CONFIG__?: { apiOrigin?: string } }).__CADASTRE_CONFIG__?.apiOrigin ?? "") {
    this.origin = origin.replace(/\/$/, "");
    if (!this.origin) throw new Error("API origin is not configured");
  }

  async get(path: ApiRoute, query: Record<string, string | undefined> = {}): Promise<Document> {
    const url = new URL(`${this.origin}${path}`, globalThis.location?.origin ?? "http://localhost");
    Object.entries(query).forEach(([key, value]) => value !== undefined && url.searchParams.set(key, value));
    const response = await fetch(url, { headers: { Accept: "application/json", ...this.headers() } });
    return this.decode(response);
  }

  async post(path: ApiRoute, body: unknown): Promise<Document> {
    const response = await fetch(`${this.origin}${path}`, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json", ...this.headers() }, body: JSON.stringify(body) });
    return this.decode(response);
  }

  brief() { return this.get("/brief"); }
  contextFor(intent: string) { return this.get("/context-for", { intent }); }
  question(id: string) { return this.get("/question", { id }); }
  lookup(id: string) { return this.get(`/lookup/${encodeURIComponent(id)}` as ApiRoute); }
  drift() { return this.get("/drift"); }
  stale() { return this.get("/stale"); }
  sources() { return this.get("/sources"); }
  // Summary by default: retained evidence can be large, and a view that dumps
  // an inventory into the page is the same mistake as one that dumps it into a
  // model's context.
  observations(source?: string, summaryOnly = true) { return this.get("/observations", { source, summary_only: summaryOnly ? "true" : undefined }); }
  check(artifact: string) { return this.post("/check", { artifact, kind: "compose", path: "proposal.yaml" }); }
  annotate(kind: string, id: string, notes: string) {
    return this.post("/annotate", { kind, id, record: { notes }, reason: "GUI reviewed annotation" });
  }

  // The GUI shows no ranking or join logic of its own (MANIFEST.md R09); it
  // only renders whatever `/manifest/*` already returned, same as every
  // other view.
  manifestBrief() { return this.get("/manifest/brief"); }
  manifestProjects() { return this.get("/manifest/projects"); }
  manifestBacklog() { return this.get("/manifest/backlog"); }
  manifestNext() { return this.get("/manifest/next"); }
  manifestDrift() { return this.get("/manifest/drift"); }
  manifestRepo(repo: string) { return this.get(`/manifest/repo/${encodeURIComponent(repo)}` as ApiRoute); }
  manifestWhy(id: string) { return this.get(`/manifest/why/${encodeURIComponent(id)}` as ApiRoute); }

  // Not a Document: a flat capability payload the GUI uses to decide whether
  // to show Manifest navigation at all, never to gate what it renders once
  // shown — the server's own route/CLI checks remain the enforcement.
  async capabilities(): Promise<{ modules?: Record<string, boolean> }> {
    const response = await fetch(`${this.origin}/health/ready`, { headers: { Accept: "application/json", ...this.headers() } });
    if (!response.ok) return {};
    return response.json() as Promise<{ modules?: Record<string, boolean> }>;
  }

  private headers(): Record<string, string> {
    const value = authorization();
    return value ? { Authorization: value } : {};
  }

  private async decode(response: Response): Promise<Document> {
    const value = await response.json() as Document | ApiError;
    if (!response.ok || "error" in value) throw new Error((value as ApiError).error?.message ?? `API request failed (${response.status})`);
    return value as Document;
  }
}

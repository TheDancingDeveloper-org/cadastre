import { useEffect, useState } from "react";
import { ApiClient } from "./api/client";
import type { Document } from "./api/generated";
import { setSession } from "./auth/session";
import { DocumentView } from "./components/DocumentView";

const api = new ApiClient();
const baseViews = ["brief", "context", "question", "entity", "drift", "stale", "sources", "observations", "check", "write-review"] as const;
const manifestViews = ["manifest-brief", "manifest-projects", "manifest-backlog", "manifest-next", "manifest-drift", "manifest-repo", "manifest-why"] as const;
type View = typeof baseViews[number] | typeof manifestViews[number];

export function App() {
  const [manifestEnabled, setManifestEnabled] = useState(false);
  const [view, setView] = useState<View>("brief");
  const [document, setDocument] = useState<Document>();
  const [error, setError] = useState<string>();
  const [token, setToken] = useState("");
  const [entityId, setEntityId] = useState("");
  const [artifact, setArtifact] = useState("services: {}\n");
  const [writeTarget, setWriteTarget] = useState("");
  const [writeNotes, setWriteNotes] = useState("");
  const [questionId, setQuestionId] = useState("");
  const [manifestId, setManifestId] = useState("");
  const [manifestRepo, setManifestRepo] = useState("");
  useEffect(() => { void api.capabilities().then((response) => setManifestEnabled(response.modules?.manifest === true)); }, []);
  const views: readonly View[] = manifestEnabled ? [...baseViews, ...manifestViews] : baseViews;
  const load = async (next: View) => {
    setView(next); setError(undefined);
    try {
      if (next === "check") {
        setDocument(await api.check(artifact));
      } else if (next === "entity") {
        if (!entityId.trim()) throw new Error("Enter an entity id before lookup");
        setDocument(await api.lookup(entityId.trim()));
      } else if (next === "write-review") {
        if (!writeTarget.trim()) throw new Error("Enter a KIND:ID target before annotating");
        const separator = writeTarget.indexOf(":");
        if (separator < 1) throw new Error("Write target must be KIND:ID");
        setDocument(await api.annotate(writeTarget.slice(0, separator), writeTarget.slice(separator + 1), writeNotes));
      } else if (next === "question") {
        if (!questionId.trim()) throw new Error("Enter a question id before asking");
        setDocument(await api.question(questionId.trim()));
      } else if (next === "manifest-why") {
        if (!manifestId.trim()) throw new Error("Enter a work item id before asking why");
        setDocument(await api.manifestWhy(manifestId.trim()));
      } else if (next === "manifest-repo") {
        if (!manifestRepo.trim()) throw new Error("Enter a repository before looking it up");
        setDocument(await api.manifestRepo(manifestRepo.trim()));
      } else if (next === "manifest-brief") {
        setDocument(await api.manifestBrief());
      } else if (next === "manifest-projects") {
        setDocument(await api.manifestProjects());
      } else if (next === "manifest-backlog") {
        setDocument(await api.manifestBacklog());
      } else if (next === "manifest-next") {
        setDocument(await api.manifestNext());
      } else if (next === "manifest-drift") {
        setDocument(await api.manifestDrift());
      } else {
        setDocument(await (next === "brief" ? api.brief() : next === "context" ? api.contextFor("") : next === "drift" ? api.drift() : next === "stale" ? api.stale() : next === "observations" ? api.observations() : api.sources()));
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Request failed"); }
  };
  return <main><header><h1>Cadastre</h1><p>Catalog client · version 0.2.0</p></header>
    <nav aria-label="views">{views.map((item) => <button key={item} onClick={() => void load(item)} aria-current={view === item ? "page" : undefined}>{item}</button>)}</nav>
    <form onSubmit={(event) => { event.preventDefault(); void load("entity"); }}><label>Entity id <input value={entityId} onChange={(event) => setEntityId(event.target.value)} /></label><button type="submit">Lookup</button></form>
    <form onSubmit={(event) => { event.preventDefault(); void load("check"); }}><label>Artifact check <textarea value={artifact} onChange={(event) => setArtifact(event.target.value)} rows={4} /></label><button type="submit">Check artifact</button></form>
    <form onSubmit={(event) => { event.preventDefault(); void load("question"); }}><label>Question id <input value={questionId} onChange={(event) => setQuestionId(event.target.value)} /></label><button type="submit">Ask question</button></form>
    <form onSubmit={(event) => { event.preventDefault(); setSession(token); void load(view); }}><label>Local development token <input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" /></label><button type="submit">Use in memory</button></form>
    {view === "write-review" && <form onSubmit={(event) => { event.preventDefault(); void load("write-review"); }}><h2>Write review</h2><label>Target KIND:ID <input value={writeTarget} onChange={(event) => setWriteTarget(event.target.value)} /></label><label>Annotation notes <textarea value={writeNotes} onChange={(event) => setWriteNotes(event.target.value)} /></label><button type="submit">Submit authorized annotation</button></form>}
    {manifestEnabled && view === "manifest-why" && <form onSubmit={(event) => { event.preventDefault(); void load("manifest-why"); }}><label>Work item id <input value={manifestId} onChange={(event) => setManifestId(event.target.value)} /></label><button type="submit">Explain score</button></form>}
    {manifestEnabled && view === "manifest-repo" && <form onSubmit={(event) => { event.preventDefault(); void load("manifest-repo"); }}><label>Repository <input value={manifestRepo} onChange={(event) => setManifestRepo(event.target.value)} /></label><button type="submit">Show repository</button></form>}
    {error && <p role="alert">{error}</p>}{document ? <DocumentView document={document}/> : <p>Select a view to query the API. Unknown data is shown explicitly.</p>}</main>;
}

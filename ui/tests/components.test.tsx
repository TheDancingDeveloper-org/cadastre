import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Trust } from "../src/components/Trust";
import { DocumentView } from "../src/components/DocumentView";

describe("trust and document states", () => {
  it("renders omitted provenance as unknown", () => {
    expect(renderToStaticMarkup(<Trust />)).toContain("provenance was omitted");
  });

  it("renders stale and contested trust text", () => {
    const markup = renderToStaticMarkup(<Trust provenance={[{ source: "dns", as_of: null, trust: "contested" }]} stale={["dns"]} />);
    expect(markup).toContain("stale");
    expect(markup).toContain("contested");
  });

  it("does not turn an unknown document into an empty state", () => {
    expect(renderToStaticMarkup(<DocumentView document={{ command: "brief", result: {}, provenance: [] }} />)).toContain("Unknown");
  });
});

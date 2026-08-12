import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../src/api/client";

describe("generated API client contract", () => {
  afterEach(() => vi.restoreAllMocks());

  it("calls typed operations against the configured API origin", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response(JSON.stringify({ command: "cadastre brief", result: {} }), { status: 200 }),
    );
    const client = new ApiClient("http://api.example.test");
    await client.brief();
    await client.contextFor("deploy");
    await client.lookup("host-01");
    await client.check("services: {}\n");
    expect(fetchMock.mock.calls.map(([request]) => String(request))).toEqual([
      "http://api.example.test/brief",
      "http://api.example.test/context-for?intent=deploy",
      "http://api.example.test/lookup/host-01",
      "http://api.example.test/check",
    ]);
  });

  it("surfaces canonical structured API errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { kind: "PermissionError", message: "denied" } }), { status: 403 }),
    );
    await expect(new ApiClient("http://api.example.test").brief()).rejects.toThrow("denied");
  });

  it("calls the manifest routes only, never joining or ranking client-side", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response(JSON.stringify({ command: "cadastre manifest brief", result: {} }), { status: 200 }),
    );
    const client = new ApiClient("http://api.example.test");
    await client.manifestBrief();
    await client.manifestProjects();
    await client.manifestBacklog();
    await client.manifestNext();
    await client.manifestDrift();
    await client.manifestRepo("org/repo");
    await client.manifestWhy("w1");
    expect(fetchMock.mock.calls.map(([request]) => String(request))).toEqual([
      "http://api.example.test/manifest/brief",
      "http://api.example.test/manifest/projects",
      "http://api.example.test/manifest/backlog",
      "http://api.example.test/manifest/next",
      "http://api.example.test/manifest/drift",
      "http://api.example.test/manifest/repo/org%2Frepo",
      "http://api.example.test/manifest/why/w1",
    ]);
  });

  it("reads module capability flags from health/ready without throwing when unreachable", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", modules: { manifest: true } }), { status: 200 }),
    );
    const enabled = await new ApiClient("http://api.example.test").capabilities();
    expect(enabled.modules?.manifest).toBe(true);

    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 503 }));
    const unavailable = await new ApiClient("http://api.example.test").capabilities();
    expect(unavailable).toEqual({});
  });
});

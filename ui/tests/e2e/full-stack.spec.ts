import { expect, test, type APIRequestContext } from "@playwright/test";

type Document = {
  command: string;
  result: Record<string, unknown>;
  provenance?: unknown[];
  stale?: string[];
};

const environment = (
  globalThis as typeof globalThis & {
    process?: { env?: Record<string, string | undefined> };
  }
).process?.env;
const apiOrigin = environment?.CADASTRE_E2E_API_ORIGIN ?? "http://127.0.0.1:18080";
const mcpOrigin = environment?.CADASTRE_E2E_MCP_ORIGIN ?? "http://127.0.0.1:18081/mcp";

async function mcpCall(
  request: APIRequestContext,
  name: string,
  args: Record<string, unknown> = {},
) {
  const initialize = await request.post(mcpOrigin, {
    data: { jsonrpc: "2.0", id: 1, method: "initialize", params: {} },
  });
  expect(initialize.ok()).toBeTruthy();
  const session = initialize.headers()["mcp-session-id"];
  expect(session).toBeTruthy();
  const response = await request.post(mcpOrigin, {
    headers: { "Mcp-Session-Id": session },
    data: {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: { name, arguments: args },
    },
  });
  expect(response.ok()).toBeTruthy();
  return response.json();
}

test.describe("synthetic full-stack environment", () => {
  test("exposes healthy HTTP and ordinary API surfaces", async ({ request }) => {
    const readyResponse = await request.get(`${apiOrigin}/health/ready`);
    expect(readyResponse.ok()).toBeTruthy();
    const ready = await readyResponse.json();
    expect(ready.lifecycle.state).toBe("ready");
    expect(ready.lifecycle.checks.runtime_store).toBe("sqlite");

    const openApiResponse = await request.get(`${apiOrigin}/openapi.json`);
    expect(openApiResponse.ok()).toBeTruthy();
    const openApi = await openApiResponse.json();
    expect(openApi.openapi).toBe("3.1.0");
    expect(openApi.paths).toHaveProperty("/brief");
    expect(openApi.paths).toHaveProperty("/check");

    const checkResponse = await request.post(`${apiOrigin}/check`, {
      data: {
        artifact: "services: {}\n",
        kind: "compose",
        path: "e2e-compose.yaml",
      },
    });
    expect(checkResponse.ok()).toBeTruthy();
    const checked = await checkResponse.json() as Document;
    expect(checked.command).toBe("cadastre check e2e-compose.yaml");
    expect(checked.result.artifact).toMatchObject({ path: "e2e-compose.yaml" });
  });

  test("shares the same fake catalog through HTTP/API and MCP", async ({ request }) => {
    const httpResponse = await request.get(`${apiOrigin}/brief`);
    expect(httpResponse.ok()).toBeTruthy();
    const httpDocument = await httpResponse.json() as Document;
    expect(httpDocument.command).toBe("cadastre brief");
    expect(httpDocument.provenance?.length).toBeGreaterThan(0);
    expect(httpDocument.result.counts).toMatchObject({ host: 7, service: 6 });
    expect(httpDocument.result.hosts).toEqual(
      expect.arrayContaining([expect.objectContaining({ id: "app-01" })]),
    );

    const mcpResponse = await mcpCall(request, "brief");
    const mcpDocument = mcpResponse.result.structuredContent as Document;
    expect(mcpDocument.command).toBe(httpDocument.command);
    expect(mcpDocument.result).toEqual(httpDocument.result);
    expect(mcpDocument.provenance).toEqual(httpDocument.provenance);
    expect(mcpDocument.stale).toEqual(httpDocument.stale);
  });

  test("renders the live HTTP document in the GUI", async ({ page }) => {
    await page.route("**/runtime-config.js", (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: `globalThis.__CADASTRE_CONFIG__={apiOrigin:${JSON.stringify(apiOrigin)}};`,
      }),
    );
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Cadastre" })).toBeVisible();
    await page.getByRole("button", { name: "brief" }).click();
    await expect(page.getByRole("heading", { name: "cadastre brief" })).toBeVisible();
    await expect(page.getByRole("region", { name: "provenance" })).toContainText("declared");
    await expect(page.locator("pre")).toContainText("app-01");
    await expect(page.locator("body")).not.toContainText("SQLite");
  });
});

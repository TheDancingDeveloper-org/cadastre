import { expect, test } from "@playwright/test";

test("GUI exposes API-only views and explicit unknown provenance", async ({ page }) => {
  await page.route("**/runtime-config.js", (route) => route.fulfill({ contentType: "application/javascript", body: "globalThis.__CADASTRE_CONFIG__={apiOrigin:'http://127.0.0.1:8000'};" }));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Cadastre" })).toBeVisible();
  await page.getByRole("button", { name: "write-review" }).click();
  await expect(page.getByText("Submit authorized annotation")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("SQLite");
});

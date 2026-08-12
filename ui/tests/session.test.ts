import { afterEach, describe, expect, it } from "vitest";
import { authorization, clearSession, setSession } from "../src/auth/session";

describe("browser session", () => {
  afterEach(clearSession);

  it("holds a development token in memory only", () => {
    setSession("operator-token");
    expect(authorization()).toBe("Bearer operator-token");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("clears the token", () => {
    setSession("operator-token");
    clearSession();
    expect(authorization()).toBeUndefined();
  });
});

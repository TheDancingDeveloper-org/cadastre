let token: string | undefined;

export function setSession(value: string): void { token = value.trim() || undefined; }
export function clearSession(): void { token = undefined; }
export function authorization(): string | undefined {
  return token ? `Bearer ${token}` : undefined;
}

export function brokerDisplayName(provider?: string | null): string {
  return provider?.toLowerCase() === "flattrade" ? "Flattrade" : "Upstox";
}

export function brokerSettingsLabel(provider?: string | null): string {
  return `Settings → Broker (${brokerDisplayName(provider)})`;
}

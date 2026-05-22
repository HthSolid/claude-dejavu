// Synthetic fixture for the recovery primitive's golden test.

export function start(): string {
  return "starting";
}

export function stop(): string {
  return "stopping";
}

export function status(): { ready: boolean } {
  return { ready: false };
}

export const VERSION = "v5";

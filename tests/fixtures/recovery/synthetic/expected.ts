// Synthetic fixture for the recovery primitive's golden test.

export function start(): string {
  return "started";
}

export function stop(): string {
  return "stopped";
}

export function status(): { ready: boolean } {
  return { ready: true };
}

export const VERSION = "v5";

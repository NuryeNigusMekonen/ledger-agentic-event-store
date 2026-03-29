export function formatCount(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "0";
  }
  return Math.round(value).toLocaleString();
}

export function formatPercent(
  value: number | null | undefined,
  fractionDigits = 1
): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "n/a";
  }

  const digits = Math.max(0, Math.min(2, fractionDigits));
  return `${value.toFixed(digits)}%`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return "n/a";
  }

  if (seconds < 60) {
    return `${trimTrailingZeros(seconds.toFixed(seconds < 10 ? 2 : 1))} sec`;
  }

  const minutes = seconds / 60;
  const minuteDigits = minutes < 10 ? 2 : 1;
  return `${trimTrailingZeros(minutes.toFixed(minuteDigits))} min`;
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "Not available";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  });
}

function trimTrailingZeros(value: string): string {
  return value.replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
}

// 秒数配置附带可读时长，避免把 21600 这类值误读。
export function durationHint(label: string, value: unknown) {
  const seconds = Number(value);
  if (!/（秒）|\(秒\)/.test(label) || !Number.isFinite(seconds) || seconds < 120) return "";
  const units: [number, string][] = [
    [86400, "天"],
    [3600, "小时"],
    [60, "分钟"],
  ];
  for (const [size, unit] of units)
    if (seconds >= size) {
      const amount = seconds / size;
      return `约 ${Number.isInteger(amount) ? amount : amount.toFixed(1)} ${unit}`;
    }
  return "";
}

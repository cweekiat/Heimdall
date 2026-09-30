// The ONLY place colours are defined (task 014e). Five semantic tokens, dark and light variants.
//   alert    red: every alert and anything needing attention. Severity = size/opacity, never hue.
//   neutral  all normal aircraft, ships and tracks (told apart by shape, not colour).
//   military amber highlight for military-flagged traffic and the selection.
//   notice   muted outline only (navigation warnings, NOTAMs, coverage, area outlines).
//   base     UI chrome, text, halos and outlines.
export type Rgb = [number, number, number];
export type Rgba = [number, number, number, number];
export type Mode = "dark" | "light";
export type Token = "alert" | "neutral" | "military" | "notice" | "base";

export const PALETTE: Record<Token, Record<Mode, Rgb>> = {
  alert: { dark: [248, 81, 73], light: [198, 40, 40] },
  neutral: { dark: [138, 160, 180], light: [84, 104, 124] },
  military: { dark: [255, 176, 32], light: [176, 106, 0] },
  notice: { dark: [160, 170, 186], light: [96, 108, 124] },
  base: { dark: [235, 241, 247], light: [20, 32, 43] },
};
/** Chrome surface used for text halos and map backgrounds. */
export const SURFACE: Record<Mode, Rgb> = { dark: [11, 16, 22], light: [233, 238, 243] };

let mode: Mode = "dark";
export function setMode(m: Mode): void {
  mode = m;
}
export const currentMode = (): Mode => mode;

export const rgb = (t: Token, m: Mode = mode): Rgb => PALETTE[t][m];
export const rgba = (t: Token, alpha: number, m: Mode = mode): Rgba => {
  const c = PALETTE[t][m];
  return [c[0], c[1], c[2], Math.round(Math.min(1, Math.max(0, alpha)) * 255)];
};
export const css = (t: Token, alpha = 1, m: Mode = mode): string => {
  const c = PALETTE[t][m];
  return alpha >= 1 ? `rgb(${c[0]},${c[1]},${c[2]})` : `rgba(${c[0]},${c[1]},${c[2]},${alpha})`;
};
export const surfaceCss = (m: Mode = mode): string => `rgb(${SURFACE[m].join(",")})`;
export const surfaceRgba = (alpha: number, m: Mode = mode): Rgba => [...SURFACE[m], Math.round(alpha * 255)] as Rgba;

// ---- severity: size, opacity and intensity only. The colour is always the alert token.
export type Sev = "info" | "low" | "medium" | "high" | "critical";
const SEV_RANK: Record<Sev, number> = { info: 0, low: 1, medium: 2, high: 3, critical: 4 };
export interface SevStyle {
  token: "alert";
  radius: number;
  opacity: number;
}
export function severityStyle(sev: Sev): SevStyle {
  const r = SEV_RANK[sev] ?? 0;
  return { token: "alert", radius: 7 + r * 1.5, opacity: 0.45 + r * 0.14 };
}
/** Intensity ramp for a 0..1 amount (share of affected traffic): same hue, more opaque. */
export const intensity = (x: number): number => 0.18 + 0.7 * Math.min(1, Math.max(0, x));

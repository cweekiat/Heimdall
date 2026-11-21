// Icon registry: only the lucide icons we use are imported (tree-shaken). ISC licence.
import {
  Activity, Ban, Box, Compass, Bell, ChartLine, Check, CircleDashed, CircleDot, CircleHelp, Clock, Crosshair, Database, Eye, FileText, Focus,
  Gauge, Globe, Hourglass, Info, Trash2, Layers, LayoutGrid, ListChecks, ListFilter, Maximize2, Minus, Pencil, Pentagon, Plane, Plus, Radio, RotateCcw,
  Ruler, Search, Ship, ShieldAlert, StickyNote, SunMoon, TriangleAlert, Undo2, Waypoints, X, Zap, createElement,
  Satellite, Settings, KeyRound, EyeOff, ChevronRight, LocateFixed, Map as MapIcon, Earth, ChevronUp, ChevronDown, LoaderCircle, LogOut,
} from "lucide";
import type { IconNode } from "lucide";

const ICONS = {
  alerts: Bell, areas: LayoutGrid, layers: Layers, sources: Database, actions: ListChecks, timeline: ChartLine, shift: FileText,
  help: CircleHelp, close: X, minimise: Minus, maximise: Maximize2, search: Search, theme: SunMoon, zoomIn: Plus, zoomOut: Minus,
  measure: Ruler, ring: CircleDot, reset: RotateCcw, activity: Activity, ack: Check, review: Eye, confirm: ShieldAlert, dismiss: Ban,
  shelve: Hourglass, note: Pencil, warn: TriangleAlert, plane: Plane, ship: Ship, radio: Radio, gps: Crosshair, coverage: Gauge,
  clock: Clock, area: Globe, seen: Zap, follow: Focus, undo: Undo2, waypoints: Waypoints, notice: StickyNote, dashed: CircleDashed,
  about: Info, trash: Trash2, draw: Pentagon, filter: ListFilter, cube: Box, compass: Compass,
  orbit: Satellite, settings: Settings, home: LocateFixed, map: MapIcon, globe: Earth, up: ChevronUp, down: ChevronDown, spinner: LoaderCircle, key: KeyRound, eyeOff: EyeOff, right: ChevronRight, logout: LogOut,
} satisfies Record<string, IconNode>;

export type IconName = keyof typeof ICONS;

/** A decorative SVG (16 px default). Callers put the aria-label on the button, not the icon. */
export function icon(name: IconName, size = 16): SVGElement {
  return createElement(ICONS[name], { width: size, height: size, "aria-hidden": "true", focusable: "false", class: "ico" });
}

/** Small square icon button with tooltip + aria-label (text label only where the icon alone is ambiguous). */
export function iconButton(
  name: IconName,
  label: string,
  onclick: (e: Event) => void,
  opts: { text?: string; pressed?: boolean; cls?: string } = {},
): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `ibtn ${opts.cls ?? ""}`.trim();
  b.title = label;
  b.setAttribute("aria-label", label);
  if (opts.pressed !== undefined) b.setAttribute("aria-pressed", String(opts.pressed));
  b.append(icon(name));
  if (opts.text) b.append(document.createTextNode(opts.text));
  b.addEventListener("click", onclick);
  return b;
}

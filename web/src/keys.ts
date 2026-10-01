// Global keyboard map. Ignored while typing in a field (except Esc).
import { CLEAR_REASON } from "./inspector";
import { showHelp } from "./overlays";
import { visibleAlerts } from "./panels";
import { patch, selectAlert, selectAoi, state, triage } from "./store";
import { togglePlay } from "./timeline";
import { closeFocused, openWindow } from "./wm";

/** `windows` maps a shortcut key to a window toggle (wired in main.ts). */
export function installKeys(windows: Record<string, () => void>): void {
  let cursor = -1;
  document.addEventListener("keydown", (e) => {
    const t = e.target as HTMLElement;
    const typing = t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement;
    if (e.key === "Escape") {
      if (typing) return void t.blur();
      if (closeFocused()) return;
      if (state.selectedEntity) return patch({ selectedEntity: null });
      if (state.selected) patch({ selected: null, detail: null, tracks: [] });
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    const list = visibleAlerts();
    const sel = state.selected;
    const move = (d: number): void => {
      const i = sel ? list.findIndex((a) => a.id === sel) : cursor;
      cursor = Math.min(list.length - 1, Math.max(0, i + d));
      const a = list[cursor];
      if (a) {
        void selectAlert(a.id, false);
        document.querySelector(`[data-id="${CSS.escape(a.id)}"]`)?.scrollIntoView({ block: "nearest" });
      }
    };
    const needSel = (fn: (id: string) => void): void => {
      if (sel) fn(sel);
    };
    switch (e.key) {
      case "/": {
        e.preventDefault();
        document.getElementById("global-q")?.focus();
        break;
      }
      case "j": move(1); break;
      case "k": move(-1); break;
      case "Enter": {
        const a = sel ?? list[Math.max(0, cursor)]?.id;
        if (a) {
          void selectAlert(a);
          openWindow("inspector");
        }
        break;
      }
      case "a": needSel((id) => void triage(id, "acknowledged")); break;
      case "d": needSel((id) => void triage(id, "dismissed", CLEAR_REASON)); break;
      case "f": {
        const a = state.alerts.find((x) => x.id === sel);
        if (a && a.lat !== null && a.lon !== null) patch({ fly: { lat: a.lat, lon: a.lon, n: Date.now() } });
        break;
      }
      case " ": e.preventDefault(); togglePlay(); break;
      case "?": showHelp(); break;
      default:
        if (windows[e.key]) {
          windows[e.key]!();
          break;
        }
        if (/^[1-5]$/.test(e.key)) {
          const s = state.summary[Number(e.key) - 1];
          if (s) selectAoi(s.name);
        }
    }
  });
}

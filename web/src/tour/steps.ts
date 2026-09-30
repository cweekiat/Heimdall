// Tour content: the operational workflow monitor -> triage -> verify -> decide -> log, in plain words.
// Each step points at a real element through a data-tour or data-win attribute.

export interface TourStep {
  id: string;
  phase: "Monitor" | "Triage" | "Verify" | "Decide" | "Log";
  title: string;
  body: string;
  /** CSS selector of the element to highlight. If nothing matches, the step is skipped. */
  anchor: string;
  /** Window to open first (window id), if any. */
  open?: string;
  /** Select the first alert first, so the details window has something to show. */
  needsAlert?: boolean;
}

export const TOUR_STEPS: TourStep[] = [
  {
    id: "toolbar",
    phase: "Monitor",
    title: "The toolbar",
    body: "Each icon opens or closes a window: alerts, areas, layers, data sources, actions, timeline and shift summary. Hover an icon to see its keyboard key. The ? icon starts this tour again.",
    anchor: '[data-tour="toolbar"]',
  },
  {
    id: "map",
    phase: "Monitor",
    title: "The map and what its colours mean",
    body: "A red area marker is one map cell about 52 km across where GPS jamming was detected during a time window. It is not a single aircraft. Alerts about one vessel or one aircraft are single-track alerts and sit on that track. On the traffic layers, neutral colour is normal traffic and amber is military.",
    anchor: '[data-tour="map"]',
  },
  {
    id: "legend",
    phase: "Monitor",
    title: "Legend and 2D / 3D",
    body: "The legend explains the symbols. Use the 2D / 3D buttons in the map controls to tilt the map and show altitude. A grey outline means too little data to judge, which is not the same as no activity.",
    anchor: '[data-tour="legend"]',
  },
  {
    id: "layers",
    phase: "Monitor",
    title: "Layers and filters",
    body: "Switch aircraft, ships, notices and coverage on or off, and filter what you see. Turn things off when the map is busy.",
    anchor: '[data-win="layers"]',
    open: "layers",
  },
  {
    id: "queue",
    phase: "Triage",
    title: "The alert queue",
    body: "Alerts are sorted by priority. Red means attention now. Start at the top and click one to select it and see it on the map. The number on the toolbar icon counts alerts nobody has acknowledged yet.",
    anchor: '[data-win="queue"]',
    open: "queue",
  },
  {
    id: "details",
    phase: "Verify",
    title: "Alert details",
    body: "The top shows the reference (for example HMD-20260930-0007), the sources behind the alert, and one plain sentence. Quote the reference when you report or discuss an alert.",
    anchor: '[data-win="inspector"]',
    needsAlert: true,
  },
  {
    id: "evidence",
    phase: "Verify",
    title: "Why it fired, and what would clear it",
    body: "What triggered it lists each rule with the measured value against its threshold and the normal level. The trend chart shows the affected share against normal. Clears when says what would make the alert go away. Data coverage tells you how much of the area we could actually see.",
    anchor: '[data-win="inspector"]',
    needsAlert: true,
  },
  {
    id: "trust",
    phase: "Verify",
    title: "Corroboration and Admiralty grade",
    body: "Two badges show how reliable the source is (A to F) and how likely the information is true (1 to 6). Other sources lists independent confirmation. No second source yet never means false; it only means not confirmed yet.",
    anchor: '[data-win="inspector"]',
    needsAlert: true,
  },
  {
    id: "actions",
    phase: "Decide",
    title: "The actions board",
    body: "Actions come only from an approved list (the playbook). Pick one that fits the evidence. You cannot type your own commands here, and nothing runs without your decision.",
    anchor: '[data-win="actions"]',
    open: "actions",
  },
  {
    id: "log",
    phase: "Log",
    title: "Log your decision",
    body: "Acknowledge or dismiss the alert and say whether it was useful. That record goes into the shift summary and helps tune thresholds later. The timeline shows what happened and when.",
    anchor: '[data-win="timeline"]',
    open: "timeline",
  },
  {
    id: "sources",
    phase: "Monitor",
    title: "Data sources and API keys",
    body: "Check that each source is fresh. Turn a source on or off here, and add optional API keys. Keys are stored on this computer only and are never shown again after saving.",
    anchor: '[data-win="sources"]',
    open: "sources",
  },
];

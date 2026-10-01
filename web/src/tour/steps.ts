// Tour content: short, one idea per step. Map steps draw an EXAMPLE scene (tour/demo.ts) so the
// analyst sees what each visual looks like before real data arrives.
import type { DemoScene } from "./demo";

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
  /** Example visuals to draw on the map for this step. */
  demo?: DemoScene;
}

export const TOUR_STEPS: TourStep[] = [
  {
    id: "toolbar",
    phase: "Monitor",
    title: "Welcome to Heimdall",
    body: "Pages are on the left (hover to see names). Search is at the top, alerts top right, map controls bottom right. The next steps show example data on the map.",
    anchor: '[data-tour="toolbar"]',
  },
  {
    id: "gnss",
    phase: "Monitor",
    title: "GPS interference map",
    body: "Each hexagon is about 52 km across and shows the share of aircraft with poor GPS: green under 2 %, yellow 2-10 %, red over 10 %. It is not a single aircraft. Rolling 24 h, refreshed every 15 min.",
    anchor: '[data-tour="map"]',
    demo: "gnss",
  },
  {
    id: "alert",
    phase: "Triage",
    title: "Alerts on the map",
    body: "A red dot is an alert. Critical alerts get a small card with the basics; warnings are paler dots. Aircraft and ships are grey-blue; amber is military.",
    anchor: '[data-tour="map"]',
    demo: "alert",
  },
  {
    id: "heat",
    phase: "Verify",
    title: "Heat spots",
    body: "Amber circles are satellite fire and heat detections (NASA FIRMS). Bigger means hotter. Useful to check a fire, flare or strike near an alert.",
    anchor: '[data-tour="map"]',
    demo: "heat",
  },
  {
    id: "satellites",
    phase: "Monitor",
    title: "Satellites and passes",
    body: "Zoomed out, the map becomes a globe. Satellites are drawn at their height with a line to the ground; the bright line is where each goes in the next 100 minutes.",
    anchor: '[data-tour="map"]',
    demo: "satellites",
  },
  {
    id: "area",
    phase: "Monitor",
    title: "Areas of operation",
    body: "Draw areas (up to 250 nm) in Area of Ops. Green outline = monitored: data is collected and alerts are raised there. Grey = drawn but not monitored.",
    anchor: '[data-tour="map"]',
    demo: "area",
  },
  {
    id: "filters",
    phase: "Monitor",
    title: "Filters",
    body: "Show or hide layers (aircraft, ships, GNSS, C-band, L-band, heat spots, satellites) and each type of detection. Filters never change what is collected.",
    anchor: ".filter-btn",
  },
  {
    id: "queue",
    phase: "Triage",
    title: "The alert list",
    body: "Two categories: Critical and Warning. The number on the button counts alerts nobody has acknowledged yet.",
    anchor: '[data-for="queue"]',
  },
  {
    id: "details",
    phase: "Decide",
    title: "Alert details",
    body: "Why it fired, the evidence and how much to trust it. Two actions: Acknowledge (I have seen it) or Clear (close it; it is logged).",
    anchor: '[data-win="inspector"]',
    needsAlert: true,
  },
  {
    id: "sources",
    phase: "Log",
    title: "Data sources",
    body: "See what is collecting and switch sources on or off. Click a source that needs a key to paste it.",
    anchor: '[data-win="sources"]',
    open: "sources",
  },
];

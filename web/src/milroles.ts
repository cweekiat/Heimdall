// Military roles (task 039/046): labels, order and map colours, shared by the map, the filters, the Military panel
// and the aircraft list. Role comes from the server (plane-alert-db, then the ICAO type code). Red stays reserved
// for alerts and cyan for satellites (theme/palette.ts), so role colours avoid both.

export const ROLE_LABEL: Record<string, string> = {
  tanker: "Tankers",
  bomber: "Bombers",
  isr: "Surveillance",
  c2: "Command posts",
  airlift: "Transport",
  fighter: "Fighters",
  special: "Special forces",
  uav: "Drones",
  rotary: "Helicopters",
  gov_vip: "Government / VIP",
  trainer: "Trainers",
  other: "Other",
};

/** Roles that matter most for build-ups first. */
export const ROLE_ORDER = ["tanker", "bomber", "isr", "c2", "airlift", "special", "fighter", "uav", "gov_vip", "rotary", "trainer", "other"];

export const ROLE_RGB: Record<string, [number, number, number]> = {
  tanker: [255, 176, 32], // the military amber
  bomber: [255, 118, 40],
  isr: [190, 140, 230],
  c2: [235, 110, 205],
  airlift: [90, 160, 255],
  fighter: [205, 220, 70],
  special: [150, 115, 240],
  uav: [70, 205, 150],
  rotary: [150, 225, 120],
  gov_vip: [255, 160, 185],
  trainer: [160, 175, 200],
  other: [215, 170, 95],
};

export const roleOf = (p: { mil?: boolean; role?: string | null }): string | null => (p.mil ? (p.role ?? "other") : null);

export function roleRgba(role: string | null | undefined, alpha = 1): [number, number, number, number] {
  const c = ROLE_RGB[role ?? "other"] ?? ROLE_RGB.other!;
  return [c[0], c[1], c[2], Math.round(alpha * 255)];
}

export const roleCss = (role: string | null | undefined): string => `rgb(${(ROLE_RGB[role ?? "other"] ?? ROLE_RGB.other!).join(",")})`;

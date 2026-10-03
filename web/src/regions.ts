// Region presets (task 050): one-click views of the world's hot spots, found through the search box.
// Views only: they never change what is collected. Keywords make e.g. "hormuz" or "kaliningrad" find the region.

export interface Region {
  name: string;
  lon: number;
  lat: number;
  zoom: number;
  keys: string;
}

export const REGIONS: Region[] = [
  { name: "Global", lon: 40, lat: 20, zoom: 1.6, keys: "world everything" },
  { name: "Southeast Asia", lon: 110, lat: 10, zoom: 3.6, keys: "sea asean" },
  { name: "South China Sea", lon: 114, lat: 13, zoom: 4.6, keys: "spratly paracel philippines scs" },
  { name: "Taiwan Strait", lon: 120, lat: 24, zoom: 5.6, keys: "taiwan luzon strait" },
  { name: "Korea and Japan", lon: 132, lat: 36, zoom: 4.6, keys: "korea japan okinawa yellow sea" },
  { name: "Malacca and Singapore", lon: 102.5, lat: 2.8, zoom: 5.6, keys: "malacca singapore strait" },
  { name: "Myanmar", lon: 96, lat: 20, zoom: 5.2, keys: "burma myanmar" },
  { name: "Indian Ocean", lon: 75, lat: -5, zoom: 3.2, keys: "diego garcia maldives" },
  { name: "Gulf and Hormuz", lon: 53, lat: 26.5, zoom: 5, keys: "gulf persian hormuz iran qatar uae" },
  { name: "Red Sea and Horn of Africa", lon: 42, lat: 15, zoom: 4.8, keys: "red sea bab el mandeb yemen houthi djibouti" },
  { name: "Israel and Levant", lon: 35.5, lat: 32.5, zoom: 6, keys: "israel gaza lebanon syria levant" },
  { name: "East Mediterranean", lon: 30, lat: 35, zoom: 4.8, keys: "cyprus crete turkey" },
  { name: "Ukraine and Black Sea", lon: 33, lat: 47.5, zoom: 4.8, keys: "ukraine crimea black sea russia" },
  { name: "Baltic", lon: 21, lat: 57, zoom: 4.8, keys: "kaliningrad baltic poland finland" },
  { name: "High North", lon: 25, lat: 69.5, zoom: 4.4, keys: "arctic norway kola murmansk" },
  { name: "Western Europe", lon: 5, lat: 49, zoom: 4.4, keys: "europe ramstein mildenhall uk germany france" },
  { name: "Western Pacific and Guam", lon: 145, lat: 15, zoom: 4.2, keys: "guam pacific" },
  { name: "Australia (north)", lon: 131, lat: -14, zoom: 4.4, keys: "darwin tindal australia" },
];

export function findRegions(q: string): Region[] {
  const s = q.trim().toLowerCase();
  if (!s) return [];
  return REGIONS.filter((r) => r.name.toLowerCase().includes(s) || r.keys.split(" ").some((k) => k.startsWith(s)));
}

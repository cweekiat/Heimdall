import assert from "node:assert/strict";
import { test } from "node:test";
import type { MilAircraft } from "../src/api.ts";
import { EMPTY_QUERY, inBbox, matchQuery, selectRows, sortRows, toCsv, toKml } from "../src/aclist.ts";
import { roleOf, roleRgba } from "../src/milroles.ts";

const ac = (p: Partial<MilAircraft>): MilAircraft => ({
  hex: "ae1234",
  flight: null,
  type: null,
  type_name: null,
  registration: null,
  role: "other",
  country: null,
  operator: null,
  category: null,
  tags: [],
  lat: 1,
  lon: 104,
  alt: 20000,
  on_ground: false,
  gs: 400,
  track: 90,
  ts: "2026-10-03T06:00:00Z",
  high_interest: false,
  known: true,
  ...p,
});

const kc = ac({ hex: "ae04c5", flight: "RCH123", type: "K35R", type_name: "Boeing KC-135R", role: "tanker", operator: "United States Air Force", country: "United States", alt: 28000 });
const c17 = ac({ hex: "ae07e1", flight: "RCH9", type: "C17", type_name: "Globemaster III", role: "airlift", operator: "USAF", lon: 10, lat: 50, alt: 31000 });
const heli = ac({ hex: "43c001", type: "H60", role: "rotary", operator: "Royal Navy", country: "United Kingdom", on_ground: true, alt: null });

test("filters by callsign, type name, operator/country and role", () => {
    assert.equal(matchQuery(kc, { ...EMPTY_QUERY, callsign: "rch1" }), true);
    assert.equal(matchQuery(c17, { ...EMPTY_QUERY, type: "globemaster" }), true);
    assert.equal(matchQuery(heli, { ...EMPTY_QUERY, operator: "kingdom" }), true);
    assert.equal(matchQuery(kc, { ...EMPTY_QUERY, roles: ["airlift"] }), false);
  });

test("tabs: in view honours the antimeridian, selected uses the set", () => {
    const all = [kc, c17, heli];
    assert.deepEqual(selectRows(all, "view", EMPTY_QUERY, "90,-20,130,30", new Set()).map((a) => a.hex), ["ae04c5", "43c001"]);
    assert.equal(inBbox({ lat: 0, lon: -179 }, "170,-10,-170,10"), true);
    assert.deepEqual(selectRows(all, "selected", EMPTY_QUERY, "", new Set(["ae07e1"])).map((a) => a.hex), ["ae07e1"]);
  });

test("sorts by role order and by altitude", () => {
    assert.deepEqual(sortRows([heli, c17, kc], "role", false).map((a) => a.role), ["tanker", "airlift", "rotary"]);
    assert.deepEqual(sortRows([kc, heli, c17], "alt", true).map((a) => a.hex), ["ae07e1", "ae04c5", "43c001"]);
  });

test("exports CSV with formula guard and escaped KML", () => {
    const bad = ac({ operator: '=HYPERLINK("x")', flight: "<b>&" });
    const csv = toCsv([bad]);
    assert.ok(csv.split("\n")[1].includes(`"'=HYPERLINK(""x"")"`));
    const kml = toKml([bad]);
    assert.ok(kml.includes("&lt;b&gt;&amp;"));
    assert.ok(!kml.includes("<b>"));
  });

test("role colour only for military, other as fallback", () => {
    assert.equal(roleOf({ mil: false, role: "tanker" }), null);
    assert.equal(roleOf({ mil: true, role: null }), "other");
    assert.equal(roleRgba("nonsense", 0.5)[3], 128);
  });

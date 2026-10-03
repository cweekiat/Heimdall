import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BANDS,
  cellSentence,
  hfAlpha,
  jammerHover,
  parseStatus,
  sparkPath,
  worstState,
  bandCounts,
  bandDot,
  gnssDateLabel,
  gnssHover,
  inBand,
  parseGnssDay,
  readHash,
  spectrumHover,
  stepDay,
  writeHash,
} from "../src/spectrummodel.ts";
import type { LayerFeature } from "../src/p3model.ts";

test("bands are frequency ordered and filter alerts", () => {
  assert.deepEqual(BANDS.map((b) => b.band), ["hf", "vhf_marine", "sat_downlink", "l_1090", "l1_gnss", "l_1400", "c_sar"]);
  assert.equal(inBand({ bands: ["l1_gnss"] }, "l1_gnss"), true);
  assert.equal(inBand({ bands: ["l1_gnss"] }, "hf"), false);
  assert.equal(inBand({}, ""), true);
  assert.equal(inBand({}, "hf"), false); // older alerts without bands never match a band
  assert.deepEqual(bandCounts([{ bands: ["l1_gnss"] }, { bands: ["l1_gnss", "vhf_marine"] }, {}]), { l1_gnss: 2, vhf_marine: 1 });
});

test("status dot per band", () => {
  const hf = BANDS[0]!;
  assert.equal(bandDot(hf, []), "grey");
  assert.equal(bandDot(hf, [{ key: "wspr_live", status: "ok" }]), "green");
  assert.equal(bandDot(hf, [{ key: "wspr_live", status: "ok" }, { key: "pskreporter", status: "degraded" }]), "amber");
  assert.equal(bandDot(BANDS.find((b) => b.band === "l_1400")!, [{ key: "smap_rfi", status: "needs_key" }]), "amber");
  assert.equal(bandDot(hf, [{ key: "wspr_live", status: "ok", enabled: false }]), "grey");
});

test("hash deep link round trip and validation", () => {
  assert.deepEqual(readHash("#band=l1_gnss&gnss=2026-09-30"), { band: "l1_gnss", gnss: "2026-09-30" });
  assert.deepEqual(readHash("#band=evil&gnss=../x"), { band: "", gnss: "" });
  assert.equal(writeHash("#x=1", { band: "hf" }), "#x=1&band=hf");
  assert.equal(writeHash("#band=hf", { band: "" }), "");
});

test("GNSS day parsing, labels and day stepping", () => {
  const d = parseGnssDay({
    date: "2026-09-30",
    incomplete: true,
    hours_with_data: 16,
    cells: [
      { h3: "841f73dffffffff", good: 1, bad: 23, total: 24, pct: 91.67, cls: "red" },
      { h3: "not-a-cell", good: 1, bad: 0, total: 1, pct: 0, cls: "green" },
    ],
  })!;
  assert.equal(d.cells.length, 1);
  assert.match(gnssHover(d.cells[0]!), /23 of 24 aircraft degraded/);
  assert.equal(gnssDateLabel(d), "data for 2026-09-30 UTC · incomplete (16/24 h)");
  assert.equal(parseGnssDay({ nope: 1 }), null);
  assert.equal(stepDay(["2026-09-29", "2026-09-30"], "last24h", -1), "2026-09-30");
  assert.equal(stepDay(["2026-09-29", "2026-09-30"], "2026-09-29", -1), "2026-09-29");
  assert.equal(stepDay(["2026-09-29"], "2026-09-29", 1), "last24h");
});

test("hover text for spectrum layers uses plain words", () => {
  const f = (p: Record<string, unknown>): LayerFeature => ({
    type: "Feature",
    id: "x",
    geometry: { type: "Point", coordinates: [100, 10] },
    properties: { kind: "k", source: "s", ts: "2026-09-29T23:22:10Z", valid_from: null, valid_to: null, value: null, ...p },
  });
  assert.match(spectrumHover("sar_rfi", f({ platform: "sentinel-1d", rfi_detected: true, max_power_ratio: 18.7 })), /RFI detected · interference strength ×18.7/);
  assert.match(spectrumHover("hf", f({ mode: "WSPR", band_mhz: 14, n: 32, paths: 19, snr_med: -17 })), /WSPR 14 MHz · 32 spots on 19 paths · median SNR -17 dB/);
  assert.match(spectrumHover("satnogs", f({ sat_name: "PADRE", rf_band: "UHF", status: "bad" })), /PADRE UHF · pass bad/);
});

test("status board parsing, worst state per band, plain sentences", () => {
  const s = parseStatus({
    generated: "2026-10-01T00:00:00Z",
    bands: [{ band: "c_sar", label: "Sentinel-1 scenes with interference (3 d)", unit: "%", direction: "high" }],
    areas: [
      { aoi: "myanmar", label: "Myanmar", bands: { c_sar: { state: "anomalous", value: 60, median: 5, z: 5.5, n: 3, n_history: 500, explanations: [], spark: [5, null, 60] } } },
      { aoi: "baltic", label: "Baltic", bands: { c_sar: { state: "bogus", value: null } } },
    ],
  })!;
  assert.equal(s.areas[1]!.bands.c_sar!.state, "no_coverage"); // unknown state is never shown as normal
  assert.equal(worstState(s, "c_sar"), "anomalous");
  assert.equal(worstState(null, "c_sar"), "no_coverage");
  assert.match(cellSentence(s.areas[0]!.bands.c_sar!, s.bands[0]!), /60%.*vs typical 5% · 5.5 spreads towards trouble/);
  assert.equal(parseStatus({ nope: 1 }), null);
});

test("sparkline breaks at gaps; HF brightness and jammer hover", () => {
  assert.match(sparkPath([1, 2, null, 4], 30, 10), /^M0\.0 .* L10\.0 .* M30\.0 /);
  assert.equal(sparkPath([null, null]), "");
  assert.ok(hfAlpha(-28) < hfAlpha(0) && hfAlpha(null) === 0.4 && hfAlpha(99) <= 0.95);
  assert.match(jammerHover({ ref: "HMD-1", method: "horizon_fit", agreement: 0.86, major_km: 38 }), /HMD-1 · fit from aircraft altitudes · 86% of aircraft consistent · about 38 km/);
});

test("059 CYGNSS day: parse, strength vs thresholds, hover", async () => {
  const { parseCygnssDay, cygnssStrength, cygnssHover } = await import("../src/spectrummodel.ts");
  const d = parseCygnssDay({
    date: "2026-10-01",
    total_cells: 10,
    thresholds: { level_db: 2.5, land_level_db: 3.5, change_db: 1.5 },
    cells: [
      { h3: "833db0fffffffff", excess_db: 2.42, delta_db: 2.51, z: 7.7, n: 60, n_sats: 1, ocean_frac: 0, base_db: -0.09, base_days: 14, flag: "change" },
      { h3: "8366c1fffffffff", excess_db: 1.25, delta_db: null, n: 80, n_sats: 3, ocean_frac: 1, flag: null },
      { h3: "bad", excess_db: 9 },
      { h3: "8366c2fffffffff", excess_db: 0.8, flag: "evil<script>" },
    ],
  })!;
  assert.equal(d.cells.length, 3);
  assert.equal(d.cells[2]!.flag, null);
  assert.equal(cygnssStrength(d.cells[0]!, d), 1);
  assert.equal(cygnssStrength(d.cells[1]!, d), 0.5);
  assert.match(cygnssHover(d.cells[0]!), /\+2\.5 dB vs its usual -0\.1 dB \(14 days\) · unusual rise for this cell · land, 60 reflections, 1 satellite$/);
  assert.match(cygnssHover(d.cells[1]!), /no baseline yet · open ocean, 80 reflections, 3 satellites/);
  assert.equal(parseCygnssDay({ nope: 1 }), null);
});

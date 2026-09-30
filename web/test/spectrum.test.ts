import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BANDS,
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
  assert.deepEqual(BANDS.map((b) => b.band), ["hf", "vhf_marine", "l_1090", "l1_gnss", "l_1400", "c_sar"]);
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
  assert.equal(bandDot(BANDS[4]!, [{ key: "smap_rfi", status: "needs_key" }]), "amber");
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

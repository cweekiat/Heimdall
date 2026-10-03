# Demo: Operation Midnight Hammer pre-strike indicators (real data)

Heimdall replays the real military ADS-B traffic of 16–22 June 2025 (adsb.lol daily archives, ODbL) through its
military pipeline and checks each indicator of the public pre-strike brief
(military-aircraft-tracker.com, plus Flightradar24 / The Aviationist / ItaMilRadar reporting).

## Run it
```bash
uv run python -m heimdall.api.demo --replay data/replay/midnight_hammer/midnight_hammer.parquet --port 8030
```
Open http://127.0.0.1:8030. The **Demo** window steps through the timeline: each step moves the replay clock and the
map to that moment; "Open Heimdall's alert" shows the alert Heimdall raised from the same data, with its rules.

**On the live server** (task 043): `scripts/midnight_hammer.py` also writes a demo pack,
`data/replay/midnight_hammer/midnight_hammer.demo.json` (≈ 1 MB: steps, aircraft snapshots, alerts + evidence). The
live UI's **Demo** rail item (key `p`) plays it without any replay work on the server; live data is hidden until
**Exit demo**, and demo alerts are read-only.

Rebuild from scratch (≈17 GB streamed, raw archives deleted; output ≈ 25 MB):
```bash
for d in 2025-06-18 2025-06-19 2025-06-20 2025-06-21 2025-06-22; do OUT=data/replay/midnight_hammer/$d.parquet scripts/fetch_adsblol_day.sh $d --mil --every 60; done
uv run python scripts/midnight_hammer.py
```

## What the data shows
| Brief indicator | In open ADS-B | Heimdall |
|---|---|---|
| Airlift surge (C-17 / C-5M) | **Seen**: 38 transports crossed the Atlantic eastbound, mostly 18 Jun (Ramstein, Prestwick, …). Al Udeid / Diego Garcia arrivals not visible | *Possible build-up — Europe staging* 18 Jun 01:00 UTC (41 transports, 17 tankers flowing in) |
| B-2 deployment | **Not visible**: no B-2 broadcast (they flew dark) | – |
| Tanker surge | **Seen**: tankers crossing to European bases; 21 tankers airborne over Europe on 20 Jun vs a usual 3 | deployment + surge alerts, Europe staging |
| ISR build-up | **Partly**: RC-135V OLIVE41 near Crete on 20 Jun; nothing broadcasting over the Gulf | East Mediterranean build-up alert |
| E-6B / E-4B | **Seen**: E-6B MACRO21 airborne near Tinker 22:49–23:38 UTC 21 Jun, at strike time (not over the Indian Ocean as the brief says); E-4Bs on 18 and 22 Jun | high-interest alerts, Continental US |
| NOTAM closures | **No data**: no 2025 NOTAM archive in Heimdall (live runs ingest FAA / NGA notices) | – |
| Strike night | **Seen**: 7 tankers over the central / eastern Mediterranean after 23:00 UTC 21 Jun | – |

Lesson: the strike package itself was invisible; the **logistics chain into Europe** was the visible, early signal —
Heimdall flagged it four days before the strike. Indicators show preparation, never a target or a strike; the
visible westbound B-2 group was reportedly a decoy. Thresholds are priors and the surge baseline here is built from
the same week (no quiet reference period).

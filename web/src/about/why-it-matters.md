# Seeing the Invisible Fight: Why Open-Source EW Monitoring Matters for National Security

Modern conflict starts well before the first shot. It shows up in the electromagnetic spectrum, where GPS signals are jammed or spoofed, and in the movement of aircraft and ships whose patterns quietly change. It also shows up in outages, fires and warnings that, taken one at a time, look like noise.

For a small, trade-dependent state in Southeast Asia, these signals are not academic. The Malacca and Singapore Straits carry a large share of the world's seaborne trade. The South China Sea, the Taiwan and Luzon Straits and the conflict in Myanmar all sit within a few hours' flight. Disruption to navigation, whether deliberate or accidental, can reach shipping, aviation and the economy long before anyone makes an official statement.

## The problem: signals without sense-making

A great deal of this activity is already visible in free, open data:

- **Aircraft (ADS-B) broadcasts** report position and navigation integrity. When many aircraft in one area report degraded GPS at the same time, that points to interference.
- **Ship (AIS) broadcasts** show vessels loitering, going dark, or "teleporting" to impossible positions, a classic sign of spoofing.
- **Navigational warnings and NOTAMs** announce live-fire exercises, missile tests and closed airspace.
- **Satellite fire detections, internet outage monitors and night-lights imagery** reveal strikes, blackouts and displacement on land.

The raw data is not what's scarce. What's scarce is the analyst time needed to fuse it, to separate a real surge from a quiet Sunday, and to explain *why* something deserves attention.

## What Heimdall does

Heimdall is an open-source-intelligence monitor that watches these feeds continuously and raises **explainable, cross-checked alerts**:

1. **Detect.** Rules flag GNSS jamming and spoofing, military loitering and unusual activity, always measured against the local baseline rather than a global threshold.
2. **Corroborate.** Alerts are fused across domains (air, sea, warnings, and in future phases thermal, connectivity and imagery) and graded with the **Admiralty system** (source reliability A–F × information credibility 1–6), the same language intelligence professionals already use.
3. **Explain.** Every alert carries its evidence: the rules that fired, the observed values against thresholds, the underlying records, and what would have had to be different for no alert to fire. There is no black box.
4. **Stay honest.** An alert must persist across more than one time window. Confidence is lowered where sensor coverage is thin, and the absence of evidence is labelled "Uncorroborated", never "False".

## Why this matters for national security

**Early warning at low cost.** Governments pay heavily for classified collection. An open-data layer does not replace it, but it provides a continuous, shareable baseline that can cue scarcer assets to where they matter.

**Shareability.** Because Heimdall is built only on open sources, its findings can be shared with allies, industry, port authorities and the public without exposing sensitive sources and methods. That makes it valuable for coalition work and for strategic communication.

**Resilience of critical infrastructure.** GNSS interference is a safety problem as much as a military one. Airlines, ports and maritime pilots benefit from timely, evidence-backed warnings.

**Countering disinformation.** In a crisis, claims spread faster than facts. A tool that says "here is what the sensors show, here is how confident we are, and here is what we cannot see" helps decision-makers resist both panic and denial.

**Sovereign capability.** Heimdall runs on a laptop, uses free data, and keeps everything local by default. Small states can own their monitoring capability instead of depending entirely on others' assessments.

## The bottom line

The spectrum and the shipping lanes are contested every day, mostly out of sight. Heimdall's aim is modest but important: to turn scattered public signals into evidence a human analyst can trust, question and act on.

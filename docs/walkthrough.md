# Walkthrough: from alert to logged decision

The same steps as the in-app tour (it starts on your first visit; relaunch it from the **?** icon in
the toolbar, then "Take the guided tour"). Try it on demo data:

```
uv run python -m heimdall.api.demo      # synthetic data, no network
```

open http://127.0.0.1:8000. The Hormuz replay (task 014d) works the same way once loaded.

The workflow is: **monitor, triage, verify, decide, log.**

## 1. Monitor

- **Toolbar** (left edge): each icon opens a window. Alerts `q`, Areas `b`, Layers `l`, Data sources `u`,
  Actions `x`, Timeline `t`, Shift summary `h`, Help `?`.
- **Map and legend.** Read the map in plain words:
  - A **red area marker** is one map cell (an H3 cell) about **52 km across** where GPS jamming was
    detected over a time window. It is **not a single aircraft**.
  - **Single-track alerts** cover one vessel or one aircraft and sit on that track.
  - On the traffic layers, **neutral colour is normal traffic and amber is military**.
  - A grey outline means too little data to judge. That is not the same as no activity.
- **2D / 3D**: the map controls tilt the map and show altitude.
- **Layers and filters** (`l`): switch aircraft, ships, notices and coverage on or off; filter to military
  only, an altitude band or ship types.

## 2. Triage

Open the **alert queue** (`q`). Alerts are sorted by priority; red means attention now. Use `j` / `k`
to move and `Enter` to open one. The toolbar badge counts alerts nobody has acknowledged.

## 3. Verify

In **Alert details** check, in order:

1. **Reference** (for example `HMD-20260930-0007`), the **sources** behind it and the plain sentence.
2. **What triggered it**: each rule with the measured value against its threshold and the normal level.
3. **Trend**: the affected share against normal over the last hours.
4. **Clears when**: the counterfactual, what would make the alert go away.
5. **Data coverage** and **What we cannot see**: a quiet area with poor coverage is not a safe area.
6. **Corroboration and Admiralty grade**: two badges, source reliability (A to F) and how likely the
   information is true (1 to 6). **Other sources** lists independent confirmation. "No second source
   yet" never means false.

## 4. Decide

Open the **Actions board** (`x`). Actions come only from the approved playbook
(`config/playbook.yaml`); there are no free-form commands. Approving one records it and runs nothing
external.

## 5. Log

Acknowledge (`a`), mark under review (`r`), confirm (`c`) or dismiss with a reason (`d`), and add a note
(`n`). Every change goes to the alert's log and to the **Shift summary** (`h`), which you can download as
Markdown. The **Timeline** (`t`) shows what happened and when.

## Housekeeping

**Data sources** (`u`) shows whether each source is fresh, lets you switch one on or off, and takes
optional API keys. Keys are stored on this computer only and never shown again after saving.

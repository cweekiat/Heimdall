# aisstream fixtures

`stream_sample.jsonl`: handcrafted from the public AISStream documentation; replace with a recorded sample after source-scout verification.

Covers every handled message type, an `{"error": ...}` frame, not-available values (lat 91, lon 181, sog 102.3, cog 360, heading 511, lat=lon=0), bad MMSIs, an unhandled type carrying prompt-injection text, and repeated MMSIs for thinning.

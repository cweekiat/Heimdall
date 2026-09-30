"""Hand-built VNP46A2-like HDF5 tile (task 019). Same group/dataset names, QF and fill values as
the real product; tiny grid. Written to tmp_path by tests, never checked in as a binary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import h5py
import numpy as np

GRID = "HDFEOS/GRIDS/VNP_Grid_DNB/Data Fields"
FILL = 65535
SCALE = 0.1
CLOUD_CLEAR = 0
CLOUD_CONFIDENT = 3 << 6  # Cloud_Detection_Results = 3 (confident cloudy)


def make_tile(
    path: Path,
    n: int = 240,
    raw_value: int = 1000,
    edit: Callable[[dict[str, np.ndarray]], None] | None = None,
) -> dict[str, np.ndarray]:
    """Uniform lit tile (radiance raw_value * 0.1 nW/cm2/sr), all pixels good/clear/no snow.

    `edit` mutates the arrays (keys: ntl, qf, cloud, snow, gap_filled) before they are written.
    """
    arrays: dict[str, np.ndarray] = {
        "ntl": np.full((n, n), raw_value, np.uint16),
        "qf": np.zeros((n, n), np.uint8),
        "cloud": np.full((n, n), CLOUD_CLEAR, np.uint8),
        "snow": np.zeros((n, n), np.uint8),
        "gap_filled": np.full((n, n), 30000, np.uint16),  # must never be read
    }
    if edit is not None:
        edit(arrays)
    with h5py.File(path, "w") as f:
        g = f.create_group(GRID)
        ntl = g.create_dataset("DNB_BRDF-Corrected_NTL", data=arrays["ntl"])
        ntl.attrs["scale_factor"] = np.float32(SCALE)
        ntl.attrs["add_offset"] = np.float32(0.0)
        ntl.attrs["_FillValue"] = np.uint16(FILL)
        g.create_dataset("Mandatory_Quality_Flag", data=arrays["qf"])
        g.create_dataset("QF_Cloud_Mask", data=arrays["cloud"])
        g.create_dataset("Snow_Flag", data=arrays["snow"])
        gf = g.create_dataset("Gap_Filled_DNB_BRDF-Corrected_NTL", data=arrays["gap_filled"])
        gf.attrs["scale_factor"] = np.float32(SCALE)
    return arrays

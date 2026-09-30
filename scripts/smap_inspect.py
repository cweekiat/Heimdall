"""Print the structure and RFI flag attributes of a SMAP L1B TB HDF5 file (verifies smap.py fields).

Usage: uv run python scripts/smap_inspect.py path/to/SMAP_L1B_TB_NRT_*.h5
"""

from __future__ import annotations

import sys

import h5py

from heimdall.ingest.smap import GROUP, rfi_mask


def main(path: str) -> None:
    with h5py.File(path, "r") as f:
        g = f[GROUP]
        for name, ds in g.items():
            if isinstance(ds, h5py.Dataset):
                print(f"{GROUP}/{name} {ds.shape} {ds.dtype}")
        for name in ("tb_qual_flag_h", "tb_qual_flag_v"):
            ds = g[name]
            print(name, "flag_meanings:", ds.attrs.get("flag_meanings"))
            print(name, "flag_masks:", ds.attrs.get("flag_masks"))
            print(name, "RFI mask used:", rfi_mask(ds, 2))


if __name__ == "__main__":
    main(sys.argv[1])

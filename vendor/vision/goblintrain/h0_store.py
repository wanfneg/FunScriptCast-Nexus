"""Per-row store of the frozen frontend's output, so head refits pay
only the TCN.

The frontend (mask net -> attention pooling -> 1x1 input projection)
is pointwise in time and deterministic: h0[t] is a pure function of
row t's latent grid, its cut flag and the frontend weights --
weights every checkpoint grafted from one frozen trunk shares. So h0
is a per-row quantity, independent of any window or chunk layout, and
computing it once serves every refit sweep over that trunk. Without
it, a corpus refit epoch re-streams the whole latent tree (~72 KB per
row) and spends ~5x the TCN's multiplies in the mask net recomputing
a value that never changes.

Rows are stored as the autocast frontend's own bf16 bits in an int16
npz member (numpy has no bfloat16): a consumer reinterprets, never
converts. ~0.5 KB per row -- the read volume of an epoch drops ~150x,
and the corpus store is a few GB beside the caches it derives from.

Equivalence scope, measured: conv kernels are batch-SHAPE-sensitive
at 1 bf16 ulp (the live path's own h0 for a window depends on which
shuffled batch it lands in), so store-vs-live comparisons are
BEHAVIORAL, with head weights differing at ulp scale. Store-vs-store
is bit-wise and stronger than the live path's own guarantee: every
consumer of one store reads identical h0 bits regardless of batch
membership.

Layout: <dataset>/h0/<key>/<ID>.npz -- every store under one parent,
because a trunk's frozen frontend is shared by every checkpoint
grafted from it and the key is what says which. The key hashes the
frontend weights and the feature/mask config (the JepaClip identity);
each clip file additionally stamps the data the frontend consumed
there (row count, cut rows, the mask sidecar) so a boundary or
mask change rebuilds exactly the clips it touched. The store is a
derived, discardable cache: deleting any of it costs one rebuild pass.
"""
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import torch

from . import common

BUILD_CHUNK = 4096          # rows per frontend forward while building


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def frontend_key(model, ck):
    """Hex key of everything that determines h0 corpus-wide: the
    frontend weights and the feature/mask configuration."""
    h = hashlib.sha256()
    for mod_name, mod in (("mask", model.mask), ("inp", model.inp)):
        for name, t in sorted(mod.state_dict().items()):
            h.update(f"{mod_name}.{name}{tuple(t.shape)}".encode())
            h.update(t.detach().cpu().contiguous().numpy().tobytes())
    arch = ck.get("arch", {})
    # the feature configuration by CONTENT: the row grid and whether banner
    # masks were applied (their content is in each clip's stamp). Directory
    # names carry no identity.
    cfg = {"row_hz": ck.get("row_hz"), "masks": bool(ck.get("masks_dir")),
           "coord": arch.get("coord"), "cut_flag": arch.get("cut_flag")}
    h.update(json.dumps(cfg, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def _clip_stamp(clip, masks_dir):
    mj = Path(masks_dir) / f"{clip.id}.json" if masks_dir else None
    return {
        "rows": int(clip.feats.shape[0]),
        "cut": _sha(clip.cut.numpy().tobytes()),
        "mask": _sha(mj.read_bytes()) if mj is not None and mj.exists()
                else "",
        "scale": clip.feat_scale,
    }


def _build(model, clip, path, device, stamp):
    """One frontend pass over the clip, written atomically. The stamp
    rides a second member of the same savez, so a partial write can
    never look current: the rename lands the rows and the stamp that
    describes them together or not at all."""
    n = int(clip.feats.shape[0])
    warm = 0        # every frontend stage is pointwise in time
    out = []
    with torch.no_grad():
        for a in range(0, n, BUILD_CHUNK):
            e = min(a + BUILD_CHUNK, n)
            a0 = max(0, a - warm)
            x = clip.fwin(a0, e)
            if clip.masked:
                clip.mask_feats(x)
            x = x.unsqueeze(0).to(device).float()
            if clip.feat_scale != 1.0:
                x.mul_(clip.feat_scale)
            cut = clip.cut[a0:e].unsqueeze(0).to(device)
            with torch.autocast("cuda", dtype=torch.bfloat16,
                                enabled=device == "cuda"):
                h0, _gate = model.frontend(x, cut)
            out.append(h0[0, :, a - a0:].T.to(torch.bfloat16).contiguous()
                       .view(torch.int16).cpu().numpy())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, feats=np.concatenate(out, axis=0),
             stamp=np.array(json.dumps(stamp)))
    os.replace(tmp, path)


def attach(model, ck, clips, dataset, device):
    """Ensure every clip's h0 rows exist and match their stamps, building
    the missing or stale ones, then hang a ``RowStream`` on each clip as
    ``clip.h0`` (plus the store's channel count as ``clip.h0_ch``). The
    refit's batch path reads these instead of the latent caches."""
    for clip, s in zip(clips, _attach_one(model, ck, clips, dataset, device)):
        clip.h0 = s
        clip.h0_ch = int(s.shape[1])


def _attach_one(model, ck, clips, dataset, device):
    """One trunk's store over ``clips``: the stream per clip, in order."""
    key = frontend_key(model, ck)
    root = Path(dataset) / common.H0_DIR / key
    masks_dir = Path(dataset) / ck["masks_dir"] if ck.get("masks_dir") else None
    meta = root / "meta.json"
    built = reused = 0
    streams = []
    for clip in clips:
        path = root / f"{clip.id}.npz"
        stamp = _clip_stamp(clip, masks_dir)
        ok = False
        if path.exists():
            try:
                with np.load(path) as z:
                    ok = json.loads(str(z["stamp"])) == stamp \
                        and z["feats"].shape[0] == stamp["rows"]
            except Exception:
                ok = False
        if not ok:
            _build(model, clip, path, device, stamp)
            built += 1
        else:
            reused += 1
        streams.append(common.RowStream(path, "feats"))
    if built and not meta.exists():
        meta.write_text(json.dumps(
            {"key": key, "trunk_epoch": ck.get("epoch"),
             "ch": int(model.inp.out_channels)}, indent=1),
            encoding="utf-8")
    print(f"h0 store {root}: {reused} clips reused, {built} built",
          flush=True)
    return streams

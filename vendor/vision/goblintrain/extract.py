"""Latent extraction: the frozen perception pass, one clip at a time.

The frozen V-JEPA 2.1 ViT-B encoder runs over the clip at the training
frame grid (``grid_fps``, the full frame squashed to the encoder's input
size). Tubelets are frame pairs ``tubelet_stride`` frames apart; every one
of the ``alignments`` offsets is encoded, and the rows interleave back to
one row per decoded frame, so the row rate equals the frame rate. Windows
sit on a fixed frame grid and never consult the cut list (cut-blind): cuts
reach the model only through the training-time cut-flag channel.

Per row the grid x grid tokens are projected on the FROZEN whitened PCA
basis (``weights/pca_basis.npz``) and the top ``dim`` components are stored
int8 at the fixed scale 8/127 to ``<project>/latents/<id>.npz``:
``feats`` (T, dim, grid, grid) int8, ``times_ms`` (T,) the covered pair's
midpoint in absolute video time, ``scale``, and the perception stamps
every reader checks (``basis_id``, ``grid_fps``, ``tubelet_stride``,
``alignments``, ``row_hz``, ``cut_blind``).

Streaming and resumable: rows accumulate in shards under
``latents/_shards_<id>/`` (a killed run re-seeks past complete shards);
after the last shard the run merges them into the cache and removes the
shard dir. Peak RAM is one shard. The cache appears under its final name
only when it is complete.
"""
import io
import os
import shutil
import time
import zipfile

import numpy as np
import torch

from . import common, encoders
from . import perception as P

CLIP_LEN = 32                 # frames per window per alignment
SHARD_ROWS = 9600             # rows per shard (a multiple of CLIP_LEN)
INT8_SCALE = np.float32(8.0 / 127.0)   # |whitened latent| < 8 corpus-wide;
                                       # the rare value beyond clips


class Basis:
    """The frozen basis on the device, ready to project tokens."""

    def __init__(self, perception, device):
        z = np.load(common.BASIS_PATH)
        self.id = common.basis_id(z["mean"], z["components"], z["evals"])
        if self.id != perception["basis_id"]:
            raise SystemExit(f"{common.BASIS_PATH} is basis {self.id}, the "
                             f"project is frozen to {perception['basis_id']}")
        dim = int(perception["dim"])
        self.dim = dim
        self.tok_dim = int(z["mean"].shape[0])
        self.mu = torch.from_numpy(z["mean"]).half().to(device)
        self.W = torch.from_numpy(
            z["components"][:, :dim]
            / np.sqrt(np.maximum(z["evals"][:dim], 1e-6))).half().to(device)


def load_encoder(perception, device):
    enc = encoders.Encoder(encoders.DEFAULT, device)
    for p in enc.model.parameters():
        p.requires_grad_(False)
    if enc.res != perception["enc_res"] or enc.grid != perception["grid"]:
        raise SystemExit(f"encoder is {enc.res} px on a {enc.grid} grid; the "
                         f"project is frozen to {perception['enc_res']} px "
                         f"on {perception['grid']}")
    return enc


def path(project, clip_id):
    return project.root / common.LATENTS_DIR / f"{clip_id}.npz"


def _write_int8_npz(tmp_out, shards, t, dim, grid, stamps):
    """Merge fp16 shards into the int8 cache, streaming: the assembled
    array never exists. The hand-written STORED member carries an npy 1.0
    header, byte-compatible with np.savez, so np.load and
    common.RowStream read it unchanged."""
    shape = (len(t), dim, grid, grid)
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_STORED) as z:
        with z.open("feats.npy", "w", force_zip64=True) as fp:
            np.lib.format.write_array_header_1_0(
                fp, {"descr": np.lib.format.dtype_to_descr(np.dtype(np.int8)),
                     "fortran_order": False, "shape": shape})
            n = 0
            for s in shards:
                part = np.load(s)["feats"]
                for a in range(0, len(part), 4096):
                    x = part[a:a + 4096].astype(np.float32)
                    np.clip(np.round(x / INT8_SCALE), -127, 127, out=x)
                    fp.write(np.ascontiguousarray(x.astype(np.int8)).tobytes())
                n += len(part)
                del part
        if n != len(t):
            raise SystemExit(f"shard rows {n} != times {len(t)}: corrupt "
                             "shard dir")
        for name, arr in {"times_ms": t, "scale": INT8_SCALE,
                          **stamps}.items():
            b = io.BytesIO()
            np.save(b, np.asarray(arr))
            z.writestr(name + ".npy", b.getvalue())
    return shape


def ensure(project, clip_id, enc, basis, device, log=print):
    """Extract the clip's latents unless the cache exists. Returns True
    when it was written."""
    out = path(project, clip_id)
    if out.is_file():
        return False
    pc = project.config["perception"]
    fps = float(pc["grid_fps"])
    k = int(pc["tubelet_stride"])
    n_align = int(pc["alignments"])
    row_hz = fps * n_align / (2.0 * k)
    GROUP = (CLIP_LEN // 2) * 2 * k          # frames (== rows) per group
    meta = P.load_meta(project.root, clip_id)
    out.parent.mkdir(exist_ok=True)
    shard_dir = out.parent / f"_shards_{clip_id}"
    shard_dir.mkdir(exist_ok=True)
    shards_on_disk = sorted(shard_dir.glob("[0-9]*.npz"))
    n_shards = len(shards_on_disk)
    # the TRUE row count, read from the shards: a shard overshoots by up to
    # one window, so the product form would re-emit rows the shards hold
    resume_rows = 0
    for s in shards_on_disk:
        with np.load(s) as z:
            resume_rows += len(z["times_ms"])
    rpf = n_align / (2.0 * k)          # rows per decoded frame
    resume_frame = int(round(resume_rows / rpf))
    # windows sit on a fixed GROUP grid, so any group boundary is a
    # bitwise-identical resume point
    start_frame = (resume_frame // GROUP) * GROUP
    skip_rows = resume_rows - int(round(start_frame * rpf))
    if n_shards:
        log(f"  [{clip_id}] resuming past {n_shards} shards ({resume_rows} "
            "rows)")
    feats, times = [], []
    buf, tbuf = [], []
    n_frames, n_rows = 0, 0
    t0 = time.time()
    total_frames = meta["duration_ms"] / 1000.0 * fps
    last_pct = [-1]

    def write_shard():
        nonlocal n_shards, feats, times, n_rows
        f = np.concatenate(feats)
        t = np.concatenate(times)
        tmp = shard_dir / "_tmp_shard.npz"
        np.savez(tmp, feats=f, times_ms=t)
        os.replace(tmp, shard_dir / f"{n_shards:05d}.npz")
        n_shards += 1
        feats, times, n_rows = [], [], 0

    def encode(x):
        """(n,res,res,3) uint8 -> (n//2,dim,grid,grid) fp16 on cpu."""
        y = enc.tokens(x)
        y = (y.reshape(-1, enc.grid, enc.grid, enc.dim) - basis.mu) @ basis.W
        return y.permute(0, 3, 1, 2).cpu().numpy().astype(np.float16)

    def emit(f_arr, t_arr):
        nonlocal skip_rows, n_rows
        if skip_rows:                  # rows the shards already hold
            n = min(skip_rows, len(f_arr))
            f_arr, t_arr = f_arr[n:], t_arr[n:]
            skip_rows -= n
        if len(f_arr):
            feats.append(f_arr)
            times.append(t_arr)
            n_rows += len(f_arr)

    def flush():
        """Emit one group of rows: one row per frame, whatever the stride.
        Every row is owned by a first-frame index ``a``; its tubelet is the
        frame pair ``(a, a+k)``. Alignment j collects a = j, j+2k, j+4k, ...
        and the ``n_align`` alignments interleave back to a row per frame."""
        nonlocal buf, tbuf
        limit = min(GROUP, len(buf) - k)      # rows this group can produce
        if limit <= 0:
            return 0
        aa_all, t_all, y_all = [], [], []
        for j in range(n_align):
            aa = list(range(j, limit, 2 * k))[:CLIP_LEN // 2]
            if not aa:
                continue
            idx = [i for a in aa for i in (a, a + k)]
            x = torch.from_numpy(np.stack([buf[i] for i in idx])).to(device)
            aa_all.append(np.asarray(aa))
            t_all.append(np.array([(tbuf[a] + tbuf[a + k]) / 2.0 for a in aa]))
            with torch.no_grad():
                y_all.append(encode(x))
        if not aa_all:
            return 0
        a_cat = np.concatenate(aa_all)
        o = np.argsort(a_cat, kind="stable")   # back into video order
        emit(np.concatenate(y_all)[o], np.concatenate(t_all)[o])
        buf, tbuf = buf[limit:], tbuf[limit:]
        if n_rows >= SHARD_ROWS:
            write_shard()
        return limit

    def end():
        nonlocal buf, tbuf
        if buf:
            # the last frames as static pairs: exact pose, zero observed
            # motion, so the final k frames still own a row
            last_t = tbuf[-1]
            for i in range(k):
                buf.append(buf[-1])
                tbuf.append(last_t + (i + 1) * 1000.0 / fps)
        while flush() > 0:
            pass
        buf, tbuf = [], []

    for _t_ms, frame in P.decode_frames(
            project.video_path(clip_id), w=enc.res, h=enc.res, fps=fps,
            start_s=start_frame / fps):
        # absolute frame index, not the seek-offset time: identical whether
        # the run was resumed or continuous
        fidx = start_frame + n_frames
        buf.append(frame)
        tbuf.append(fidx / fps * 1000.0)
        n_frames += 1
        if len(buf) >= GROUP + k:
            flush()
        pct = int(100 * (start_frame + n_frames) / max(total_frames, 1))
        if pct // 10 > last_pct[0] // 10 and pct < 100:
            last_pct[0] = pct
            el = time.time() - t0
            rate = n_frames / max(el, 1e-9)
            eta = (total_frames - start_frame - n_frames) / max(rate, 1e-9)
            log(f"  [{clip_id}] latents {pct:3d}%  ({rate:.0f} frames/s, "
                f"about {eta / 60:.0f} min left)")
    end()
    if feats:
        write_shard()
    shards = sorted(shard_dir.glob("[0-9]*.npz"))
    if not shards:
        raise SystemExit(f"[{clip_id}] no frames decoded")
    t = np.concatenate([np.load(s)["times_ms"] for s in shards])
    if not np.all(np.diff(t) > 0):
        raise SystemExit(f"[{clip_id}] shard times not monotonic: corrupt "
                         f"shard dir {shard_dir.name}")
    stamps = {"basis_id": basis.id, "grid_fps": np.float32(fps),
              "tubelet_stride": np.int32(k), "alignments": np.int32(n_align),
              "row_hz": np.float32(row_hz), "cut_blind": np.bool_(True)}
    tmp_out = out.parent / f"_tmp_{clip_id}.npz"
    shape = _write_int8_npz(tmp_out, shards, t, basis.dim, enc.grid, stamps)
    os.replace(tmp_out, out)
    el = time.time() - t0
    log(f"  [{clip_id}] latents: {shape[0]} rows, "
        f"{out.stat().st_size / 1e9:.1f} GB, {el / 60:.1f} min")
    try:
        shutil.rmtree(shard_dir)
    except OSError:
        log(f"  [{clip_id}] could not remove the shard directory; the cache "
            "is complete, delete it by hand")
    return True

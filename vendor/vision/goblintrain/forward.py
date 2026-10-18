"""The whole-clip forward: the model over a clip's timeline in decode
chunks with a warm context either side, returning the phase track and,
on request, every deploy head's per-row track. jepa_infer drafts from it,
parity reads its reference tracks from it and the lag fit reads its
marginal from it."""
import numpy as np
import torch

from . import common

MIN_PIECE_S = 2.0   # shortest forward piece that carries signal


def predict_shots(model, ds, shots, device, max_frames, collect_tracks=False,
                  ctx=None):
    """0-anchor velocity (normalized units) over the shot spans -> full-T
    array (NaN outside); long shots in hard ``max_frames`` chunks.
    ``collect_tracks`` additionally returns a dict of the auxiliary head
    tracks that exist on the checkpoint: ``level`` (position head), ``env``
    (generated amplitude envelope) and ``vmarg`` (the marginal phase
    velocity, = the returned track unless gen_env recombination is active).
    Forwards the continuous timeline with the cut channel -- matching how
    the model trained; the per-shot spans still define the audit
    decomposition.

    ``ctx``: rows of real timeline forwarded on BOTH sides of every chunk
    and cut from the output. A kept row then carries a warm TCN receptive
    field and a warm envelope AR history where a bare chunk edge sees zero
    padding and a freshly reseeded buffer -- the chunk boundary leaves the
    decode. ``None`` reads ``common.DECODE_CTX_S`` on the clip's grid; 0 is
    the bare tiling (what the pre-ctx decoders shipped)."""
    if ctx is None:
        ctx = common.rows_at(common.DECODE_CTX_S, ds.row_hz)
    T = len(ds.vel)
    full = np.full(T, np.nan)
    tracks = {}
    if collect_tracks:
        if getattr(model, "pos_head", False):
            tracks["level"] = np.full(T, np.nan)
        if getattr(model, "gen_env", False):
            tracks["env"] = np.full(T, np.nan)
        if getattr(model, "ext_head", False):
            tracks["blo"] = np.full(T, np.nan)
            tracks["bhi"] = np.full(T, np.nan)
        if getattr(model, "plat_head", False):
            tracks["plat_top"] = np.full(T, np.nan)
            tracks["plat_bot"] = np.full(T, np.nan)
        if getattr(model, "rev_head", False):
            tracks["rev_top"] = np.full(T, np.nan)
            tracks["rev_bot"] = np.full(T, np.nan)
        tracks["vmarg"] = np.full(T, np.nan)
    lo_all = min(lo for lo, _ in shots) if shots else 0
    hi_all = max(hi for _, hi in shots) if shots else 0
    spans = [(s, min(s + max_frames, hi_all))
             for s in range(lo_all, hi_all, max_frames)]
    with torch.no_grad():
        for s, e in spans:
            # a tail span shorter than 2 s still owns rows inside a val
            # shot: forward it with a lookback window (overlapping rows
            # recompute identically) and write only this span's rows.
            # Skipping it would leave NaN predictions that zero the
            # shot's last piece and poison the concatenated corr.
            mr = common.rows_at(MIN_PIECE_S, ds.row_hz)
            fs = s if e - s >= mr else max(lo_all, e - mr)
            fs = max(lo_all, min(fs, s) - ctx)
            fe = min(hi_all, e + ctx)
            if e - fs < mr:                        # >=2 s of signal
                continue
            off = s - fs
            oe = off + (e - s)      # kept slice inside the forward
            # the flow envelope's base draw is keyed to the ABSOLUTE row,
            # so the chunk has to say where it starts. Without this every
            # chunk redraws from row 0 and the envelope repeats with the
            # chunk period -- deterministic, reproducible, and wrong
            model.env_row0 = int(fs)
            # the envelope recurrence is causal, so it stops at the end
            # of the kept slice: the trailing context feeds the TCN alone
            model.env_rows = int(oe)
            if getattr(ds, "h0", None) is not None:
                # the refit's h0 store (h0_store.attach): per-row
                # frontend output as bf16 bits in int16, so any chunk
                # layout slices it and the forward pays the TCN alone
                # (~150x less read volume, no mask-net compute). The
                # store carries the autocast-bf16 frontend where the
                # live path here runs fp32, so store-read panels are
                # BEHAVIORAL against live ones: sweeps on one frozen
                # trunk standardize ON the store (store-vs-store is
                # bit-wise); reference cuts stay on the live path.
                front = (ds.h0[fs:fe][None].to(device).view(torch.bfloat16)
                         .float().transpose(1, 2), None)
                v, _conf, _gate = model(front=front)
            else:
                # the cache dtype rides the bus (int8 is 4x smaller than
                # fp32); dequant happens on the device copy, which is
                # also the writable copy the mask rects zero in place
                x = ds.fwin(fs, fe)[None].to(device).float()
                if ds.feat_scale != 1.0:      # int8 cache dequant
                    x.mul_(ds.feat_scale)
                if ds.masked:         # accepted banner-mask rects
                    x = ds.mask_feats(x)
                cut = ds.cut[fs:fe][None].to(device)
                v, _conf, _gate = model(x, cut=cut)
            full[s:e] = v[0].float().cpu().numpy()[off:oe]
            if collect_tracks:
                if "level" in tracks:
                    tracks["level"][s:e] = \
                        model.level[0].float().cpu().numpy()[off:oe]
                if "env" in tracks:
                    tracks["env"][s:e] = \
                        model.eg[0].float().cpu().numpy()[off:oe]
                if "blo" in tracks:
                    tracks["blo"][s:e] = \
                        model.ext_lo[0].float().cpu().numpy()[off:oe]
                    tracks["bhi"][s:e] = \
                        model.ext_hi[0].float().cpu().numpy()[off:oe]
                if "plat_top" in tracks:
                    pp = model.plat_logits.softmax(1)[0] \
                        .float().cpu().numpy()
                    tracks["plat_top"][s:e] = pp[1, off:oe]
                    tracks["plat_bot"][s:e] = pp[2, off:oe]
                if "rev_top" in tracks:
                    rp = model.rev_logits.softmax(1)[0] \
                        .float().cpu().numpy()
                    tracks["rev_top"][s:e] = rp[1, off:oe]
                    tracks["rev_bot"][s:e] = rp[2, off:oe]
                tracks["vmarg"][s:e] = \
                    (model.v_marginal[0].float().cpu().numpy()[off:oe]
                     if getattr(model, "gen_env", False) else full[s:e])
    return (full, tracks) if collect_tracks else full

"""The vjepa-core model: stroke regression from frozen V-JEPA 2.1 latents.

Mask net over the latent grid (taken from the cache shape) -> per-head
attention pooling of the latents -> dilated TCN trunk -> the heads.
Motion, including stroke direction, must come out of the frozen
features. Windows run straight across cuts with a per-row cut-flag
channel at the TCN input -- the model learns what a seam means; no
footage is discarded. Unscripted script gaps supervise nothing: their
interpolated targets say "hold still" regardless of the screen, the
deploy-domain lesson backwards.

The trunk recipe is fixed (recipes/*.json name the few knobs that
remain): the envelope-cotrained gap-masked trunk with the phase-cap
objective (per step, a supervised row whose softmax puts below-chance
mass on the +-2-bin neighborhood of its target leaves every phase-side
loss -- self-releasing, no labels, no sidecars), phase-only R-Drop
(level head excluded), and coordinate channels into the mask net with
per-head gate mass and attention centroid to the TCN input. The heads:

  * marginal velocity -- 41-bin CE, expectation decode; sole owner of
    PHASE (reversal timing). Phase supervision stays strict everywhere.
  * position level -- CE over the ~2 s-smoothed script position,
    class-balanced. Velocity cannot represent level (holds at the top
    and at mid-stroke are both zero velocity), but V-JEPA latents see
    pose -- the head anchors level-aware styling (jepa_infer) so drafts
    stop returning to the center during extremes and holds.
  * generative envelope -- an autoregressive head over the cycle-scale
    AMPLITUDE envelope, the axis where one-to-many authorship ambiguity
    lives. At decode the marginal signal is normalized to a phase
    carrier and rescaled by the generated envelope: zero crossings
    never move, amplitude commits. Its aux supervision also helps the
    trunk.
  * deploy refit (jepa_refit.py) -- AFTER training, the band-extent
    rails, the stillness envelope (the SHIPPING amplitude head -- the
    co-trained aux envelope above only shapes the trunk), the
    trapezoid-dwell head and the reversal-event head are refit on the
    run's own frozen best checkpoint and grafted in. The trunk
    trajectory is over before the refit starts, so the attention gate
    is the trained checkpoint's by construction (training the rails or
    the dwell head alongside the trunk measurably costs gate quality).
    Every deploy head lives here for that reason: they are decode-time
    signals, and the trunk pays for carrying them.

Checkpoint selection: mean 0-anchor val corr over per-clip held-out
segments (common.val_regions -- the ONE val definition every scorer
shares), always on the MARGINAL decode. Per-clip verdicts come from
the eval command's read tables.

Usage:
    python goblintrain.py train <project> --recipe recipes/v0.6.0.json
"""

import argparse
import collections
import itertools
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from . import common
from . import perception as P

BINS_DEFAULT = 41
W_DERIV = 0.05
W_CONF = 0.05
ENV_SMOOTH_S = 0.6   # envelope window (~one stroke
# period). A DURATION: the row count is taken on the run's grid (19 at
# 30 rows/s) and frozen into the checkpoint as arch ``env_k`` --
# left in rows the envelope halves in span and starts carrying stroke
# rhythm, so the phase carrier u = v/env stops being a pure normalizer
SLOW_BAND_POS_S = (5.0, 100.0)   # the slow-stroke read's band, ABSOLUTE
# pos/s so arms with different --vel-std stay comparable. mean0 is a
# whole-signal Pearson and the fast rows carry the variance, so a change
# that only moves this band is invisible to it -- measured twice on
# quantile arms, which lose mean0 and win the product panel. Reported
# beside mean0, never selected on: selection stays the shipped rule's.
# The floor keeps the stillness mass out; the ceiling sits just under the
# gate clips' own slow/mid speed edges (97 and 107 pos/s).
FEAT_DIR_DEFAULT = common.LATENTS_DIR
# roster NAMES, resolved against the project (common.resolve_ids): what a
# bare run trains on, the clips the wide gate reads, and the screening subset
GATE_IDS_DEFAULT = ["gate"]
IDS_DEFAULT = ["train"]
# RAM-resident screening subset (~46 GB int8-128 on 64 GB RAM). Screens A/B
# arms by RANK (same-seed shared-init); winners graduate to a full-corpus
# run. Absolute numbers are NOT comparable to corpus runs (the gate pair is
# ~69% of subset rows against ~24% of the corpus).


def pearson(a, b, dim=-1, eps=1e-8):
    a = a - a.mean(dim, keepdim=True)
    b = b - b.mean(dim, keepdim=True)
    return (a * b).mean(dim) / (a.std(dim) * b.std(dim) + eps)


def masked_ce(logits, tb, m, weight=None):
    """Per-frame CE over the supervised rows only (``m``: B,W in {0,1}),
    keeping F.cross_entropy's weighted-mean semantics."""
    ls = F.cross_entropy(logits, tb.round().long(), label_smoothing=0.05,
                         weight=weight, reduction="none")
    if weight is not None:
        return (ls * m).sum() / \
            (weight[tb.round().long()] * m).sum().clamp_min(1e-6)
    return (ls * m).sum() / m.sum().clamp_min(1)


def neighborhood_keep(logits, tb, cap, half=2):
    """Rows to KEEP: 1.0 where the model puts at-or-above-chance
    probability MASS on the +-``half``-bin NEIGHBORHOOD of the target bin,
    0.0 where it contradicts the target's velocity REGION.

    Chance is the uniform model's share of the realized window,
    ``(hi - lo) * e^-cap``, so the window narrows correctly at the edges of
    the bin array and ``cap = ln(bins)`` is the self-anchored dose. Use a
    hair ABOVE the anchor (3.72 for the 41 velocity bins): exactly at it a
    uniform prediction knife-edges into absorption.

    Self-releasing by construction — it is recomputed from the current
    model every step, reads no labels and writes no state.
    """
    t = tb.round().long()
    cs = F.softmax(logits, 1).cumsum(1)
    hi = (t + half).clamp(max=logits.shape[1] - 1)
    lo = (t - half - 1).clamp(min=-1)
    mass = cs.gather(1, hi.unsqueeze(1)).squeeze(1)
    mass = mass - torch.where(
        lo >= 0,
        cs.gather(1, lo.clamp(min=0).unsqueeze(1)).squeeze(1),
        torch.zeros_like(mass))
    # chance counts the bins the window REALLY spans: both ends clamp, so a
    # target at the edge of the bin array is tested against the narrower
    # window it actually got. An unclamped width would test a 3-bin window
    # against 5 bins of chance there and absorb correct predictions.
    chance = (hi - lo).float() * float(np.exp(-cap))
    return (mass >= chance).float()


def rdrop_kl(lg_a, lg_b, m):
    """Symmetric KL between two dropout views of the same window, over the
    supervised rows. Logits are (B,bins,W); ``m`` is the (B,W) row mask the
    matching CE uses (float -- soft flag weights carry through unchanged).

    Spends compute, not parameters: the second view costs a forward and no
    weights, so it raises VRAM use while pushing AGAINST memorization --
    the model must give the same answer under two different dropout masks."""
    la = F.log_softmax(lg_a, dim=1)
    lb = F.log_softmax(lg_b, dim=1)
    kl = 0.5 * ((la.exp() * (la - lb)).sum(1) + (lb.exp() * (lb - la)).sum(1))
    return (kl * m).sum() / m.sum().clamp_min(1e-6)


def masked_pearson(a, b, m, eps=1e-8):
    """Row-masked per-window Pearson."""
    n = m.sum(-1).clamp_min(1)
    am = (a * m).sum(-1) / n
    bm = (b * m).sum(-1) / n
    av = (a - am.unsqueeze(-1)) * m
    bv = (b - bm.unsqueeze(-1)) * m
    return (av * bv).sum(-1) / n / (
        (av.square().sum(-1) / n).sqrt()
        * (bv.square().sum(-1) / n).sqrt() + eps)


def envelope(v, k):
    """Cycle-scale amplitude envelope: box-smoothed |v|. Rhythm-free by
    construction (k spans about one stroke period), it is the quantity
    the generative envelope head models and the normalizer that turns
    the marginal velocity into a phase carrier. ``k`` is REQUIRED and
    grid-dependent: the model's ``env_k`` for anything checkpoint-scoped,
    ``rows_at(ENV_SMOOTH_S, row_hz, odd=True)`` otherwise."""
    return F.avg_pool1d(v.abs().unsqueeze(1), k, stride=1, padding=k // 2,
                        count_include_pad=False).squeeze(1)


READ_POOL = ThreadPoolExecutor(8, thread_name_prefix="rowread")


def _span_any(flag_src, bounds):
    """Per target row, whether any source row of its span carries the
    flag: ``bounds`` is (W+1,) with span k = [bounds[k], bounds[k+1]).
    A repeated source row spans nothing after its first target row, so a
    flag lands once, on the first target row that covers it."""
    cs = torch.cat([flag_src.new_zeros(1, dtype=torch.float32),
                    flag_src.float().cumsum(0)])
    return (cs[bounds[1:]] - cs[bounds[:-1]]) > 0


PREFETCH_POOL = ThreadPoolExecutor(1, thread_name_prefix="prefetch")


def prefetched(seq, fn, depth=3, wait=None):
    """Yield ``fn(item)`` in order, computing up to ``depth`` items ahead on
    the prefetch worker. Host-side batch prep (memmap page faults,
    stacking, pinning) overlaps the GPU step instead of starving it -- the
    copies release the GIL, so one worker hides nearly all of it, and ONE
    worker is correct: parallel memmap faulting measured SLOWER (1 thread
    3.3 GiB/s, 2-4 threads 1.6-2.5). ``depth`` is the burst buffer -- how
    far a fast prep may run ahead to ride out disk-latency spikes when the
    cache exceeds RAM. ``fn`` must be RNG-free; order is preserved, so
    consumer-side RNG draws are unchanged. ``wait`` (one-element list)
    accumulates time the consumer spent BLOCKED on prep: the loader-bound
    signal. A generator dropped before exhaustion cancels what it queued."""
    futs = collections.deque()
    it = iter(seq)
    try:
        for item in itertools.islice(it, depth):
            futs.append(PREFETCH_POOL.submit(fn, item))
        while futs:
            t0 = time.time()
            out = futs.popleft().result()
            if wait is not None:
                wait[0] += time.time() - t0
            for item in itertools.islice(it, 1):
                futs.append(PREFETCH_POOL.submit(fn, item))
            yield out
    finally:
        for f in futs:
            f.cancel()



class JepaClip:
    """Windows over one clip's V-JEPA latent cache.

    Latents are memory-mapped, never loaded: window slices are copied out
    per access (training/eval batches, tool span forwards), so RAM use is
    independent of corpus size.

    Windows are plain strided spans over the whole timeline; ``self.cut``
    marks the first row of each shot and is fed to the model, which learns
    what a seam means instead of being reset at it -- no footage is
    discarded."""

    def __init__(self, dataset, vid_id, win, stride,
                 feat_dir=FEAT_DIR_DEFAULT, gap_mask=False,
                 hold_gaps_s=0.0,
                 apply_lag=True, masks_dir=None, row_hz=None,
                 speed_clamp=True):
        zpath = Path(dataset) / feat_dir / f"{vid_id}.npz"
        common.check_basis(zpath)   # crash on a refit-basis/stale-cache mix
        self.id = vid_id
        # Rows come off DISK per window, never mapped: the corpus is many
        # times RAM and an epoch is a shuffled single pass, so there is no
        # resident set to keep.
        self.feats = common.RowStream(zpath, "feats")
        # int8 caches (extract.py) store a dequant scale; the
        # multiply happens AFTER the H2D copy so int8 also halves the bus
        with np.load(zpath) as z:
            self.feat_scale = float(z["scale"]) \
                if self.feats.dtype == torch.int8 else 1.0
            times = z["times_ms"]
            # absent stamp = pre-stamp era = cut-aware extraction
            self.cut_blind = bool(z["cut_blind"]) \
                if "cut_blind" in z.files else False
        # the row grid is read off the cache, never assumed: consumers scale
        # by it, and a checkpoint trained on another grid is a hard stop
        self.row_hz = common.row_hz_of(times)
        common.check_row_hz(self.row_hz, row_hz, f"[{vid_id}] {zpath}")
        # per-clip global script<->video lag (jepa_lagfit.py sidecar, fitted
        # OUT OF SAMPLE): ``applied_ms`` is the peak-gated value -- the raw
        # fit when confident + material, else 0. Sidecars are PER GRID
        # (common.lag_dir_name) -- a fit rides its grid's decode frame
        # choices and never carries over -- so clips without a sidecar on
        # THIS grid are unlagged (lag 0).
        # apply_lag=False: fit the RAW (uncorrected) script -- jepa_lagfit
        # MUST measure the true offset, never the residual after its own
        # sidecar (a double-correction feedback loop).
        lf = Path(dataset) / common.lag_dir_name(self.row_hz) \
            / f"{vid_id}.json"
        lag_ms, invert = 0.0, False
        if apply_lag and lf.exists():
            _lag = json.load(open(lf, encoding="utf-8"))
            lag_ms = float(_lag["applied_ms"])
            # ``invert`` is a MANUAL per-clip correction for a script authored
            # with flipped positions (jepa_lagfit alarms on polarity but never
            # sets this). Reflecting position negates velocity.
            invert = bool(_lag.get("invert", False))
        meta = P.load_meta(dataset, vid_id)
        # TRAINING-TARGET conditioning, both stamped on the checkpoint and
        # read by nothing downstream: the harness scores every arm against
        # the script read the one linear, speed-clamped way
        clean = common.sanitize_aligned(
            P.load_script(dataset, vid_id), meta["duration_ms"],
            max_speed=common.MAX_POS_RATE if speed_clamp else None)
        # video clock = script time - lag_ms; consumers that compare raw
        # script timestamps against the grid (jepa_infer's ms-timing
        # readout) need the applied shift
        self.lag_ms = lag_ms
        self.clean = clean
        self.duration_ms = float(meta["duration_ms"])
        self.speed_clamp = bool(speed_clamp)
        vel, pos = common.funscript_velocity(clean, times + lag_ms,
                                             "linear", 1.0)
        self.vel = torch.from_numpy(vel).float()
        self.pos = torch.from_numpy(pos).float()
        self.vel_phase = self.vel
        if invert:
            self.pos = 100.0 - self.pos
            self.vel = -self.vel
            self.vel_phase = -self.vel_phase
        # rows whose targets are real script (not interpolation across an
        # unscripted gap); with gap_mask these are the only rows any loss
        # sees -- gap rows still enter the model as input context
        sc = np.ones(len(times), dtype=bool)
        gaps = common.script_gaps(clean, meta["duration_ms"])
        for g0, g1 in gaps:
            sc[((times + lag_ms) >= g0) & ((times + lag_ms) < g1)] = False
        # ``scripted`` is the EVAL mask (val scoring, v_std, audits) and
        # never moves; ``trainable`` is the loss mask -- with hold_gaps_s
        # it additionally reinstates flat-gap hold rows (hold_rows()).
        # Keeping the two masks separate keeps epoch selection and
        # normalization bit-identical across A/B arms. Unlearnable rows are
        # not masked by data labels: the phase-cap objective absorbs rows
        # the model actively contradicts, per step, in the train loop.
        self.scripted = torch.from_numpy(sc)
        self._clean, self._gap_spans = clean, gaps
        self._times_lag = times + lag_ms
        self.trainable = self.scripted | self.hold_rows(hold_gaps_s)
        self.gap_mask = gap_mask
        bpath = Path(dataset) / "boundaries" / f"{vid_id}.json"
        cuts = json.load(open(bpath, encoding="utf-8"))["cuts_ms"] \
            if bpath.exists() else []
        T = len(times)
        cut_idx = np.searchsorted(times, cuts)
        self.shot_edges = [0] + [int(c) for c in cut_idx if 0 < c < T] + [T]
        self.times_ms = times
        self.win = win
        self.cut = torch.zeros(T)
        self.cut[[c for c in self.shot_edges[1:-1]]] = 1.0
        cand = list(range(0, T - win + 1, stride)) if T >= win else []
        if cand and cand[-1] != T - win:
            cand.append(T - win)
        self.starts = [s for s in cand if self._has_signal(s)]
        # mask rects (masks/<id>.json sidecars, curated outside this
        # tree): human-ACCEPTED banner rects are zeroed out of the model INPUT
        # -- static banners are clip-identity fingerprints the mask net
        # measurably attends. An input transform, not a
        # loss mask: it must follow the CHECKPOINT (a trunk trained
        # with masks is evaluated with them), so callers pass
        # ``masks_dir`` from the checkpoint stamp, never a default.
        # Rects are video-clock (banners live on the video): no lag.
        self._mask_static = None
        self._mask_keep = None
        # a relative masks_dir (the checkpoint stamp) lives inside the project
        mj = Path(dataset) / masks_dir / f"{vid_id}.json" if masks_dir else None
        if mj is not None and mj.exists():
            gh, gw = self.feats.shape[-2:]
            xs = np.arange(gw) / gw
            ys = np.arange(gh) / gh
            static = np.zeros((gh, gw), dtype=bool)
            for r in json.load(open(mj, encoding="utf-8"))["rects"]:
                # "accepted" = human verdict (banners); "auto" = trusted
                # border detection, masks unless a human rejected it
                if r.get("status") not in ("accepted", "auto"):
                    continue
                # a cell is masked when the rect covers >= 20% of it
                ovx = np.clip(np.minimum(r["x1"], xs + 1 / gw)
                              - np.maximum(r["x0"], xs), 0, None) * gw
                ovy = np.clip(np.minimum(r["y1"], ys + 1 / gh)
                              - np.maximum(r["y0"], ys), 0, None) * gh
                # a rect is a PLACE: it masks its cells for the whole clip
                static |= np.outer(ovy, ovx) >= 0.2
            if static.any():
                self._mask_static = torch.from_numpy(static)
                self._mask_keep = torch.from_numpy(~static).to(torch.int8)
                print(f"[{vid_id}] masks: {int(static.sum())} cells zeroed",
                      flush=True)

    @property
    def masked(self):
        return self._mask_static is not None

    @property
    def feat_dim(self):
        """Model-input feature dim: the cache dim."""
        return self.feats.shape[1]

    def fwin(self, a, e):
        """Model-input rows [a:e): the latent cache slice, a fresh writable
        tensor read from disk. ``fwin_into`` is the batched path and skips
        the intermediate."""
        return self.feats[a:e]

    def fwin_into(self, a, e, out):
        """``fwin(a, e)`` written straight into ``out`` -- disk to the
        pinned staging buffer in one copy."""
        return self.feats.read_into(a, e, out)

    def mask_feats(self, x):
        """Zero the accepted mask-rect cells in a feature slice. A rect
        masks its cells for the whole clip, so the slice's own start row
        says nothing here. ``x`` is (..., W, dim, gh, gw) and is mutated
        in place; returns it. No-op when the clip carries no accepted
        rects. Integer slices multiply by the precomputed keep grid --
        one vectorized pass instead of a scatter, exact on int8; float
        slices keep the scatter (a negative float times +0 is IEEE
        -0.0, a byte the scatter never writes)."""
        ints = not torch.is_floating_point(x)
        if self._mask_static is not None:
            if ints:
                x.mul_(self._mask_keep)
            else:
                x[..., self._mask_static] = 0
        return x

    def hold_rows(self, hold_gaps_s):
        """Rows of short FLAT interior script gaps, as a mask separate
        from ``scripted``: two flanking actions at the same position with
        nothing between them is how scripters write a hold (user-verified
        on the gate pair), so these rows carry real stillness labels.
        Callers choose which losses see them -- only the envelope
        refit's (jepa_refit.py); trunk-level reinstatement is REJECTED
        (breaks the gate). Window eligibility (``starts``) never depends
        on this mask unless it was folded into ``trainable`` at
        construction (hold_gaps_s > 0)."""
        hr = np.zeros(len(self._times_lag), dtype=bool)
        if hold_gaps_s > 0 and self._clean:
            for g0, g1 in self._gap_spans:
                if g0 < self._clean[0][0] or g1 > self._clean[-1][0]:
                    continue                       # head/tail: unknowable
                if g1 - g0 > hold_gaps_s * 1000.0:
                    continue
                p0, p1 = common.funscript_position(self._clean, [g0, g1])
                if abs(p1 - p0) <= 5.0:
                    hr[(self._times_lag >= g0)
                       & (self._times_lag < g1)] = True
        return torch.from_numpy(hr)

    def _has_signal(self, s):
        """A window is trainable if its supervised rows carry variance;
        with gap_mask, rows inside unscripted gaps don't count."""
        v = self.vel[s:s + self.win]
        if self.gap_mask:
            sc = self.trainable[s:s + self.win]
            # a single supervised row carries no variance (and std of one
            # sample is NaN, which would read as no-signal anyway)
            return int(sc.sum()) > 1 and float(v[sc].std()) > 1e-3
        return float(v.std()) > 1e-3

    def __len__(self):
        return len(self.starts)

    def __getitem__(self, i):
        s = self.starts[i]
        f = self.fwin(s, s + self.win)
        if self.masked:                 # fwin already owns its rows
            f = self.mask_feats(f)
        return f, self.vel[s:s + self.win]


class JepaModel(nn.Module):
    """Mask net + attention pooling + dilated TCN over V-JEPA latents."""

    def __init__(self, dim=128, heads=8, tcn_ch=256, dropout=0.4,
                 vel_bins=BINS_DEFAULT, gen_env=True, env_ctx=8, env_bins=21,
                 env_k=9,
                 pos_head=True, pos_bins=21, ext_head=False, plat_head=False,
                 plat_mlp=False, plat_mlp_ch=64, rev_head=False,
                 cut_flag=True, coord=True,
                 env_flow=False, env_flow_steps=2,
                 mask_ch=(48, 32),
                 dilations=(1, 2, 4, 8, 16, 32, 64, 128)):
        super().__init__()
        self.heads = heads
        self.cut_flag = cut_flag
        # coord: the mask net sees WHERE each cell sits (two coordinate
        # channels), and the trunk is fed each head's gate mass and
        # attention centroid -- the centroid trajectory is otherwise
        # computed for the pooling weights and discarded, and it is
        # plausibly the most level-relevant signal the frontend holds
        self.coord = coord
        mask_ch = tuple(int(c) for c in mask_ch)
        layers, c_in = [], dim + (2 if coord else 0)
        for c in mask_ch:
            layers += [nn.Conv2d(c_in, c, 3, padding=1), nn.GELU()]
            c_in = c
        layers.append(nn.Conv2d(c_in, heads, 1))
        self.mask = nn.Sequential(*layers)
        self.inp = nn.Conv1d(heads * dim + (3 * heads if coord else 0)
                             + int(cut_flag), tcn_ch, 1)
        drop = nn.Dropout
        self.blocks = nn.ModuleList(nn.Sequential(
            nn.Conv1d(tcn_ch, tcn_ch, 3, padding=d, dilation=d), nn.GELU(),
            drop(dropout),
            nn.Conv1d(tcn_ch, tcn_ch, 3, padding=d, dilation=d), nn.GELU(),
            drop(dropout)) for d in dilations)
        self.vel_bins = vel_bins
        self.head_v = nn.Conv1d(tcn_ch, vel_bins, 1)
        self.gen_env = gen_env
        # the flow readout is a REFIT arch (U6 is one refit, no
        # training): jepa_refit sets it and stamps it, and a checkpoint
        # carrying it reconstructs here. jepa_train has no flag for it
        # because its two-view loss path does not fit it
        self.env_flow = bool(env_flow) and bool(gen_env)
        self.env_flow_steps = int(env_flow_steps)
        # the decode's position on the ABSOLUTE row clock, and the
        # authorship seed. jepa_infer sets env_row0 per chunk; the
        # default 0 is correct for a window that starts at row 0 and for
        # every training path, which never samples
        self.env_row0 = 0
        self.env_rows = None       # the decode's reach; None = the window
        self.env_seed = common.ENV_DRAW_SEED
        self.env_ctx = env_ctx
        self.env_k = env_k       # envelope box width on the TRAINED grid
        self.env_bins = env_bins
        if gen_env:
            self.env_emb = nn.Conv1d(env_ctx, 32, 1)
            self.head_e = nn.Sequential(
                nn.Conv1d(tcn_ch + 32, 64, 1), nn.GELU(),
                nn.Conv1d(64, env_bins, 1))
            self.register_buffer("env_centers",
                                 torch.linspace(0.0, 6.0, env_bins))
            # the FLOW readout: same conditioning z, a velocity field
            # v(e, tau, z) in place of 21 logits collapsed to their mean.
            # Two extra input channels are the current iterate and the
            # path position; one output channel is the field
            if self.env_flow:
                self.head_ef = nn.Sequential(
                    nn.Conv1d(tcn_ch + 32 + 2, 64, 1), nn.GELU(),
                    nn.Conv1d(64, 1, 1))
        self.pos_head = pos_head
        self.pos_bins = pos_bins
        if pos_head:
            self.head_p = nn.Conv1d(tcn_ch, pos_bins, 1)
            self.register_buffer("pos_centers",
                                 torch.linspace(0.0, 100.0, pos_bins))
        self.head_conf = nn.Conv1d(tcn_ch, 1, 1)
        self.register_buffer("bin_centers",
                             torch.linspace(-6.0, 6.0, vel_bins))
        self.ext_head = ext_head
        if ext_head:      # band-extent probes, grafted post-training by
            # jepa_refit.py -- never trained through the trunk
            self.head_xlo = nn.Conv1d(tcn_ch, pos_bins, 1)
            self.head_xhi = nn.Conv1d(tcn_ch, pos_bins, 1)
            self.register_buffer("ext_centers",
                                 torch.linspace(0.0, 100.0, pos_bins))
        self.plat_head = plat_head
        if plat_head:     # trapezoid-dwell classes (none/top/bottom):
            # P(row inside a >=0.5 s script dwell at an extreme). The phase
            # head alone rides straight through long dwells (|vmarg| ~46
            # pos/s inside deleted ones), so composed styling pins the
            # predicted runs' LEVEL to the band rail (jepa_infer's level
            # lock) and keeps their ripple. Defined LAST so a same-seed run
            # without it draws identical init for everything else.
            # plat_mlp: nonlinear readout -- the 1x1 conv is a LINEAR
            # probe of the frozen features, and linear calibration moves
            # (label smear, corner loss weight) only slide the head's
            # MAE/dwell frontier without shifting it.
            self.head_plat = nn.Sequential(
                nn.Conv1d(tcn_ch, plat_mlp_ch, 1), nn.GELU(),
                nn.Conv1d(plat_mlp_ch, 3, 1)) if plat_mlp \
                else nn.Conv1d(tcn_ch, 3, 1)
        self.rev_head = rev_head
        if rev_head:      # reversal-event classes (none/peak/valley):
            # P(script reversal at this frame). Refit-only (jepa_refit
            # pattern -- never trained through the trunk). The marginal's
            # zero crossings localize SLOW reversals 2-5 frames off (a
            # shallow crossing wanders under vmarg noise); composed
            # styling's --rev-snap moves each crossing to this head's
            # local argmax, keeping the marginal's stroke structure
            # (common.reversal_labels is the one label definition).
            self.head_rev = nn.Sequential(
                nn.Conv1d(tcn_ch, 64, 1), nn.GELU(),
                nn.Conv1d(64, 3, 1))

    def env_flow_field(self, z, e, tau):
        """The velocity field v(e, tau, z) at one path point. ``z`` is the
        envelope step's own conditioning (B, tcn_ch+32, W), ``e`` the
        current iterate and ``tau`` the path position, both (B, W)."""
        return self.head_ef(torch.cat(
            [z, e.unsqueeze(1), tau.unsqueeze(1)], dim=1))[:, 0]

    def env_flow_sample(self, z, e0):
        """Integrate the field from the base draw ``e0`` to a sample, in
        ``env_flow_steps`` equal Euler steps. A FIXED step count, so the
        loop unrolls and ``env_step`` stays one traced graph -- the
        property that separates this from a diffusion head. Deterministic
        given ``e0``, which ``common.env_base_draw`` makes a pure function
        of the absolute row, so both decoders integrate the same ODE."""
        n = max(1, self.env_flow_steps)
        e = e0
        for i in range(n):
            tau = torch.full_like(e, i / n)
            e = e + self.env_flow_field(z, e, tau) / n
        return e.clamp(0.0, common.ENV_BASE_HI)

    def _expect(self, logits, centers=None):
        c = self.bin_centers if centers is None else centers
        return (logits.softmax(1) * c.view(1, -1, 1)).sum(1)

    @staticmethod
    def _prev_stack(seq, k, w):
        """(B,W) -> (B,k,W) where channel j at time t holds seq[t-1-j]
        (zeros before the start)."""
        pad = F.pad(seq, (k, 0))
        return torch.stack([pad[:, k - 1 - j:k - 1 - j + w]
                            for j in range(k)], dim=1)

    def frontend(self, x, cut=None):
        """Mask net -> attention pooling -> input projection.

        DETERMINISTIC by construction: dropout lives only in the TCN
        blocks, so this stage is a pure function of the latents. It is
        also the EXPENSIVE half -- the mask net runs a conv stack over
        B*W separate 24x24 grids, ~5x the trunk's multiplies -- which is
        why two R-Drop views share it instead of recomputing it (see
        ``forward``'s ``front``)."""
        b, w, d, gh, gw = x.shape
        f = x.reshape(b * w, d, gh, gw)
        if self.coord:
            ys = torch.linspace(-1.0, 1.0, gh, device=f.device,
                                dtype=f.dtype)
            xs = torch.linspace(-1.0, 1.0, gw, device=f.device,
                                dtype=f.dtype)
            cg = torch.stack(torch.meshgrid(ys, xs, indexing="ij"))
            gate = torch.sigmoid(self.mask(
                torch.cat([f, cg.expand(b * w, 2, gh, gw)], dim=1)))
        else:
            gate = torch.sigmoid(self.mask(f))
        att = gate / (gate.sum(dim=(2, 3), keepdim=True) + 1e-6)
        pooled = torch.einsum("bhij,bdij->bhd", att, f)  # (BW,H,dim)
        seq = pooled.reshape(b, w, -1).transpose(1, 2)
        if self.coord:
            # per head: raw gate mass (how much attends at all) and the
            # attention centroid (where) -- the weights the pooling just
            # used, handed to the trunk instead of discarded
            mass = gate.mean(dim=(2, 3))                       # (BW,H)
            cy = torch.einsum("bhij,i->bh", att, ys)
            cx = torch.einsum("bhij,j->bh", att, xs)
            seq = torch.cat(
                [seq, torch.cat([mass, cy, cx], dim=1)
                 .reshape(b, w, -1).transpose(1, 2)], dim=1)
        if self.cut_flag:
            if cut is None:
                cut = seq.new_zeros(b, w)
            seq = torch.cat([seq, cut.to(seq.dtype).unsqueeze(1)], dim=1)
        return self.inp(seq), gate.reshape(b, w, self.heads, gh, gw)

    def trunk(self, h):
        """Dilated TCN, the model's ONLY stochastic stage."""
        for blk in self.blocks:
            h = h + blk(h)
        return h

    def trunk_taps(self, h0, idx):
        """The final features beside the residual stream after each
        block in ``idx`` (block positions in the dilation ladder), for a
        head that reads a shorter temporal context than the last block
        carries: after block 2 the stream has seen 29 rows, after block
        4 it has seen 125, where the final features have seen 4093."""
        want = set(int(i) for i in idx)
        taps, h = [], h0
        for i, blk in enumerate(self.blocks):
            h = h + blk(h)
            if i in want:
                taps.append(h)
        return h, taps

    def trunk_final(self, h0):
        """The trunk features every head reads."""
        return self.trunk(h0)

    def features(self, x, cut=None):
        """Trunk only (mask net -> attention pooling -> TCN): (B,W,dim,
        24,24) -> (B,C,W) features + the attention gate. jepa_refit
        trains post-hoc heads on these without paying the generative
        decode."""
        h0, gate = self.frontend(x, cut)
        return self.trunk_final(h0), gate

    def _decode_env(self, h):
        """The generated amplitude envelope over the window: the AR decode
        of the envelope head on the trunk features ``h`` (B,C,W), one
        row at a time through the context buffer, (B,W). The buffer
        carries the BASE decode: the gain sits OUTSIDE the recurrence,
        scaling only the published value. A gained buffer re-inflates
        its own next step and the track compounds toward span**k, not
        gain (measured: free-run slopes ~2x on every band)."""
        b, w = h.shape[0], h.shape[2]
        # ``env_rows`` is the decode's reach inside the window: a decode
        # chunk carries a trailing context the TCN reads and the output
        # cuts, and the recurrence is causal, so no kept row depends on
        # a row past the kept slice. Rows past the reach read 0.
        n = w if getattr(self, "env_rows", None) is None \
            else max(0, min(w, int(self.env_rows)))
        # the base draw is a pure function of the ABSOLUTE row, so a
        # decode chunk that reseeds the AR buffer does not reseed this
        # and goblinscript integrates the same ODE without exchanging
        # state. Drawn for the window at once, in the double the mixer
        # yields, and rounded to the step's dtype at the step
        e0 = torch.as_tensor(common.env_base_draw(
            np.arange(self.env_row0, self.env_row0 + n), self.env_seed),
            device=h.device) if self.env_flow else None
        if h.is_cuda and not torch.is_grad_enabled() \
                and not torch.is_autocast_enabled():
            return self._decode_env_graphed(h, n, e0)
        buf = h.new_zeros(b, self.env_ctx)
        es = h.new_zeros(b, w)
        for t in range(n):
            et, buf = self._env_step(
                h[:, :, t:t + 1],
                e0[t:t + 1].expand(b, 1) if e0 is not None else None,
                buf)
            es[:, t] = et
        return es

    def _env_step(self, h_t, e0, buf):
        """One row of the envelope recurrence: the features ``h_t``
        (B,C,1), the row's base draw ``e0`` (B,1), the context ``buf``
        (B,env_ctx) -> the row's envelope (B,) and the shifted context.
        This body IS the recurrence, so the graphed loop and the eager
        one run the same kernels on the same shapes."""
        z = torch.cat([h_t, self.env_emb(buf.unsqueeze(-1))], dim=1)
        if self.env_flow:
            et = self.env_flow_sample(z, e0.to(z.dtype))[:, 0]
        else:
            # the expectation over the bins hedges upward on a quiet row
            # by the mass label smoothing leaves above a low mode, as the
            # level's did before pos_temp; env_temp sharpens it the same
            # way (1 = the plain expectation)
            et = self._expect(self.head_e(z) / getattr(self, "env_temp", 1.0),
                              self.env_centers)[:, 0]
        buf = torch.cat([et.unsqueeze(1), buf[:, :-1]], dim=1)
        return et, buf

    def _decode_env_graphed(self, h, n, e0):
        """The recurrence as ONE captured CUDA graph replayed per row.
        The eager loop launches every kernel of a row step from Python,
        which is the whole cost of a decode: the GPU idles between
        launches of work that takes microseconds. The graph holds the
        step's kernels on static buffers -- the window's features, its
        base draws, the context and a row counter the step advances
        itself -- so a row costs one replay and the numbers are the
        eager loop's own, because the kernels and the shapes are. The
        capture is keyed on the shapes and the head modules and lives
        on the model; a wider window or a replaced head recaptures."""
        b, c, w = h.shape
        mods = (self.env_emb,
                self.head_ef if self.env_flow else self.head_e)
        key = (b, c, h.dtype, h.device, self.env_flow)
        st = getattr(self, "_env_graph", None)
        if st is None or st["key"] != key or st["cap"] < w \
                or any(a is not m for a, m in zip(st["mods"], mods)):
            st = self._env_graph_capture(key, mods, b, c, w, h)
            self._env_graph = st
        st["h"][:, :, :w].copy_(h)
        if e0 is not None:
            st["e0"][:n].copy_(e0)
        st["buf"].zero_()
        st["es"].zero_()
        st["ti"].zero_()
        for _ in range(n):
            st["graph"].replay()
        return st["es"][:, :w].clone()

    def _env_graph_capture(self, key, mods, b, c, cap, like):
        dev = like.device
        st = dict(key=key, mods=mods, cap=cap,
                  h=torch.zeros(b, c, cap, dtype=like.dtype, device=dev),
                  e0=torch.zeros(cap, dtype=torch.float64, device=dev),
                  buf=torch.zeros(b, self.env_ctx, dtype=like.dtype,
                                  device=dev),
                  es=torch.zeros(b, cap, dtype=like.dtype, device=dev),
                  ti=torch.zeros(1, dtype=torch.long, device=dev))

        def step():
            ti = st["ti"]
            h_t = st["h"].index_select(2, ti)
            e0 = st["e0"].index_select(0, ti).expand(b, 1) \
                if self.env_flow else None
            et, nb = self._env_step(h_t, e0, st["buf"])
            st["buf"].copy_(nb)
            st["es"].index_copy_(1, ti, et.unsqueeze(1))
            ti.add_(1)

        # the capture recipe: a few eager steps on a side stream settle
        # the allocator and the conv plans, then the step is recorded
        s = torch.cuda.Stream(dev)
        s.wait_stream(torch.cuda.current_stream(dev))
        with torch.cuda.stream(s):
            for _ in range(3):
                step()
        torch.cuda.current_stream(dev).wait_stream(s)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            step()
        st["graph"] = graph
        return st

    def forward(self, x=None, teacher_env=None, cut=None, front=None):
        """(B,W,dim,24,24) -> velocity, confidence, attention gate.

        ``front``: an (h0, gate) pair from an earlier ``frontend()`` call.
        R-Drop's second view hands it back and pays the TCN alone -- the
        frontend is deterministic, so a shared one is bit-for-bit what a
        recomputed one would have been, not an approximation of it.
        """
        if front is None:
            front = self.frontend(x, cut)
        h0, gate = front
        h = self.trunk_final(h0)
        self.h = h                    # the features every head read
        w = h.shape[2]
        self.logits = self.head_v(h)
        v = self._expect(self.logits)
        if self.gen_env and teacher_env is not None:    # forced env head
            prev = self._prev_stack(teacher_env, self.env_ctx, w)
            self.env_logits = self.head_e(
                torch.cat([h, self.env_emb(prev)], dim=1))
        elif self.gen_env:            # decode env, rescale the carrier
            eg = self._decode_env(h)
            self.v_marginal = v       # phase track, pre-recombination
            self.eg = eg              # generated amplitude envelope
            u = (v / (envelope(v, self.env_k) + 0.05)).clamp(-3, 3)
            v = u * eg                # zero crossings of v are untouched
        if self.pos_head:
            self.pos_logits = self.head_p(h)
            # pos_temp < 1 sharpens the decode toward the mode (eval knob:
            # expectation decode compresses the level's extremes ~0.5x)
            self.level = self._expect(
                self.pos_logits / getattr(self, "pos_temp", 1.0),
                self.pos_centers)
        if self.ext_head:
            self.ext_logits_lo = self.head_xlo(h)
            self.ext_logits_hi = self.head_xhi(h)
            t = getattr(self, "pos_temp", 1.0)
            self.ext_lo = self._expect(self.ext_logits_lo / t,
                                       self.ext_centers)
            self.ext_hi = self._expect(self.ext_logits_hi / t,
                                       self.ext_centers)
        if self.plat_head:
            self.plat_logits = self.head_plat(h)
        if self.rev_head:
            self.rev_logits = self.head_rev(h)
        conf = torch.sigmoid(self.head_conf(h)).squeeze(1)
        return v, conf, gate


def _model_from_ck(ck, device):
    import inspect
    known = inspect.signature(JepaModel.__init__).parameters
    arch = {k: v for k, v in ck["arch"].items() if k in known}
    model = JepaModel(**arch).to(device).eval()
    model.load_state_dict(ck["model"])
    return model


def load_model(ckpt, device="cpu"):
    """Checkpoint path -> (eval model on device, checkpoint dict). Arch keys
    from retired experiment configs are ignored, so every recorded
    checkpoint loads."""
    ck = torch.load(ckpt, map_location="cpu", weights_only=False)
    if "members" in ck:
        raise SystemExit(f"{ckpt} is an ensemble of {len(ck['members'])} "
                         f"trunks; this tree trains and reads one trunk")
    bid = ck.get("basis_id")
    cur = common.basis_id_of(common.BASIS_PATH)
    if bid and cur and bid != cur:
        raise SystemExit(
            f"checkpoint {ckpt} was trained on basis {bid}, but the frozen "
            f"basis is {cur} -- its features do not exist here")
    if not bid:
        print(f"note: {ckpt} carries no basis_id (predates stamping)",
              flush=True)
    # Location stamps from before the project layout name directories that
    # no longer exist; every consumer reads the project's fixed ones.
    ck["feat_dir"] = FEAT_DIR_DEFAULT
    if ck.get("masks_dir"):
        ck["masks_dir"] = common.MASKS_DIR
    for k in ("dataset", "dataset2", "feat_dir2", "feat_dim", "row_stride"):
        ck.pop(k, None)
    return _model_from_ck(ck, device), ck


EVAL_CTX_S = 12.8                           # warm-up context each eval chunk
                                            # carries before its scored rows
# The held-out geometry (common.val_regions and its durations) lives in
# common: training and every scorer read ONE definition of val, so a metric
# can never describe rows the trunk was fitted on.


def seg_split(clip, val_frac, win, margin=None):
    """Cut-window mode split -> (train window idx, val segments, sup_mask).

    The held-out ROWS are ``common.val_regions`` -- the one definition every
    scorer reads, so no metric can describe rows this trunk was fitted on,
    and the per-epoch ranking measures the same rows the audit does.
    Loss-masking replaces window discards: EVERY window trains, but rows
    inside a val region (plus a ``margin`` guarding against target
    autocorrelation across the boundary) supervise nothing."""
    T = clip.feats.shape[0]
    margin = common.rows_at(common.SEG_MARGIN_S, clip.row_hz) \
        if margin is None else margin
    segs = common.val_regions(T, clip.row_hz, val_frac)
    sup = torch.ones(T, dtype=torch.bool)
    for lo, hi in segs:
        sup[max(0, lo - margin):min(T, hi + margin)] = False
    eff = sup & clip.trainable if clip.gap_mask else sup
    tr = []
    for i, s in enumerate(clip.starts):
        m = eff[s:s + win]
        if int(m.sum()) > 1 and clip.vel[s:s + win][m].std() > 1e-3:
            tr.append(i)
    return tr, segs, sup


# The release trunk's fixed configuration: architecture, regularization and
# the loss weights every shipped trunk was trained with. A recipe sets the
# schedule (epochs, window, stride, batch, learning rate, seed); these it
# does not.
MASK_HEADS = 8
TCN_CH = 256
DROPOUT = 0.4
DILATIONS = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)
MASK_CH = (48, 32)
ENV_CTX = 16
PHASE_CAP = 3.72
PHASE_CAP_HALF = 2
PHASE_CAP_WARM = 0
POS_W = 0.15
ENV_W = 0.25
ENV_NOISE = 0.25
ENV_DROP = 0.1
RDROP = 2.0
PATCH_DROP = 0.2
MASK_DROPOUT = 0.25
LATENT_NOISE = 0.2
COTRAIN_HOLD_W = 3.0
COTRAIN_QUIET_THR = 0.3
COTRAIN_LOUD_W = 3.0
COTRAIN_LOUD_THR = 1.0
COTRAIN_HOLD_GAPS = 5.0
GATE_IDS = ["gate"]


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default="dataset_v2",
                    help="the project directory")
    ap.add_argument("--ids", nargs="*", default=IDS_DEFAULT)
    ap.add_argument("--epochs", type=int, default=30,
                    help="training epochs; also the cosine-LR horizon, so "
                         "schedule length is part of any baseline. Default "
                         "30; 60 under --screen (screen numbers compare "
                         "only against the 60-epoch screen baseline)")
    # window geometry is a DURATION on the row grid: 1536 rows is ~51 s at
    # common.ROW_HZ, and the stride is half of it
    ap.add_argument("--keep-pareto", action="store_true",
                    help="also retain the best PHASE-TAIL (worst-2 clip "
                         "corr), best SLOW-BAND and best LEVEL epochs as "
                         "jepa_best_tail.pt / jepa_best_slow.pt / "
                         "jepa_best_level.pt. Selection is pooled phase "
                         "alone today while the deploy heads are refit "
                         "after selection, so the epoch with the best "
                         "frozen features for the PRODUCT is not "
                         "necessarily the one that ships -- measured at 0.4 "
                         "ms of reversal timing plus stillness, band recall "
                         "and the spike guard on one arm. Level is kept "
                         "because the tail criterion COSTS it every time it "
                         "wins. Reversal timing has no criterion here by "
                         "construction: the rev head is refit-only and does "
                         "not exist while the trunk trains. Costs three "
                         "checkpoint copies; the OOS deploy read decides "
                         "between them and nothing is promoted by keeping "
                         "them")
    ap.add_argument("--win", type=int, default=1536)
    ap.add_argument("--stride", type=int, default=768)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--cap-two-view", action="store_true",
                    help="absorb a row only when BOTH R-Drop views find it "
                         "contradictory, instead of each view testing "
                         "alone. The per-view test spends one dropout draw "
                         "to decide a row is unlearnable, so a row the "
                         "model is merely UNSURE about reads the same as a "
                         "contradictory label; agreement separates them. "
                         "Needs --rdrop > 0 and costs nothing extra -- the "
                         "second view is already drawn. The R-Drop KL "
                         "already restricts itself to rows both views "
                         "keep; this extends that rule to the CE")
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--patience", type=int, default=8,
                    help="stop after this many epochs without a mean0 "
                         "improvement. Every run pays this in full -- the "
                         "best checkpoint is always followed by exactly "
                         "this many dead epochs -- so it is a flat tax, not "
                         "an occasional one. Across 32 recorded runs the "
                         "longest dry spell a later improvement ever broke "
                         "was 6 (28 of 32 never exceeded 3), and an "
                         "improvement is scored BEFORE this check, so 6 is "
                         "the floor that truncates nothing on record; 8 "
                         "keeps 2 epochs of margin over it")
    ap.add_argument("--seed", type=int, default=888)
    ap.add_argument("--init-from", default=None, metavar="CKPT",
                    help="start from this trunk checkpoint's weights instead "
                         "of a random init: the trunk fine-tuning path. Same "
                         "basis and row grid required (asserted); the "
                         "optimizer, schedule, epochs and validation split "
                         "are this run's own, not the source's.")
    ap.add_argument("--runs-dir", default="runs/jepa")
    ap.add_argument("--resume", action="store_true",
                    help="continue a killed/OOM'd run from its per-epoch "
                         "resume_state.pt (weights + AdamW + cosine-LR + RNG "
                         "+ patience state). Config must match (asserted). "
                         "A resumed run byte-matches an uninterrupted one.")
    return ap


def run(args):
    """Train under a parsed ``build_parser()`` namespace."""
    common.cap_working_set()    # memmap streaming must not eat the box
    args.ids = common.resolve_ids(args.ids, args.dataset)   # rosters by NAME
    # the gate clips are a readout, not an input: a project without that
    # roster trains the same
    gate_ids = [g for spec in GATE_IDS
                     for g in (common.load_roster(args.dataset, spec)
                               if not str(spec).isdigit() else [spec]) or []]
    # the training clips by script content, for eval's seen/unseen test:
    # clip IDs mean nothing outside their project
    trained_sigs = sorted(set(common.clip_sigs(args.dataset,
                                               args.ids).values()))

    device = "cuda" if torch.cuda.is_available() else "cpu"
    runs = Path(args.runs_dir)
    runs.mkdir(parents=True, exist_ok=True)
    # the run's own configuration, beside its checkpoint: an arm that cannot
    # be reproduced from disk is not an experiment
    (runs / "args.json").write_text(
        json.dumps(vars(args), indent=1, sort_keys=True, default=str),
        encoding="utf-8")
    resume_path = runs / "resume_state.pt"
    resuming = args.resume and resume_path.exists()
    if args.resume and not resume_path.exists():
        print(f"--resume: no {resume_path}, starting fresh", flush=True)
    clips = [JepaClip(args.dataset, v, args.win, args.stride,
                      feat_dir=FEAT_DIR_DEFAULT, gap_mask=True,
                      masks_dir=common.MASKS_DIR)
             for v in args.ids]
    # one run, one row grid: mixing rates would give the same window
    # different durations per clip and every row-indexed constant two meanings
    rates = {c.id: c.row_hz for c in clips}
    row_hz = clips[0].row_hz
    if any(abs(r - row_hz) > 0.01 * row_hz for r in rates.values()):
        raise SystemExit("clips do not share one row grid: "
                         + ", ".join(f"{k} {v:g}/s" for k, v in rates.items()))
    print(f"row grid: {row_hz:g} rows/s ({1000.0 / row_hz:.1f} ms spacing)",
          flush=True)
    for c in clips:
        un = 1.0 - c.scripted.float().mean().item()
        if un > 0.005:
            print(f"[{c.id}] gap-mask: {un*100:.1f}% of rows unscripted "
                  "-> no loss there", flush=True)
    seg_splits = [seg_split(c, args.val_frac, args.win) for c in clips]
    splits = []          # va = fully-inside-val windows (heatmap viz only;
    for c, (tr, segs, sup) in zip(clips, seg_splits):       # eval is by seg)
        va = [i for i, s in enumerate(c.starts)
              if not sup[s:s + args.win].any()]
        if not va and c.starts:
            # A window longer than a val segment cannot sit fully inside
            # one, so the viz falls back to the LEAST supervised windows:
            # the same picture, drawn on the rows the trunk saw least.
            va = sorted(range(len(c.starts)),
                        key=lambda i: int(sup[c.starts[i]:
                                              c.starts[i] + args.win].sum()))
        splits.append((tr, va))
        print(f"[{c.id}] {len(tr)} train windows / {len(segs)} val segs "
              f"({sum(hi - lo for lo, hi in segs)} rows)", flush=True)
    if not any(tr for tr, _ in splits):
        raise SystemExit(f"no training windows: no clip gives a {args.win}-"
                         f"row window with supervised motion")
    # effective per-row supervision mask: seg-split val exclusion AND (with
    # gap-mask) script coverage
    sup_eff = []
    # the DEPLOY heads' supervision mask: script coverage only, never the
    # reinstated hold rows. dwell_labels and reversal_labels are both read
    # off ``scripted``, so a gap-interpolated row carries a label the label
    # definition declined to assert; the rails and the envelope are read
    # off the same script. This is the mask jepa_refit's CLI supervises
    # them on, and one recipe cannot mean two label sets.
    sup_head = []
    for ci, c in enumerate(clips):
        sup = seg_splits[ci][2].clone()
        sup_head.append(sup & c.scripted)
        sup &= c.trainable
        sup_eff.append(sup)
    v_std = float(torch.cat([c.vel[c.scripted]
                             for c in clips]).std().clamp_min(1e-3))
    f_scale = clips[0].feat_scale
    if any(c.feat_scale != f_scale for c in clips):
        raise SystemExit("mixed latent cache formats across --ids")
    if any(c.cut_blind != clips[0].cut_blind for c in clips):
        raise SystemExit("mixed extraction windowing (cut_blind stamp) "
                         "across --ids -- cut-aware and cut-blind caches "
                         "do not mix in one run")
    torch.manual_seed(args.seed)   # model init + dropout; without this only
    # data order was seeded and every "same-seed" run drew a fresh init
    dim = clips[0].feat_dim
    model = JepaModel(dim=dim, heads=MASK_HEADS, tcn_ch=TCN_CH,
                      dropout=DROPOUT, vel_bins=BINS_DEFAULT,
                      env_ctx=ENV_CTX,
                      env_k=common.rows_at(ENV_SMOOTH_S, row_hz, odd=True),
                      mask_ch=MASK_CH,
                      dilations=DILATIONS).to(device)
    if args.init_from:
        # trunk fine-tuning: adjust a released trunk instead of earning a
        # fresh one. load_model enforces the basis and refuses ensembles;
        # a strict load refuses an architecture drift.
        _, ick = load_model(args.init_from, "cpu")
        ck_hz = ick.get("row_hz")
        if ck_hz and abs(ck_hz - row_hz) > 0.01 * row_hz:
            raise SystemExit(
                f"--init-from: {args.init_from} was trained at {ck_hz:g} "
                f"rows/s, this project runs {row_hz:g}")
        model.load_state_dict(ick["model"])
        # add the training clips of the source trunk
        trained_sigs = sorted(set(trained_sigs)
                              | set(ick.get("trained_sigs", ())))
        print(f"trunk init from {Path(args.init_from).name} "
              f"(epoch {ick.get('epoch')})", flush=True)
    for c in clips:      # ~2 s level target (style-adjacent, low-pass)
        k = common.level_pool_rows(c.times_ms)
        # taken inside each shot under --cut-aware-level: run across a
        # cut this window mixes two scenes for a second either side, so
        # the target RAMPS where the scene steps and the head learns
        # the ramp. Refitting the head against a stepped target on a
        # trunk co-trained against a ramped one does not recover it
        # (measured: step slope 0.03 -> 0.05 at a 28% seam loss
        # weight), which is why the switch belongs here too.
        c.level = F.avg_pool1d(c.pos[None], k, stride=1, padding=k // 2,
                               count_include_pad=False)[0]
    pos_weight = None
    parts = []
    for ci, (c, (trn, _)) in enumerate(zip(clips, splits)):
        for i in trn:
            s = c.starts[i]
            lv = c.level[s:s + args.win]
            if sup_eff is not None:         # supervised rows only
                lv = lv[sup_eff[ci][s:s + args.win]]
            parts.append(lv)
    lv_tr = torch.cat(parts)
    idx = (lv_tr / 100.0 * (model.pos_bins - 1)) \
        .clamp(0, model.pos_bins - 1).round().long()
    freq = torch.bincount(idx, minlength=model.pos_bins).float() \
        / max(len(idx), 1)
    pos_weight = 1.0 / (freq + 1e-4).sqrt()
    pos_weight = (pos_weight / pos_weight.mean()).to(device)
    print(f"model {sum(p.numel() for p in model.parameters())/1e3:.0f}k "
          f"params, target std {v_std:.1f}", flush=True)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, args.epochs)
    gen = torch.Generator().manual_seed(args.seed)
    # the latent-noise draws live on their OWN device generator --
    # created only when LATENT_NOISE > 0, so a noise-0 run never touches
    # it and stays byte-identical, and the global CUDA stream (TCN
    # dropout) is untouched either way
    noise_gen = None
    if LATENT_NOISE > 0:
        noise_gen = torch.Generator(device=device)
        noise_gen.manual_seed(args.seed + 7)
    items = [(ci, i) for ci, (tr, _) in enumerate(splits) for i in tr]
    sup_env = [sup_eff[ci]
               | (seg_splits[ci][2]
                  & c.hold_rows(COTRAIN_HOLD_GAPS))
               for ci, c in enumerate(clips)]
    run_bid = common.basis_id_of(common.BASIS_PATH)   # checkpoint provenance
    best, best_epoch, best_corrs = -np.inf, 0, {}
    best_of = {}          # one running best per retention criterion
    # Resume state surface: weights + AdamW moments + cosine-LR
    # position + the window/env Generator + global CPU & CUDA RNG (dropout /
    # R-Drop draw from CUDA RNG -- set_rng_state_all, NOT manual_seed) +
    # patience bookkeeping. A config fingerprint refuses a mismatched resume.
    fingerprint = {"seed": args.seed, "ids": list(args.ids),
                   "epochs": args.epochs, "win": args.win,
                   "stride": args.stride, "batch": args.batch,
                   "lr": args.lr, "dropout": DROPOUT, "heads": MASK_HEADS,
                   "tcn_ch": TCN_CH, "rdrop": RDROP,
                   "phase_cap": PHASE_CAP,
                   "phase_cap_half": PHASE_CAP_HALF,
                   "latent_noise": LATENT_NOISE,
                   "gen_env": True, "env_cotrain": True}
    if PHASE_CAP_WARM > 0 or args.cap_two_view:
        fingerprint["cap_shape"] = {"warm": PHASE_CAP_WARM,
                                    "two_view": args.cap_two_view}
    if args.init_from:
        fingerprint["init_from"] = Path(args.init_from).name
    start_epoch = 1
    if resuming:
        # no map_location: tensors return to their saved devices -- model &
        # AdamW moments on cuda, RNG ByteTensors on CPU (gen.set_state /
        # set_rng_state_all require CPU state; map_location=cuda breaks them)
        st = torch.load(resume_path, weights_only=False)
        if st.get("fingerprint") != fingerprint:
            raise SystemExit(
                f"--resume: config fingerprint mismatch, refusing.\n"
                f"  saved: {st.get('fingerprint')}\n  now:   {fingerprint}")
        model.load_state_dict(st["model"])
        opt.load_state_dict(st["opt"])
        sched.load_state_dict(st["sched"])
        gen.set_state(st["gen"])
        torch.set_rng_state(st["cpu_rng"])
        torch.cuda.set_rng_state_all(st["cuda_rng"])
        if noise_gen is not None and st.get("noise_rng") is not None:
            noise_gen.set_state(st["noise_rng"])
        best, best_epoch, best_corrs = st["best"], st["best_epoch"], st["best_corrs"]
        best_of = dict(st.get("best_of") or {"": best})
        start_epoch = st["epoch"] + 1
        print(f"RESUMED from epoch {st['epoch']} (best {best:.3f} @ "
              f"{best_epoch}); continuing at epoch {start_epoch}", flush=True)

    def evaluate_segs(clip, segs, ctx=None, chunk=None, eval_batch=2):
        """Val rows forwarded with real surrounding context (like deploy),
        scored only inside the segments. Chunks are stacked by equal length
        and forwarded batched (pearson over the concatenated pairs is
        order-invariant). ``ctx`` (12.8 s warmup) and ``chunk`` (133 s)
        are durations on the clip's grid.

        ``eval_batch`` is small because these are the run's largest device
        tensors and they set the allocator's high-water mark: a segment is
        int8 on the host but float32 after the H2D copy, so eight 4000-row
        segs are 4.7 GB in ONE tensor -- enough to push the reserved pool
        past the card, at which point the driver keeps the overflow in
        system memory and every kernel stalls faulting it back. Grouping is
        all this changes: eval runs under model.eval() with no RNG and no
        BatchNorm, so per-segment outputs do not depend on it."""
        ctx = common.rows_at(EVAL_CTX_S, clip.row_hz) \
            if ctx is None else ctx
        chunk = common.rows_at(common.VAL_SEG_S, clip.row_hz) \
            if chunk is None else chunk
        spans = []
        for lo, hi in segs:
            s = lo
            while s < hi:
                a = max(0, s - ctx)
                e = min(a + chunk, hi)
                if e - a < 2:
                    break
                spans.append((a, s, e))
                s = e
        by_len = {}
        for sp in spans:
            by_len.setdefault(sp[2] - sp[0], []).append(sp)
        gbatches = []
        for group in by_len.values():
            gbatches += [group[b0:b0 + eval_batch]
                         for b0 in range(0, len(group), eval_batch)]

        # Every eval batch asks the pinned allocator for the SAME number of
        # elements and views it down to the batch shape. Page-locked blocks
        # are keyed by size and never returned to the OS, so a fresh shape
        # per batch grows untrimmable private commit without bound -- span
        # tails truncate to arbitrary lengths, eval runs every epoch, and
        # the commit climbs past the working-set cap, which can then only
        # balance the books by evicting mapped cache pages. One size keeps
        # one block in the cache. Asking through torch.empty rather than
        # hand-rolling a ring keeps the allocator's CUDA-event reuse guard.
        # The size is a PARAMETER, not a measurement of this clip's batches:
        # a span is at most ``chunk`` rows and a group at most ``eval_batch``
        # of them, and both are identical for every clip on one row grid, so
        # every eval batch in the run asks for exactly this many elements.
        # Sizing it per clip instead would put a distinct block per clip back
        # in the cache, which is the growth this exists to stop.
        per_row = int(np.prod(clip.feats.shape[1:]))
        arena_numel = per_row * eval_batch * chunk

        def prep_eval(g):
            # each seg is read from disk STRAIGHT into its slot of the
            # pinned batch -- these are the run's largest host tensors, so
            # a temporary per seg followed by a stack would double both the
            # copies and the peak; mask rects are then zeroed in place on it
            span = g[0][2] - g[0][0]
            shape = (len(g), span, clip.feat_dim) + tuple(clip.feats.shape[2:])
            arena = torch.empty(arena_numel, dtype=clip.feats.dtype,
                                pin_memory=device == "cuda")
            x = arena[:int(np.prod(shape))].view(shape)

            def _seg(j_sp):
                j, (a, _, e) = j_sp
                clip.fwin_into(a, e, x[j])
            list(READ_POOL.map(_seg, enumerate(g)))
            if clip.masked:
                for j, (a, _, e) in enumerate(g):
                    clip.mask_feats(x[j])
            cw = torch.stack([clip.cut[a:e] for a, _, e in g])
            return g, x, cw

        out, tgt = [], []
        # the LEVEL read rides the same forward: model.level is decoded on
        # every pass and thrown away by the [0], and clip.pos is already
        # resident, so a per-clip level corr costs a Pearson. Velocity
        # cannot represent level, which is why the position head exists --
        # and why a phase-only selection metric is blind to it
        lvl_o, lvl_t = [], []
        with torch.no_grad():
            for g, x, cw in prefetched(gbatches, prep_eval, depth=2):
                # .float() makes the writable device copy; dequant in
                # place -- eval batches are the run's largest tensors
                # (GBs), so a second copy here is real GPU headroom
                x = x.to(device, non_blocking=True).float()
                if clip.feat_scale != 1.0:
                    x.mul_(clip.feat_scale)
                cw = cw.to(device)
                v = model(x, cut=cw)[0]
                for j, (a, s, e) in enumerate(g):
                    sc = clip.scripted[s:e]       # gap rows never score
                    if sc.any():
                        out.append(v[j, s - a:][sc].float())
                        tgt.append((clip.vel[s:e][sc].to(device) / v_std)
                                   .clamp(-6, 6))
                        if model.pos_head:
                            lvl_o.append(model.level[j, s - a:][sc].float())
                            lvl_t.append(clip.pos[s:e][sc].to(device))

        def scores(oo):
            if not oo:
                return (float("nan"),) * 5
            o, t = torch.cat(oo), torch.cat(tgt)
            # third and fourth: the same two readings restricted to the
            # slow band (SLOW_BAND_POS_S), where the whole-signal Pearson
            # is structurally blind because the fast rows own the
            # variance. Recorded, never selected on.
            spd = t.abs() * v_std
            m = (spd >= SLOW_BAND_POS_S[0]) & (spd <= SLOW_BAND_POS_S[1])
            if int(m.sum()) > 8:
                so, st = o[m], t[m]
                slow = (pearson(so, st).item(),
                        float(so.abs().mean() / st.abs().mean()
                              .clamp_min(1e-6)))
            else:
                slow = (float("nan"), float("nan"))
            # second value: predicted-over-target mean |v| -- the hedging
            # detector. Pearson is scale-invariant, so amplitude collapse
            # is invisible to selection without it
            # fifth: the position head's own Pearson over the same val
            # rows -- the LEVEL axis, the one a phase tail criterion
            # trades away, so it is recorded per clip rather than
            # inferred from the velocity numbers
            lvl = float("nan")
            if lvl_o:
                lvl = pearson(torch.cat(lvl_o), torch.cat(lvl_t)).item()
            return (pearson(o, t).item(),
                    float(o.abs().mean() / t.abs().mean().clamp_min(1e-6)),
                    slow[0], slow[1], lvl)
        return scores(out)

    def val_corrs():
        return {c.id: evaluate_segs(c, segs)
                for c, (_, segs, _) in zip(clips, seg_splits)}

    for epoch in range(start_epoch, args.epochs + 1):
        model.train()
        t0 = time.time()
        # the cap's warmup: an untrained head contradicts its target
        # almost everywhere, so absorption in the first epochs reads the
        # head rather than the label. Held off, the cap engages once the
        # model has an opinion worth listening to.
        cap_on = bool(PHASE_CAP) and epoch > PHASE_CAP_WARM
        ep_items = items
        perm = torch.randperm(len(ep_items), generator=gen).tolist()
        # mask-dropout: per CLIP per EPOCH, withhold the clip's banner masks
        # so unmasked input stays in-distribution (a 100%-masked schedule
        # made "train masked, infer unmasked" fail as pure distribution
        # shift). Draws only when enabled -- the flag at 0 is byte-neutral.
        mskip = set()
        if MASK_DROPOUT > 0:
            d = torch.rand(len(clips), generator=gen)
            mskip = {ci for ci in range(len(clips))
                     if float(d[ci]) < MASK_DROPOUT}
        tot, nb = 0.0, 0
        cap_absorbed, cap_rows = 0.0, 0.0
        kl_acc = [0.0, 0.0, 0]  # R-Drop KL sum / sum of squares / batches
        win_shape = (args.win, clips[0].feat_dim) \
            + tuple(clips[0].feats.shape[2:])
        def prep_train(batch):
            # host side of a batch: disk reads straight into pinned memory.
            # RNG-free (prefetched() requirement); device moves stay in
            # the consumer so the H2D copy overlaps via non_blocking. Each
            # window lands in its own slot of the ONE pinned batch -- a
            # temporary per window then a stack would copy everything
            # twice, and prep is the epoch's rate limiter (io_wait). The
            # allocation is always full-batch sized and sliced down, so
            # the pinned cache holds ONE block size however the last batch
            # of an epoch falls. Mask rects are zeroed in place on it.
            arena = torch.empty((args.batch,) + win_shape,
                                dtype=clips[0].feats.dtype,
                                pin_memory=device == "cuda")
            x = arena[:len(batch)]

            def _win(j_ci_i):
                j, (ci, i) = j_ci_i
                s = clips[ci].starts[i]
                clips[ci].fwin_into(s, s + args.win, x[j])
            list(READ_POOL.map(_win, enumerate(batch)))
            for j, (ci, i) in enumerate(batch):
                if clips[ci].masked and ci not in mskip:
                    clips[ci].mask_feats(x[j])
            v = torch.stack(
                [clips[ci].vel[clips[ci].starts[i]:
                               clips[ci].starts[i] + args.win]
                 for ci, i in batch])
            vp = torch.stack(
                [clips[ci].vel_phase[clips[ci].starts[i]:
                                     clips[ci].starts[i] + args.win]
                 for ci, i in batch])
            lvl = None
            m = torch.stack(
                [sup_eff[ci][clips[ci].starts[i]:
                             clips[ci].starts[i] + args.win]
                 for ci, i in batch]).float()
            cut_b = torch.stack(
                [clips[ci].cut[clips[ci].starts[i]:
                               clips[ci].starts[i] + args.win]
                 for ci, i in batch])
            lvl = torch.stack(
                [clips[ci].level[clips[ci].starts[i]:
                                 clips[ci].starts[i] + args.win]
                 for ci, i in batch])
            menv = None
            if sup_env is not None:
                menv = torch.stack(
                    [sup_env[ci][clips[ci].starts[i]:
                                 clips[ci].starts[i] + args.win]
                     for ci, i in batch]).float()
            return (x, v, m, cut_b, lvl, menv, vp)

        batches = [[ep_items[j] for j in perm[b0:b0 + args.batch]]
                   for b0 in range(0, len(perm), args.batch)]
        io_wait = [0.0]
        # depth 3 (not 8): each prefetched batch holds PINNED (non-evictable)
        # host RAM and races the memmap reader ahead; 8 was an OOM risk on a
        # box where the int8 cache already fills RAM as standby. Prefetch
        # depth is I/O overlap only -- order-preserved + RNG-free, so the
        # trained model is byte-identical.
        # prefetched preserves order, so a batch's window list still
        # identifies the rows the tensors were built from -- which is what
        # the absorption trace attributes releases to a clip with
        for bmeta, (x, v, m, cut_b, lvl, menv, vp) in zip(
                batches,
                prefetched(batches, prep_train, depth=3, wait=io_wait)):
            # .float() makes the writable device copy; dequant (and the
            # patch-drop zeroing below) mutate it in place rather than
            # allocating fresh x-sized tensors per batch
            x = x.to(device, non_blocking=True).float()
            if f_scale != 1.0:
                x.mul_(f_scale)
            # the cut channel moves to the device BEFORE the augmentations
            # so the attention guide can read the clean latents with the
            # flag its own frontend expects
            cut_b = cut_b.to(device)
            if PATCH_DROP > 0:
                # occlusion pressure on the mask net: zero grid cells so
                # attention cannot lean on any single patch. No 1/(1-p)
                # rescale -- attention pooling renormalizes over cells,
                # and the point is occlusion, not expectation-preserving
                # noise. Train-only by construction (eval runs val_corrs,
                # never this loop).
                ps = (x.shape[0], 1, 1, x.shape[3], x.shape[4])
                keep = torch.rand(ps, generator=gen) >= PATCH_DROP
                x.mul_(keep.to(device, non_blocking=True).float())
            if noise_gen is not None:
                # additive Gaussian on the dequantized latents
                # (whitened features, ~unit std -- sigma is in std units).
                # Own device generator: the global CUDA stream and the CPU
                # stream never see these draws. Train-only by construction
                # (eval runs val_corrs, never this loop); both R-Drop
                # views share the noised frontend, like patch-drop.
                x.add_(torch.randn(x.shape, generator=noise_gen,
                                   device=device) * LATENT_NOISE)
            v = v.to(device)
            # amplitude-shaped target (envelope) and phase target (CE):
            # one tensor unless --env-interp splits them
            tgt_env = (v / v_std).clamp(-6, 6)
            tgt = (vp.to(device) / v_std).clamp(-6, 6)
            m = m.to(device)
            mo_t = m      # off-phase mask (level/env); occlusion carves it
            teacher_env = env_t = None
            env_t = envelope(tgt_env, model.env_k)
            teacher_env = env_t + ENV_NOISE * torch.randn(
                env_t.shape, generator=gen).to(device)
            keep = torch.rand(env_t.shape[0], generator=gen) \
                >= ENV_DROP
            teacher_env = teacher_env * keep.to(device).float()[:, None]
            # target bins and loss masks: RNG-free, view-independent -- both
            # R-Drop views score against the same targets, so the two
            # forwards differ ONLY in their dropout draws
            tb = ((tgt + 6.0) / 12.0 * (BINS_DEFAULT - 1)) \
                .clamp(0, BINS_DEFAULT - 1)
            eb = me = pb = None
            eb = (env_t / 6.0 * (model.env_bins - 1)) \
                .clamp(0, model.env_bins - 1)
            me = mo_t
            me = menv.to(device) \
                * (1.0 + COTRAIN_HOLD_W
                   * (env_t < COTRAIN_QUIET_THR).float()
                   + COTRAIN_LOUD_W
                   * (env_t > COTRAIN_LOUD_THR).float())
            ml = mo_t                                    # level CE mask
            lvl = lvl.to(device)
            pb = (lvl / 100.0 * (model.pos_bins - 1)) \
                .clamp(0, model.pos_bins - 1)
            # the frontend (mask net + attention pooling) carries no
            # dropout, so both R-Drop views see it identically -- and it
            # is ~5x the trunk's multiplies. Computing it ONCE makes the
            # second view cost a TCN pass, not a whole forward.
            with torch.autocast("cuda", dtype=torch.bfloat16,
                                enabled=device == "cuda"):
                front = model.frontend(x, cut_b)

            def forward_view():
                """One dropout view: TCN + heads over the shared frontend.
                Every tensor the loss reads comes back HERE rather than off
                the model, because the two-view cap criterion needs both
                views' logits before either view's loss is formed."""
                with torch.autocast("cuda", dtype=torch.bfloat16,
                                    enabled=device == "cuda"):
                    pred, conf, _gate = model(teacher_env=teacher_env,
                                              front=front)
                return (pred.float(), conf.float(), model.logits.float(),
                        model.env_logits.float(),
                        model.pos_logits.float())

            def cap_keep(vlg):
                """The rows one view keeps under the phase cap, or None
                when the cap is off or still inside its warmup.

                A row is absorbed when the model puts below-chance
                probability MASS on the target's neighborhood --
                contradiction in velocity region, not in exact bin. A
                sharp prediction one bin off is directionally right and
                keeps its supervision; the exact-bin and smoothed-CE tests
                both absorbed such near-misses (measured: 36% of rows,
                learning behind the no-cap baseline). Absorbed rows leave
                EVERY phase-side loss: the CE, the derivative term and the
                R-Drop KL at the call site. Level and envelope keep full
                supervision everywhere (draining them is the measured
                failure mode of span masking). The WIDTH is independent of
                the dose: chance scales with the window, so ln(bins) stays
                the self-anchored threshold at any --phase-cap-half.
                """
                if not cap_on:
                    return None
                with torch.no_grad():
                    return neighborhood_keep(vlg, tb, PHASE_CAP,
                                             PHASE_CAP_HALF)

            def loss_from(view, keep):
                """The full supervised loss for one view, over the rows
                ``keep`` leaves in phase supervision."""
                pred, conf, vlg, env_lg, pos_lg = view
                mk_ = m if keep is None else m * keep
                l_main = masked_ce(vlg, tb, mk_)
                dd = (pred[:, 1:] - pred[:, :-1]) \
                    - (tgt[:, 1:] - tgt[:, :-1])
                mp = mk_[:, 1:] * mk_[:, :-1]
                l_deriv = (dd.square() * mp).sum() / mp.sum().clamp_min(1)
                with torch.no_grad():
                    corr_w = masked_pearson(pred, tgt, m).clamp(0, 1)
                cm = (conf * m).sum(dim=1) / m.sum(dim=1).clamp_min(1)
                l_conf = F.mse_loss(cm, corr_w)
                lo = l_main + W_DERIV * l_deriv + W_CONF * l_conf
                lo = lo + ENV_W * masked_ce(env_lg, eb, me)
                l_pos = POS_W * masked_ce(
                    pos_lg, pb, ml,
                    weight=pos_weight)
                if l_pos is not None:
                    lo = lo + l_pos
                return lo, l_main, l_pos

            view1 = forward_view()
            keep = cap_keep(view1[2])
            view2 = keep2 = None
            if args.cap_two_view and RDROP > 0 and keep is not None:
                # the two-view criterion: a row leaves phase supervision
                # only when BOTH dropout views find it contradictory. The
                # per-view test absorbs on the strength of one dropout
                # draw, so a row the model is merely UNSURE about reads as
                # a contradictory label; agreement is what separates the
                # two. The partner view is drawn here rather than below,
                # so the two forwards keep their order and only the losses
                # move after them.
                view2 = forward_view()
                keep2 = cap_keep(view2[2])
                keep = keep2 = torch.maximum(keep, keep2)
            loss, l_phase, l_level = loss_from(view1, keep)
            vlg = view1[2]
            if RDROP > 0:
                # second view, same batch and same teacher envelope: only
                # the dropout masks differ. The consistency term covers the
                # PHASE distributions alone: the envelope is generative by
                # construction (its one-to-many freedom is the whole point
                # of the factorization; forcing two views to agree there
                # would train the authorship axis out), and the level head
                # measured harmful in the KL -- consistency there smooths
                # the level decode toward the mean, which is where composed
                # styling reads stroke level from
                if view2 is None:
                    view2 = forward_view()
                    keep2 = cap_keep(view2[2])
                loss2, _lp2, _ll2 = loss_from(view2, keep2)
                vlg2 = view2[2]
                mr = m if keep is None else m * keep * keep2
                lkl_r = rdrop_kl(vlg, vlg2, mr)
                loss = 0.5 * (loss + loss2) + RDROP * lkl_r
                kl_v = lkl_r.detach().item()
                kl_acc[0] += kl_v
                kl_acc[1] += kl_v * kl_v
                kl_acc[2] += 1
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            tot += loss.item()
            nb += 1
            if keep is not None:
                absorbed = (1.0 - keep) * m
                cap_absorbed += float(absorbed.sum())
                cap_rows += float(m.sum())
        sched.step()
        t_tr = time.time()
        model.eval()
        # selection is on the marginal (phase) decode, and the trainer
        # reads nothing else: the generative decode is the deploy
        # tools' read, on drafts, where its numbers mean something
        model.gen_env = False
        corrs = val_corrs()
        model.gen_env = True
        mean0 = np.nanmean([v[0] for v in corrs.values()])
        vmag = np.nanmean([v[1] for v in corrs.values()])
        # the slow-band read rides beside mean0 without touching selection:
        # what it is FOR is telling whether the two rankings diverge, which
        # they cannot do if one is derived from the other
        slow0 = np.nanmean([v[2] for v in corrs.values()])
        slowmag = np.nanmean([v[3] for v in corrs.values()])
        # level rides beside the phase numbers for the same reason slow0
        # does: it is the axis they cannot express, so a divergence is
        # only visible if both are on the line
        level0 = np.nanmean([v[4] for v in corrs.values()])
        # one glanceable line: the selection metric (mean0, starred when it
        # is a new best), the hedging detector (vmag), the benchmark pair,
        # and the three weakest clips -- a clip training cannot lift is
        # gradient noise, so the tail of the ranking is the live read.
        # The full per-clip table prints
        # once at the end, worst first, and rides in the checkpoint.
        gate_str = "  ".join(f"{g}:{corrs[g][0]:.3f}"
                             for g in gate_ids if g in corrs)
        ranked = sorted((v[0], k) for k, v in corrs.items()
                        if not np.isnan(v[0]))
        worst = "  ".join(f"{k}:{c:.2f}" for c, k in ranked[:3])
        # The caching allocator only grows its reserved pool, and once that
        # pool crowds the card the WDDM driver keeps the overflow in SYSTEM
        # memory: kernels then stall faulting their own tensors back, which
        # reads as a slow epoch with the loader idle. Returning the cache
        # each epoch bounds the pool -- ``rsv`` is what to watch, and it
        # should sit flat rather than climb toward the card's capacity.
        rsv = torch.cuda.memory_reserved() / 2**30 if device == "cuda" else 0.0
        if device == "cuda":
            torch.cuda.empty_cache()
        print(f"ep {epoch:3d}/{args.epochs} loss {tot/max(nb,1):.4f}"
              f" mean0 {mean0:.3f}{'*' if mean0 > best else ' '}"
              f" vmag {vmag:.2f}"
              + (f" slow0 {slow0:.3f}/{slowmag:.2f}"
                 if not np.isnan(slow0) else "")
              + (f" lvl {level0:.3f}" if not np.isnan(level0) else "")
              + (f" cap {100 * cap_absorbed / max(cap_rows, 1):.1f}%"
                 if PHASE_CAP else "")
              # the R-Drop KL term's own trace, mean/std over the epoch's
              # batches: the reading of what the tail bins carry in it
              + (f" kl {kl_acc[0] / kl_acc[2]:.4f}/"
                 f"{max(kl_acc[1] / kl_acc[2] - (kl_acc[0] / kl_acc[2]) ** 2, 0.0) ** 0.5:.4f}"
                 if kl_acc[2] else "")
              + (f" | gate {gate_str}" if gate_str else "")
              + f" | worst {worst}"
              + f" | {time.time()-t0:.0f}s"
                f" ({t_tr-t0:.0f}+{time.time()-t_tr:.0f},"
                f" io {io_wait[0]:.0f}, rsv {rsv:.1f}G)", flush=True)
        # Selection criteria, each keeping its own checkpoint. Best mean
        # phase is the one that ships; the others exist because the deploy
        # heads are refit AFTER selection, so the epoch whose frozen
        # features support the best PRODUCT is not necessarily the epoch
        # with the best pooled phase -- and screen rankings have been wrong
        # about deploy three times. The extra checkpoints change nothing on
        # their own: the OOS deploy read decides between them.
        sel = {"": mean0}
        if args.keep_pareto:
            c0 = sorted(v[0] for v in corrs.values())
            sel["_tail"] = float(np.mean(c0[:2]))
            sl = [v[2] for v in corrs.values() if not np.isnan(v[2])]
            if sl:
                sel["_slow"] = float(np.mean(sl))
            # the level criterion exists because the tail criterion COSTS
            # level every time it wins (measured on two arms). A set with
            # no level-selected member cannot win that back
            lv = [v[4] for v in corrs.values() if not np.isnan(v[4])]
            if lv:
                sel["_level"] = float(np.mean(lv))
        improved = [s for s, v in sel.items() if v > best_of.get(s, -np.inf)]
        for s in improved:
            best_of[s] = sel[s]
        if "" in improved:
            best, best_epoch, best_corrs = mean0, epoch, dict(corrs)
        # the bare trunk as every reader loads it; jepa_best.pt and an
        # epochs/epNNN.pt are the same dict at different epochs
        ck = {"model": model.state_dict(), "v_std": v_std,
              "epoch": epoch,
              "corrs0": {k: v[0] for k, v in corrs.items()},
              "trained_sigs": trained_sigs,
              "vmag0": {k: v[1] for k, v in corrs.items()},
              "feat_dir": FEAT_DIR_DEFAULT,
              "row_hz": row_hz,
              # the WINDOW GEOMETRY this trunk saw. Head refits must
              # rebuild the same windows -- a refit at another stride
              # trains the heads on a different number of gradient steps
              # per epoch and reports a clean run while fitting a
              # different recipe, so jepa_refit reads these rather than
              # defaulting.
              "win": args.win,
              "stride": args.stride,
              # what this trunk HELD OUT. Every scorer rebuilds the same
              # rows from these (common.val_regions), so a metric cannot
              # quietly describe trained material -- the defect that made
              # every per-clip reading in-sample when the scorers took a
              # tail instead. ``split`` names the geometry so a reference
              # measured under another one refuses to render a verdict.
              "val_frac": args.val_frac,
              "split": common.VAL_SPLIT,
              "masks_dir": common.MASKS_DIR,
              "dataset": args.dataset,
              "basis_id": run_bid,
              "arch": {"dim": dim, "heads": MASK_HEADS,
                       "tcn_ch": TCN_CH,
                       "dropout": DROPOUT,
                       "vel_bins": BINS_DEFAULT,
                       "gen_env": True,
                       "env_ctx": ENV_CTX,
                       "env_k": model.env_k,
                       "pos_head": True,
                       "mask_ch": list(MASK_CH),
                       "dilations": list(DILATIONS),
                       "cut_flag": True,
                       "coord": True}}
        if args.init_from:    # provenance: this trunk is a fine-tune
            ck["init_from"] = Path(args.init_from).name
        if improved:
            # one write, then copies: an epoch that improves a SECONDARY
            # criterion must never land on jepa_best.pt, which is the
            # checkpoint every defaulted tool reads
            primary = runs / (f"jepa_best{improved[0]}.pt"
                              if improved[0] else "jepa_best.pt")
            torch.save(ck, primary)
            for s in improved:
                p = runs / (f"jepa_best{s}.pt" if s else "jepa_best.pt")
                if p != primary:
                    shutil.copyfile(primary, p)
        # per-epoch resume point: full post-epoch state (weights advanced by
        # this epoch's step, RNG advanced by its draws), atomic so a kill
        # mid-write can't corrupt it. ~40 MB, ~1-2 s vs a >100 s epoch.
        rtmp = runs / "resume_state.pt.tmp"
        torch.save({"epoch": epoch, "model": model.state_dict(),
                    "opt": opt.state_dict(), "sched": sched.state_dict(),
                    "gen": gen.get_state(),
                    "cpu_rng": torch.get_rng_state(),
                    "cuda_rng": torch.cuda.get_rng_state_all(),
                    "noise_rng": (noise_gen.get_state().cpu()
                                  if noise_gen is not None else None),
                    "best": best, "best_epoch": best_epoch,
                    "best_corrs": best_corrs, "best_of": best_of,
                    "fingerprint": fingerprint},
                   rtmp)
        rtmp.replace(resume_path)
        if args.patience and epoch - best_epoch >= args.patience:
            print(f"early exit: no mean0 improvement for {args.patience} "
                  f"epochs (best {best:.3f} @ epoch {best_epoch})", flush=True)
            break
    if best_corrs:
        # the pruning view: worst first, because that end of the ranking is
        # where an unfittable target shows up (a clip training cannot lift
        # supervises nothing but mean authoring). Read off the SELECTED
        # epoch, not the last one.
        cells = [f"{k}:{v[0]:.3f}" for k, v in
                 sorted(best_corrs.items(), key=lambda kv: kv[1][0])]
        print(f"per-clip val corr @ best epoch {best_epoch} (worst first):",
              flush=True)
        for i in range(0, len(cells), 6):
            print("  " + "  ".join(cells[i:i + 6]), flush=True)
    print(f"done; best mean0 {best:.3f} @ epoch {best_epoch} "
          f"(checkpoint {runs}/jepa_best.pt)", flush=True)
    # run finished cleanly -> drop the resume point so a later fresh run in
    # this dir doesn't silently continue this one
    resume_path.unlink(missing_ok=True)


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()

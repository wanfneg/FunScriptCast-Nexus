"""The one scoring library: how a WRITTEN funscript is read against its
script, and how a tree of clips is pooled.

Every ranking column comes from here -- ``jepa_infer`` calls it on the
draft it just wrote, ``read.py`` calls it on drafts already on disk (a
goblinscript bench, a stored release). The product is the action list,
so the action list is what ranks; the grid track a decode styles is a
diagnostic and stays in ``jepa_infer``'s own panel.

Three scorers, one clock:

* **position** -- the draft and the script resampled onto the CACHE's own
  row clock (video-aligned, never re-based): corr, MAE, travel ratio and
  reversal-timing kappa on the rows the script covers. Unscripted gaps
  are dropped, because inside one the interpolated script position is a
  fabrication a draft is neither right nor wrong against.
* **timing** -- continuous-ms reversal matching at two tolerances
  (``REV_TOL_S`` and a loose 333 ms), recall beside precision, the
  matched |dt| median beside its p95, per band of the script's own stroke
  frequency. Grid-free, so a 15 rows/s product and a 30 rows/s one rank
  on equal terms.
* **speed** -- half-stroke speed against the script's LOCAL speed in the
  action domain: over-speed events per minute, the slow-section spike
  rate the admission test is anchored on, the device-cap share, and the
  chopped-fast-section read. The script itself is scored as an arm and
  anchors every multiple.

Held-out rows are honoured by all three: a corpus clip scores on the rows
its checkpoint desupervised, an out-of-sample clip on the whole clip. The
row clock and the held-out regions are STAMPED into the arm's record by
``jepa_infer`` so a later read reproduces exactly the rows it scored.

Pooling has two definitions and each column names its own: equal weight
per clip with the model's own worst-``TAIL_N`` beside the mean (the gate
rule), and slow-MINUTE weighting for the slow-section rate, which is a
rate per minute of slow script time.
"""
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from . import common
from . import perception

TAIL_N = 2                        # a model's own worst clips form its tail
SCORE_HZ = 240.0                  # the timing scorer's grid (~4.2 ms)
TOLS_MS = (66.667, 333.0)         # common.REV_TOL_S and the loose read
TOL_KEYS = ("67", "333")          # record keys for the two tolerances
AMP_MIN = 20.0                    # a 4-unit wiggle is not a stroke
FREQ_BANDS = (("slow", 0.0, 1.0), ("mid", 1.0, 2.0), ("fast", 2.0, 1e9))
# the action-domain speed read, as artifact_speed defines it
SPEED = {"ref_hz": 30.0, "smooth_s": 0.3, "fast_hz": 3.75, "amp": 20.0,
         "floor": 20.0, "stub_win_s": 3.0}
STUB_MIN_FAST = 4      # script fast half-strokes that make a window FAST
STUB_RATIO = 1 / 3     # amp below this fraction of the script's local
                       # fast-stroke amplitude = a stub
STUB_BROKEN = 0.25     # stub share above which a window counts broken
SPEED_COLS = ("fast", "amp20", "x2", "x3", "per", "p99", "over600", "peak",
              "sl2x", "sl3x", "slpeak", "stub", "brok", "spdW", "gapW",
              "sl2x_styled", "sl2x_smooth", "sl3x_styled", "sl3x_smooth")
# the STYLED half of a slow-section spike: the script itself carries a
# half-stroke of at least AMP_MIN units above STYLE_X times its own smoothed
# local speed within STYLE_DILATE_S of the draft's. A spike there is a
# scripter's fast stroke written at the wrong moment; a spike on a passage
# the script keeps smooth is the artifact the bar exists for. The two halves
# sum to the whole column. The amplitude floor is what makes the mask a
# STROKE: a script's slow passages also carry 8 to 14 unit vertices one
# frame apart, scripter texture the RDP layer never writes, and without the
# floor the mask is those vertices' neighbourhoods
STYLE_X = 2.0
STYLE_DILATE_S = 0.5
GUARD_COLS = ("fast", "amp20", "per", "stub", "brok")
# the SHAPE columns: the draft's half-stroke peak-speed and duration
# distributions against the script's own, as a Wasserstein-1 distance in
# the script's IQR. A mean can land on the script's while every stroke is a
# crawl or a sprint, and no rate column sees that; these do. Pooled at equal
# weight with the worst-2 beside them, higher is farther
SHAPE_COLS = ("spdW", "gapW")
POSITION_COLS = (("corr", False), ("kappa", False), ("mae", True),
                 ("travel", False), ("rev_prec", False), ("rev_rec", False))
PANEL_COLS = (("band_recall_hi", "bandHi", False),
              ("band_recall_lo", "bandLo", False),
              ("mae_extreme", "maeExt", True),
              ("still_p", "stillP", False), ("still_r", "stillR", False))
NAN = float("nan")


# ---------------------------------------------------------------- loading

def load_actions(path):
    """A funscript's action list (dict or bare list), as written."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d["actions"] if isinstance(d, dict) else d


def draft_path(arm_dir, vid):
    """jepa_infer names drafts ``<ID>_jepa.funscript``; goblinscript names
    them after the video, ``<ID>.funscript``. Both are written action
    lists, so an arm may be either."""
    p = Path(arm_dir) / f"{vid}_jepa.funscript"
    return p if p.exists() else Path(arm_dir) / f"{vid}.funscript"


def lag_ms(dataset, vid, field="applied_ms", row_hz=common.ROW_HZ):
    """The clip's lag on this row grid, from its sidecar; 0 without one.
    ``field`` None reads the raw script clock."""
    if not field:
        return 0.0
    f = Path(dataset) / common.lag_dir_name(row_hz) / f"{vid}.json"
    if not f.exists():
        return 0.0
    return float(json.loads(f.read_text(encoding="utf-8")).get(field, 0.0))


class Script:
    """The script on the VIDEO clock: ``clean`` sanitized (t, pos) tuples
    shifted by the applied lag (JepaClip reads the script at
    ``row_time + lag``, so an action written at s belongs at video time
    s - lag), its unscripted ``gaps`` on that clock, and ``dur`` ms."""

    def __init__(self, clean, dur, lag, speed_clamp):
        self.clean = [(t - lag, p) for t, p in clean]
        self.dur = float(dur)
        self.lag = float(lag)
        self.speed_clamp = bool(speed_clamp)
        self.gaps = common.script_gaps(self.clean, self.dur) \
            if self.clean else []

    @property
    def t(self):
        return np.array([t for t, _ in self.clean], dtype=np.float64)

    @property
    def p(self):
        return np.array([p for _, p in self.clean], dtype=np.float64)


def load_script(dataset, vid, lag_field="applied_ms", row_hz=common.ROW_HZ,
                speed_clamp=True):
    """The clip's script as ``Script``; ``None`` for a clip without one.
    ``speed_clamp`` follows the checkpoint's stamp: the harness target is
    the script read the one way training read it."""
    meta = perception.load_meta(dataset, vid)
    raw = perception.load_script(dataset, vid)
    if not raw:
        return None
    clean = common.sanitize_aligned(
        raw, meta["duration_ms"],
        max_speed=common.MAX_POS_RATE if speed_clamp else None)
    return Script(clean, meta["duration_ms"],
                  lag_ms(dataset, vid, lag_field, row_hz), speed_clamp)


def script_from_clip(ds):
    """The same ``Script`` from a loaded ``JepaClip`` (jepa_infer's path),
    so the draft-time read and the disk read share one definition."""
    return Script(ds.clean, ds.duration_ms, ds.lag_ms, ds.speed_clamp)


def load_draft(actions, dur):
    """A written action list sanitized onto the video clock as (t, pos)
    tuples. Never speed-clamped here: the written list is the artifact,
    and a cap violation in it is a finding (``score_speed`` counts it)."""
    return common.sanitize_aligned(actions, dur, max_speed=None)


# ------------------------------------------------------------- primitives

def corr(a, b):
    """Pearson correlation; NaN below 8 samples or on a flat side."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if len(a) < 8 or a.std() < 1e-9 or b.std() < 1e-9:
        return NAN
    return float(np.mean((a - a.mean()) * (b - b.mean()))
                 / (a.std() * b.std()))


def box(x, k, pad="zero"):
    """Centred moving mean of width ``k`` (forced odd), length-preserving.
    ``pad="zero"`` is numpy's ``mode="same"`` (the panel's historical
    smoother); ``pad="edge"`` never reads past the ends as position 0."""
    k = int(k) | 1
    x = np.asarray(x, dtype=np.float64)
    if pad == "edge":
        return common.local_mean(x, k)
    return np.convolve(x, np.ones(k) / k, mode="same")


def runs_of(mask):
    """(starts, ends) of the maximal True runs of a boolean mask; ends are
    exclusive."""
    m = np.concatenate(([0], np.asarray(mask, dtype=bool).view(np.int8),
                        [0]))
    d = np.diff(m)
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


def flip_indices(series, flat="up"):
    """Indices where the sign of ``diff(series)`` changes. ``flat="up"``
    counts a zero slope as rising (the grid definition); ``flat="carry"``
    carries the last non-zero slope across a plateau so its two ends do
    not mint two reversals (the ms definition)."""
    d = np.sign(np.diff(np.asarray(series, dtype=np.float64)))
    if flat == "carry":
        nz = d != 0
        if not nz.any():
            return np.zeros(0, dtype=int), d
        idx = np.where(nz, np.arange(len(d)), 0)
        np.maximum.accumulate(idx, out=idx)
        d = d[idx]
        return np.where(np.diff(d) != 0)[0] + 1, d
    d[d == 0] = 1
    return np.where(d[1:] * d[:-1] < 0)[0] + 1, d


def w1(u, v):
    """Wasserstein-1 between two empirical 1-D samples, in the samples' own
    units: the integral of |F_u - F_v|, which needs no binning choice and
    so cannot be tuned after the fact."""
    u = np.sort(np.asarray(u, dtype=np.float64))
    v = np.sort(np.asarray(v, dtype=np.float64))
    if not len(u) or not len(v):
        return NAN
    allv = np.sort(np.concatenate((u, v)))
    d = np.diff(allv)
    cu = np.searchsorted(u, allv[:-1], side="right") / len(u)
    cv = np.searchsorted(v, allv[:-1], side="right") / len(v)
    return float(np.sum(np.abs(cu - cv) * d))


def _iqr(x):
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 4:
        return NAN
    q1, q3 = np.percentile(x, [25, 75])
    return float(q3 - q1) if q3 > q1 else NAN


def half_strokes(t, p):
    """Direction-change to direction-change -> (t_mid, amp, speed, dt_s)
    of an action list given as (times_ms, pos) arrays."""
    t = np.asarray(t, dtype=np.float64)
    p = np.asarray(p, dtype=np.float64)
    fl, _ = flip_indices(p)
    idx = np.concatenate(([0], fl, [len(p) - 1]))
    a0, a1 = idx[:-1], idx[1:]
    dt = np.maximum(t[a1] - t[a0], 1e-9) / 1000.0
    amp = np.abs(p[a1] - p[a0])
    return 0.5 * (t[a0] + t[a1]), amp, amp / dt, dt


def track_reversals(clean, t0_ms, t1_ms, hz=SCORE_HZ):
    """(times_ms, polarity, prominence) of a polyline's extrema on a fine
    grid, smoothed with the project's ONE reversal definition
    (``common.EXTREMUM_SMOOTH_S``). Prominence is min(previous swing,
    next swing): a blip riding the flank of a big move does not inherit
    that move's amplitude."""
    if len(clean) < 3:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    t = np.arange(t0_ms, t1_ms, 1000.0 / hz)
    pos = np.asarray(common.funscript_position(clean, t), dtype=float)
    sm = box(pos, int(round(common.EXTREMUM_SMOOTH_S * hz)), pad="edge")
    flips, sign = flip_indices(sm, flat="carry")
    if not len(flips):
        return np.zeros(0), np.zeros(0), np.zeros(0)
    vals = sm[flips]
    pol = -sign[flips - 1]          # +1 = peak (was rising), -1 = valley
    swings = np.abs(np.diff(vals))
    prom = np.minimum(np.concatenate([[swings[0]], swings]),
                      np.concatenate([swings, [swings[-1]]])) \
        if len(swings) else np.zeros(len(vals))
    return t[flips], pol, prom


def action_reversals(clean, t0_ms, t1_ms):
    """(times_ms, polarity, prominence) of an action list's own direction
    changes at its exact stamps -- the clock the draft writes and the
    script carries, with no resample between them."""
    if len(clean) < 3:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    t = np.asarray([a[0] for a in clean], dtype=float)
    p = np.asarray([a[1] for a in clean], dtype=float)
    keep = np.concatenate(([True], np.diff(p) != 0))
    t, p = t[keep], p[keep]
    if len(p) < 3:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    d = np.sign(np.diff(p))
    fl = np.where(d[1:] * d[:-1] < 0)[0] + 1
    prom = np.minimum(np.abs(p[fl] - p[fl - 1]), np.abs(p[fl + 1] - p[fl]))
    pol = np.where(d[fl - 1] > 0, 1, -1)
    m = (t[fl] >= t0_ms) & (t[fl] < t1_ms)
    return t[fl][m], pol[m], prom[m]


def local_stroke_hz(times):
    """Stroke frequency at each reversal: half the local reversal rate."""
    if len(times) < 3:
        return np.full(len(times), np.nan)
    gaps = np.diff(times) / 1000.0
    around = np.concatenate([[gaps[0]], (gaps[:-1] + gaps[1:]) / 2.0,
                             [gaps[-1]]])
    with np.errstate(divide="ignore"):
        return 1.0 / (2.0 * np.maximum(around, 1e-6))


def match(ref_t, ref_pol, hyp_t, hyp_pol, tol_ms):
    """Greedy nearest same-polarity match, one hypothesis per reference
    -> (hit per reference, signed dt per reference, used per hypothesis)."""
    used = np.zeros(len(hyp_t), dtype=bool)
    hit = np.zeros(len(ref_t), dtype=bool)
    dt = np.full(len(ref_t), np.nan)
    for i, (t, p) in enumerate(zip(ref_t, ref_pol)):
        cand = np.where((~used) & (hyp_pol == p)
                        & (np.abs(hyp_t - t) <= tol_ms))[0]
        if len(cand):
            j = cand[np.argmin(np.abs(hyp_t[cand] - t))]
            used[j] = True
            hit[i] = True
            dt[i] = hyp_t[j] - t
    return hit, dt, used


def scripted_mask(times, gaps):
    """True where a time falls on a span the script covers."""
    m = np.ones(len(times), dtype=bool)
    for g0, g1 in gaps:
        m &= ~((times >= g0) & (times < g1))
    return m


def speed_bands(mag, sel=None):
    """Per-clip terciles of a smoothed |velocity| -> (q1, q2, band per
    row: 0 slow / 1 mid / 2 fast). ``sel`` picks the rows the edges are
    cut on (held-out rows); every row is then banded."""
    ref = mag if sel is None else mag[sel]
    q1, q2 = np.percentile(ref, [33.3, 66.7])
    return float(q1), float(q2), np.digitize(mag, [q1, q2])


def pool_tail(vals, worst_high=False, n=TAIL_N):
    """(mean, mean of the worst ``n``) over the finite values; 'worst' is
    the low end unless ``worst_high`` (MAE, |dt|)."""
    v = [float(x) for x in vals
         if x is not None and math.isfinite(float(x))]
    if not v:
        return NAN, NAN
    s = sorted(v, reverse=worst_high)
    k = min(n, len(s))
    return float(sum(v) / len(v)), float(sum(s[:k]) / k)


def slow_minute_pool(rows, col, weight="slow_min"):
    """One speed column pooled by slow minutes -- the unit the slow-section
    rates are in. ``rows`` are per-clip speed dicts."""
    pairs = [(r[col], r[weight]) for r in rows
             if r.get(col) is not None and math.isfinite(r[col])]
    tot = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tot if tot else NAN


def clip_bootstrap(deltas, resamples=20000, seed=888, stat=np.mean):
    """Paired clip-bootstrap of one column's per-clip deltas: the mean,
    its 95% interval and P(>0). Over CLIPS, never rows -- rows inside a
    clip are not independent draws."""
    d = np.asarray([x for x in deltas if math.isfinite(x)], dtype=float)
    if d.size < 2:
        return {"n": int(d.size), "mean": NAN, "lo": NAN, "hi": NAN,
                "p_gt0": NAN}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d.size, size=(resamples, d.size))
    stats = stat(d[idx], axis=1)
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return {"n": int(d.size), "mean": float(stat(d)), "lo": float(lo),
            "hi": float(hi), "p_gt0": float(np.mean(stats > 0))}


def per_minute(n, span_ms):
    return float(n) / max(span_ms / 60000.0, 1e-9)


# -------------------------------------------------------------- the clock

class Clock:
    """A clip's row clock: ``t0_ms`` + ``rows`` at ``row_hz``, the held-out
    ``val`` mask and the ``shots`` edges on it. This is the cache's own
    grid, so a score here reads the rows jepa_infer scored."""

    def __init__(self, t0_ms, rows, row_hz, val=None, shots=None):
        self.t0_ms = float(t0_ms)
        self.rows = int(rows)
        self.row_hz = float(row_hz)
        self.times = self.t0_ms + np.arange(self.rows) * 1000.0 / self.row_hz
        self.val = np.ones(self.rows, dtype=bool) if val is None \
            else np.asarray(val, dtype=bool)
        self.shots = list(shots) if shots else [0, self.rows]

    def row_of(self, t_ms):
        """Nearest row of each time, clipped into the clip."""
        r = np.rint((np.asarray(t_ms, dtype=float) - self.t0_ms)
                    * self.row_hz / 1000.0).astype(int)
        return np.clip(r, 0, self.rows - 1)

    def in_val(self, t_ms):
        return self.val[self.row_of(t_ms)]

    def stamp(self):
        a, b = runs_of(self.val)
        return {"t0_ms": self.t0_ms, "rows": self.rows,
                "row_hz": self.row_hz,
                "val": [[int(x), int(y)] for x, y in zip(a, b)],
                "shots": [int(s) for s in self.shots]}

    @classmethod
    def from_stamp(cls, s):
        val = np.zeros(int(s["rows"]), dtype=bool)
        for a, b in s.get("val", []):
            val[a:b] = True
        return cls(s["t0_ms"], s["rows"], s["row_hz"], val, s.get("shots"))

    @classmethod
    def from_times(cls, times_ms, val=None, shots=None):
        """From a cache's own ``times_ms``: the grid must be uniform, or a
        stamp of (t0, rows, hz) would not reproduce it."""
        times_ms = np.asarray(times_ms, dtype=np.float64)
        hz = common.row_hz_of(times_ms)
        c = cls(times_ms[0], len(times_ms), hz, val, shots)
        if len(times_ms) > 1 and \
                float(np.abs(c.times - times_ms).max()) > 1.0:
            raise SystemExit("row times are not a uniform grid; the clock "
                             "cannot be stamped as (t0, rows, hz)")
        return c

    @classmethod
    def from_cache(cls, dataset, vid, feat_dir, val_frac=1.0):
        """From the latent cache on disk, whole clip or the ``val_frac``
        spread split, with shot edges from the boundaries file."""
        path = Path(dataset) / feat_dir / f"{vid}.npz"
        times = np.load(path)["times_ms"].astype(np.float64)
        T = len(times)
        hz = common.row_hz_of(times)
        val = common.val_mask(T, common.val_regions(T, hz, val_frac))
        bpath = Path(dataset) / "boundaries" / f"{vid}.json"
        cuts = json.loads(bpath.read_text(encoding="utf-8"))["cuts_ms"] \
            if bpath.exists() else []
        cut_idx = np.searchsorted(times, cuts)
        shots = [0] + [int(c) for c in cut_idx if 0 < c < T] + [T]
        return cls.from_times(times, val, shots)


# ------------------------------------------ the row-grid reversal reading

def extrema(track, shot_edges, val, row_hz):
    """(row indices, prominence weights) of a position track's reversals on
    ``val`` rows: per-shot sign flips of the ``EXTREMUM_SMOOTH_S``-boxed
    derivative, weighted by mean two-sided prominence. THE extremum
    definition every row-grid reversal number in the tree reads."""
    idxs, wts = [], []
    k = common.rows_at(common.EXTREMUM_SMOOTH_S, row_hz, odd=True)
    for lo, hi in zip(shot_edges[:-1], shot_edges[1:]):
        seg = track[lo:hi]
        # a shot shorter than the smoothing kernel carries no reversal AT
        # THIS SCALE, and np.convolve(mode="same") would return more
        # samples than the shot holds
        if len(seg) < k:
            continue
        s = box(seg, k)
        flips, _ = flip_indices(s)
        if not len(flips):
            continue
        ext = np.concatenate(([0], flips, [len(seg) - 1]))
        for j in range(1, len(ext) - 1):
            i = int(ext[j])
            if not val[lo + i]:
                continue
            idxs.append(lo + i)
            wts.append((abs(s[i] - s[ext[j - 1]])
                        + abs(s[ext[j + 1]] - s[i])) / 2)
    return np.asarray(idxs), np.asarray(wts, dtype=np.float64)


def reversal_kappa(p, tgt_pos, shot_edges, val, row_hz, tol=None):
    """Amplitude-weighted reversal-timing agreement, chance-corrected.

    A predicted reversal within ``tol`` rows of a target one is a hit
    (greedy nearest match, loudest targets first). ``tol`` defaults to
    ``REV_TOL_S`` on this grid, so the slack and the chance correction
    built from it mean the same wall-clock on every grid. Kappa subtracts
    the F1 a uniformly placed predictor of the same density would score,
    so density alone earns nothing. Returns (kappa, precision, recall)."""
    tol = common.rows_at(common.REV_TOL_S, row_hz) if tol is None else tol
    ti, tw = extrema(tgt_pos, shot_edges, val, row_hz)
    pi, pw = extrema(p, shot_edges, val, row_hz)
    if not len(ti) or not len(pi):
        return NAN, NAN, NAN
    used = np.zeros(len(pi), dtype=bool)
    hit_t = np.zeros(len(ti), dtype=bool)
    hit_p = np.zeros(len(pi), dtype=bool)
    for j in np.argsort(-tw):
        d = np.abs(pi - ti[j]).astype(np.float64)
        d[used] = tol + 1
        k = int(np.argmin(d))
        if d[k] <= tol:
            used[k] = hit_t[j] = hit_p[k] = True
    rec = float((tw * hit_t).sum() / max(tw.sum(), 1e-9))
    prec = float((pw * hit_p).sum() / max(pw.sum(), 1e-9))
    f1 = 2 * prec * rec / max(prec + rec, 1e-9)
    win = (2 * tol + 1) / max(int(val.sum()), 1)
    rc, pc = min(1.0, len(pi) * win), min(1.0, len(ti) * win)
    f1c = 2 * pc * rc / max(pc + rc, 1e-9)
    return (f1 - f1c) / max(1 - f1c, 1e-9), prec, rec


# ------------------------------------------------------- artifact scorers

def _overlap(script, draft):
    t0 = max(script.clean[0][0], draft[0][0])
    t1 = min(script.clean[-1][0], draft[-1][0])
    return t0, t1


def score_position(script, draft, clock, with_gaps=False):
    """Position-domain read of a written draft on the clip's row clock:
    corr, MAE, travel ratio, reversal kappa with its precision and recall,
    over held-out, scripted rows inside the span both sides cover."""
    if not script.clean or not draft:
        return None
    t0, t1 = _overlap(script, draft)
    if t1 - t0 < 10_000:
        return None
    t = clock.times
    m = clock.val & (t >= t0) & (t <= t1)
    keep = np.ones(clock.rows, dtype=bool)
    if not with_gaps:
        for g0, g1 in script.gaps:
            keep[(t >= g0) & (t < g1)] = False
    m &= keep
    if int(m.sum()) < common.rows_at(2.0, clock.row_hz):
        return None
    ps = np.asarray(common.funscript_position(script.clean, t), dtype=float)
    pd = np.asarray(common.funscript_position(draft, t), dtype=float)
    seg = m[1:] & m[:-1]
    kap, prec, rec = reversal_kappa(pd, ps, clock.shots, m, clock.row_hz)
    ts = float(np.abs(np.diff(ps))[seg].sum())
    td = float(np.abs(np.diff(pd))[seg].sum())
    return {"rows": int(m.sum()), "minutes": float(m.sum()) / clock.row_hz / 60.0,
            "gap": float(1.0 - keep[clock.val & (t >= t0) & (t <= t1)].mean())
            if (clock.val & (t >= t0) & (t <= t1)).any() else 0.0,
            "corr": corr(pd[m], ps[m]),
            "mae": float(np.abs(pd[m] - ps[m]).mean()),
            "travel": td / max(ts, 1e-9), "kappa": kap,
            "rev_prec": prec, "rev_rec": rec,
            "actions": len(draft),
            "actions_per_s": len(draft) / max((t1 - t0) / 1000.0, 1e-9)}


def score_timing(script, draft, clock, with_gaps=False):
    """Continuous-ms reversal timing of a written draft against the
    script, at both tolerances: recall, precision, the mean matched |dt|,
    the exact-stamp matched |dt| p50/p95, and recall per script
    stroke-frequency band. Both sides read on held-out, scripted spans."""
    if not script.clean or not draft:
        return None
    t0, t1 = _overlap(script, draft)
    if t1 - t0 < 10_000:
        return None
    gaps = [] if with_gaps else script.gaps

    def keep_t(times):
        return scripted_mask(times, gaps) & clock.in_val(times)

    rt, rp, rprom = track_reversals(script.clean, t0, t1)
    ht, hp, _ = track_reversals(draft, t0, t1)
    hz = local_stroke_hz(rt)
    keep = (rprom >= AMP_MIN) & keep_t(rt)
    hm = keep_t(ht)
    ht, hp = ht[hm], hp[hm]
    xt, xp, xprom = action_reversals(script.clean, t0, t1)
    xkeep = (xprom >= AMP_MIN) & keep_t(xt)
    yt, yp, _ = action_reversals(draft, t0, t1)
    ym = keep_t(yt)
    yt, yp = yt[ym], yp[ym]
    gap_ms = sum(max(0.0, min(g1, t1) - max(g0, t0)) for g0, g1 in gaps)
    out = {"script_rev": int(keep.sum()), "draft_rev": int(len(ht)),
           "scripted_minutes": (t1 - t0 - gap_ms) / 60000.0}
    if not keep.any():
        return None
    for tol, key in zip(TOLS_MS, TOL_KEYS):
        hit, dt, used = match(rt[keep], rp[keep], ht, hp, tol)
        _h, xdt, _u = match(xt[xkeep], xp[xkeep], yt, yp, tol)
        adt = np.abs(xdt[np.isfinite(xdt)])
        row = {"recall": float(hit.mean()),
               "prec": float(used.sum() / max(len(ht), 1)),
               "dt_ms": float(np.nanmedian(np.abs(dt)))
               if np.isfinite(dt).any() else NAN,
               "p50": float(np.median(adt)) if len(adt) else NAN,
               "p95": float(np.percentile(adt, 95)) if len(adt) else NAN,
               "matched": int(len(adt)),
               # every matched offset, so a pooled percentile over the
               # tree can be taken over every reversal rather than over
               # per-clip medians
               "adt": [round(float(x), 2) for x in adt]}
        for name, lo, hi in FREQ_BANDS:
            sel = keep & (hz >= lo) & (hz < hi)
            n = int(sel.sum())
            if n < 5:
                row[name] = {"recall": NAN, "n": n}
                continue
            h2, _d2, _u2 = match(rt[sel], rp[sel], ht, hp, tol)
            row[name] = {"recall": float(h2.mean()), "n": n}
        out[key] = row
    return out


class SpeedReference:
    """The script-anchored speed reference every arm is read against:
    the script's local speed on a fine grid, its fast windows and its slow
    band. It depends on the script alone, so no arm can move it."""

    def __init__(self, script, clock=None, ref_hz=SPEED["ref_hz"],
                 smooth_s=SPEED["smooth_s"], fast_hz=SPEED["fast_hz"],
                 floor=SPEED["floor"], win_s=SPEED["stub_win_s"]):
        st, sp = script.t, script.p
        self.ref_hz, self.fast_hz, self.floor = ref_hz, fast_hz, floor
        self.grid = np.arange(st[0], st[-1], 1000.0 / ref_hz)
        S = np.interp(self.grid, st, sp)
        k = common.rows_at(smooth_s, ref_hz, odd=True)
        self.speed = box(np.abs(np.diff(S, prepend=S[:1])) * ref_hz, k,
                         pad="edge")
        # held-out membership on the reference grid: a corpus clip reads
        # its desupervised rows, an OOS clip the whole clip
        self.keep = clock.in_val(self.grid) if clock is not None \
            else np.ones(len(self.grid), dtype=bool)
        self.minutes = float(self.keep.sum()) / ref_hz / 60.0
        moving = (self.speed > floor) & self.keep
        if moving.any():
            self.q1 = float(np.percentile(self.speed[moving], 33.3))
            self.slow = moving & (self.speed <= self.q1)
        else:
            self.q1, self.slow = NAN, np.zeros_like(moving)
        self.slow_min = float(self.slow.sum()) / ref_hz / 60.0
        self.wins = self._fast_windows(st, sp, win_s * 1000.0, clock)
        # the script's own half-stroke distributions on the kept, scripted
        # rows, and their IQRs: the shape columns read a draft against them
        self.gaps = script.gaps
        mid, _amp, spd, dt = half_strokes(st, sp)
        sk = (np.interp(mid, self.grid, self.keep.astype(np.float64)) > 0.5) \
            & scripted_mask(mid, self.gaps)
        self.spd_ref, self.dt_ref = spd[sk], dt[sk] * 1000.0
        self.spd_iqr, self.dt_iqr = _iqr(self.spd_ref), _iqr(self.dt_ref)
        # the styled mask on the reference grid: every script half-stroke
        # of AMP_MIN or more above STYLE_X times the local speed it sits
        # in, dilated STYLE_DILATE_S each side. Below the device floor the
        # ratio is noise, as it is for a draft stroke
        loc = np.interp(mid, self.grid, self.speed)
        own = (loc > floor) & (spd > STYLE_X * loc) & (_amp >= AMP_MIN)
        edge = np.zeros(len(self.grid) + 1, dtype=np.int64)
        if own.any():
            r = STYLE_DILATE_S * 1000.0
            np.add.at(edge, np.searchsorted(self.grid, mid[own] - r), 1)
            np.add.at(edge, np.searchsorted(self.grid, mid[own] + r,
                                            side="right"), -1)
        self.styled = np.cumsum(edge)[:-1] > 0

    def _fast_windows(self, st, sp, win_ms, clock):
        mid, amp, _spd, dt = half_strokes(st, sp)
        fast = dt <= 1.0 / (2.0 * self.fast_hz)
        wins = []
        for w0 in np.arange(st[0], st[-1], win_ms):
            if clock is not None and not clock.in_val(w0 + win_ms / 2):
                continue
            m = fast & (mid >= w0) & (mid < w0 + win_ms)
            if m.sum() >= STUB_MIN_FAST:
                wins.append((w0, w0 + win_ms, float(np.median(amp[m])),
                             int(m.sum())))
        return wins


def score_speed(t, p, ref, amp_thr=SPEED["amp"]):
    """Every speed column for ONE written action list (times_ms, pos
    arrays) against a ``SpeedReference``: per minute of held-out time,
    the slow-section columns per minute of slow-section time."""
    t = np.asarray(t, dtype=np.float64)
    p = np.asarray(p, dtype=np.float64)
    mins = max(ref.minutes, 1e-9)
    mid, amp, spd, dt = half_strokes(t, p)

    def _in(x):
        return np.interp(x, ref.grid, ref.keep.astype(np.float64)) > 0.5

    kp = _in(mid)
    local = np.interp(mid, ref.grid, ref.speed)
    ok = kp & (local > ref.floor)     # below the device floor the ratio is noise
    r = spd[ok] / np.maximum(local[ok], 1e-9)
    fast = kp & (dt <= 1.0 / (2.0 * ref.fast_hz))
    n_fast = float(fast.sum())
    tk = _in(0.5 * (t[1:] + t[:-1]))
    step = np.abs(np.diff(p))[tk]
    trans = step * 1000.0 / np.maximum(np.diff(t), 1e-9)[tk]
    sl = (np.interp(mid, ref.grid, ref.slow.astype(np.float64)) > 0.5) & kp
    rs = spd[sl] / np.maximum(local[sl], 1e-9)
    styled = (np.interp(mid, ref.grid, ref.styled.astype(np.float64))
              > 0.5)[sl]
    smin = max(ref.slow_min, 1e-9)
    stub, brok = _stub_scan(mid, amp, ref.wins)
    sk = kp & scripted_mask(mid, ref.gaps)
    spd_w = w1(spd[sk], ref.spd_ref) / ref.spd_iqr \
        if sk.any() and np.isfinite(ref.spd_iqr) else NAN
    gap_w = w1(dt[sk] * 1000.0, ref.dt_ref) / ref.dt_iqr \
        if sk.any() and np.isfinite(ref.dt_iqr) else NAN
    return {
        "spdW": float(spd_w), "gapW": float(gap_w),
        "fast": n_fast / mins,
        "amp20": float((fast & (amp >= amp_thr)).sum()) / mins,
        "x2": float(np.sum(r > 2)) / mins,
        "x3": float(np.sum(r > 3)) / mins,
        "per": float(np.sum(r > 2)) / n_fast if n_fast else NAN,
        "p99": float(np.percentile(r, 99)) if len(r) else NAN,
        "over600": int(np.sum(trans > common.MAX_POS_RATE)),
        "over_min": float(np.sum(trans > common.MAX_POS_RATE)) / mins,
        "peak": float(trans.max()) if len(trans) else 0.0,
        "sl2x": float(np.sum(rs > 2)) / smin,
        "sl3x": float(np.sum(rs > 3)) / smin,
        "sl2x_styled": float(np.sum((rs > 2) & styled)) / smin,
        "sl2x_smooth": float(np.sum((rs > 2) & ~styled)) / smin,
        "sl3x_styled": float(np.sum((rs > 3) & styled)) / smin,
        "sl3x_smooth": float(np.sum((rs > 3) & ~styled)) / smin,
        "slpeak": float(spd[sl].max()) if sl.any() else 0.0,
        "stub": 100.0 * stub, "brok": brok,
        "travel": float(step.sum()),
        "minutes": mins, "slow_min": ref.slow_min,
        "fast_windows": len(ref.wins),
    }


def _stub_scan(mid, amp, wins):
    """(stub share, broken windows per minute of fast-window time) over the
    script's fast windows."""
    if not wins:
        return NAN, NAN
    n_str = n_stub = n_brok = 0
    for w0, w1, sa, n_fast in wins:
        m = (mid >= w0) & (mid < w1)
        n = int(m.sum())
        stubs = int((amp[m] < STUB_RATIO * sa).sum())
        n_str += n
        n_stub += stubs
        if (n and stubs / n > STUB_BROKEN) or n < 0.5 * n_fast:
            n_brok += 1
    fast_min = len(wins) * (wins[0][1] - wins[0][0]) / 60000.0
    return (n_stub / n_str if n_str else NAN, n_brok / max(fast_min, 1e-9))


def score_artifact(script, actions, clock, with_gaps=False, ref=None):
    """The whole artifact read of one written action list: position,
    timing and speed, plus the script's own speed row. ``ref`` reuses a
    ``SpeedReference`` built for this clip and clock."""
    draft = load_draft(actions, script.dur)
    if not draft:
        return None
    ref = ref or SpeedReference(script, clock)
    sp = score_speed(script.t, script.p, ref)
    return {"position": score_position(script, draft, clock, with_gaps),
            "timing": score_timing(script, draft, clock, with_gaps),
            "speed": score_speed(np.array([a[0] for a in draft]),
                                 np.array([a[1] for a in draft]), ref),
            "script_speed": sp,
            "lag_ms": script.lag}


# ---------------------------------------------------------------- pooling

def _tail_dict(vals, worst_high):
    p, t = pool_tail(vals, worst_high)
    return {"pooled": p, "tail": t}


ARTIFACT_BLOCKS = ("position", "timing", "speed", "script_speed")


def pool_arm(clips, ids, style="composed"):
    """The arm's pooled block from its per-clip ``artifact`` (and
    ``track``) records over ``ids``: position with its worst-clip tail,
    timing at both tolerances with the worst-clip companions, the speed
    columns at equal weight, the artifact floor by slow minute, and the
    product panel of the ``style`` that wrote the draft."""
    ids = [i for i in ids if i in clips and clips[i].get("artifact")]
    # a block a clip could not read is absent, whether the record holds
    # it as None in memory or as null on disk
    art = {i: {k: (v if isinstance(v, dict) or k not in ARTIFACT_BLOCKS
                   else None)
               for k, v in clips[i]["artifact"].items()} for i in ids}
    out = {"n": len(ids), "ids": ids}

    pos = {}
    for key, worst_high in POSITION_COLS:
        vals = {i: (art[i].get("position") or {}).get(key, NAN) for i in ids}
        pos[key] = _tail_dict([vals[i] for i in ids], worst_high)
    out["position"] = pos

    timing = {}
    for key in TOL_KEYS:
        rows = [(i, art[i]["timing"][key]) for i in ids
                if art[i].get("timing") and key in art[i]["timing"]]
        if not rows:
            continue
        rec = [r["recall"] for _, r in rows]
        prec = [r["prec"] for _, r in rows]
        dt = [r["dt_ms"] for _, r in rows]
        alld = np.concatenate([np.asarray(r.get("adt", []), dtype=float)
                               for _, r in rows]) if rows else np.zeros(0)
        worst_p95 = max(rows, key=lambda ir: np.nan_to_num(ir[1]["p95"],
                                                            nan=-1.0))
        worst_prec = min(rows, key=lambda ir: ir[1]["prec"])
        t = {"recall": float(np.nanmean(rec)),
             "recall_tail": pool_tail(rec)[1],
             "prec": float(np.nanmean(prec)),
             "prec_tail": worst_prec[1]["prec"],
             "prec_tail_id": worst_prec[0],
             "dt_ms": float(np.nanmean(dt)),
             "p50": float(np.median(alld)) if len(alld) else NAN,
             "p95": float(np.percentile(alld, 95)) if len(alld) else NAN,
             "p95_tail": worst_p95[1]["p95"],
             "p95_tail_id": worst_p95[0]}
        for name, _lo, _hi in FREQ_BANDS:
            vals = [r[name]["recall"] for _, r in rows]
            ns = [r[name]["n"] for _, r in rows]
            hit = sum(v * n for v, n in zip(vals, ns) if math.isfinite(v))
            den = sum(n for v, n in zip(vals, ns) if math.isfinite(v))
            t[name] = {"recall": float(np.nanmean(vals))
                       if np.isfinite(vals).any() else NAN,
                       "recall_pooled": hit / den if den else NAN,
                       "n": int(sum(ns))}
        timing[key] = t
    out["timing"] = timing

    sp_rows = [art[i]["speed"] for i in ids if art[i].get("speed")]
    sc_rows = [art[i]["script_speed"] for i in ids
               if art[i].get("script_speed")]
    out["speed"] = {c: float(np.nanmean([r.get(c, NAN) for r in sp_rows]))
                    for c in SPEED_COLS} if sp_rows else {}
    out["script_speed"] = {c: float(np.nanmean([r.get(c, NAN)
                                                for r in sc_rows]))
                           for c in SPEED_COLS} if sc_rows else {}
    # the shape columns' worst-2 companion: a distance's tail is a clip
    # whose strokes are all crawls or all sprints
    out["speed_tail"] = {c: pool_tail([r.get(c, NAN) for r in sp_rows],
                                      worst_high=True)[1]
                         for c in SHAPE_COLS} if sp_rows else {}
    out["artifact"] = artifact_floor(art, ids)

    panel = {}
    for key, lab, worst_high in PANEL_COLS:
        vals = [track_block(clips[i], style).get(key) for i in ids]
        if any(v is not None for v in vals):
            p, t = pool_tail(vals, worst_high)
            panel[lab] = {"pooled": p, "tail": t}
    out["panel"] = panel
    return out


def artifact_floor(art, ids):
    """The slow-section rates pooled by slow minute, delivery against the
    script and the per-delivered-stroke ratios."""
    rows = {i: art[i]["speed"] for i in ids if art[i].get("speed")}
    srows = {i: art[i]["script_speed"] for i in ids
             if art[i].get("script_speed")}
    if not rows:
        return {}
    allr = list(rows.values())
    fast = slow_minute_pool(allr, "fast")
    sfast = slow_minute_pool(list(srows.values()), "fast") if srows else NAN
    x2 = slow_minute_pool(allr, "x2")
    x3 = slow_minute_pool(allr, "x3")
    return {"sl3x": slow_minute_pool(allr, "sl3x"),
            "sl2x": slow_minute_pool(allr, "sl2x"),
            # the two halves of each rate, pooled the same way; the smooth
            # half's worst-2 is the companion tail, because a clean pooled
            # rate can still hide one clip written fast on a smooth script
            "sl3x_styled": slow_minute_pool(allr, "sl3x_styled"),
            "sl3x_smooth": slow_minute_pool(allr, "sl3x_smooth"),
            "sl2x_styled": slow_minute_pool(allr, "sl2x_styled"),
            "sl2x_smooth": slow_minute_pool(allr, "sl2x_smooth"),
            "sl3x_smooth_tail": pool_tail([r.get("sl3x_smooth") for r in allr],
                                          worst_high=True)[1],
            "slpeak": max(r["slpeak"] for r in allr),
            "fast": fast, "script_fast": sfast,
            "x_script": fast / sfast if sfast else NAN,
            "x2_per_stroke": x2 / fast if fast else NAN,
            "x3_per_stroke": x3 / fast if fast else NAN,
            "slow_min": float(sum(r["slow_min"] for r in allr))}


# -------------------------------------------------------------- the record

def json_safe(obj):
    """NaN/inf -> None and numpy scalars -> Python, recursively, so the
    record is JSON any reader parses."""
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return float(obj) if math.isfinite(float(obj)) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return json_safe(obj.tolist())
    return obj


def _nan_in(obj):
    """None -> NaN in numeric leaves, so arithmetic on a read record never
    meets a None."""
    if isinstance(obj, dict):
        return {k: _nan_in(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nan_in(v) for v in obj]
    return NAN if obj is None else obj


def record_path(arm_dir):
    return Path(arm_dir) / "metrics.json"


def read_record(arm_dir):
    """The arm's record, or None. Numeric ``null`` comes back as NaN."""
    p = record_path(arm_dir)
    if not p.exists():
        return None
    return _nan_in(json.loads(p.read_text(encoding="utf-8")))


def clips_digest(clips):
    """One digest of a record's per-clip block as JSON holds it."""
    return hashlib.sha256(json.dumps(json_safe(clips), sort_keys=True)
                          .encode("utf-8")).hexdigest()[:16]


def record_digest(arm_dir):
    """One digest of the record's scored content: its per-clip block as
    the file holds it, so a verdict saved beside a record can be bound to
    the clips it was computed from and refused over any other."""
    p = record_path(arm_dir)
    return clips_digest(json.loads(p.read_text(encoding="utf-8"))
                        .get("clips", {}))


def write_record(arm_dir, rec):
    p = record_path(arm_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(json_safe(rec), indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def track_block(clip_rec, style="composed"):
    """The grid-track panel of one clip's record (records written before
    the track block carry it under the styling's printed name)."""
    return ((clip_rec.get("track") or {}).get(style)
            or clip_rec.get(f"{style} styling") or {})


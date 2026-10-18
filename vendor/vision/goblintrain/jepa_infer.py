"""jepa checkpoint -> .funscript draft + position-domain evaluation.

Owns the whole position path: the model's marginal velocity gives reversal
TIMES (zero crossings, never relaxed), the position head gives stroke
LEVEL, and the generated envelope gives stroke AMPLITUDE -- composed
styling, the production synthesis. Output artifact:
``<out>/<ID>_jepa.funscript``.

Metrics per synthesis: position corr (concat + per-shot mean), MAE,
extreme-band MAE, band recall at >=80 / <=20, and the TRAVEL RATIO
(styled |dp| sum / target's) -- position corr/MAE structurally reward
amplitude hedging, so travel and the user's eyeball decide promotions,
never corr/MAE alone. Second line per synthesis: chance-corrected
amplitude-weighted reversal-timing kappa (+-1 frame -- the product
objective, scale-free), stillness precision/recall (phantom strokes
during target stillness lower recall), and the envelope calibration
slope per target-intensity tercile (hedging localizes as hi << 1).
Third line: dwell response -- of the target's >=0.5 s trapezoid dwells in
the val region, the share the draft PARKS at (local mean inside the
dwell's tolerance) vs TRAVERSES (sweeps half the adjoining stroke: the
plateau's duration is deleted and reversed as a triangle apex), plus the
TEXTURE ratio (the draft's ripple over the script's -- a dwell is a level
regime, not stillness, and flattening one is a failure, not a hold) and
the parked LEVEL error. No other metric sees dwell deletion.

``POS_TEMP`` sharpens the level/rail expectation decode (the collapse
compresses extremes; 0.25 is the measured operating point).

Usage:
    python jepa_infer.py --dataset projects/mine --ids holdout --out drafts
"""
import bisect
import argparse
import json
from pathlib import Path

import numpy as np

from . import common
from . import h0_store
from . import scoring
from .forward import predict_shots
from .jepa_train import JepaClip, load_model

FPS = 15.0         # the loaded cache's row rate; main() rebinds per clip
DT = 1.0 / FPS
REV_SOURCE = "viterbi"  # the deploy decode's reversal segmentation; the ONE
                        # definition -- jepa_infer's argparse default and the
                        # bundle manifest (goblinscript) both read it. The
                        # alternating event decode over the rev head delivers
                        # the fast band at the rate the head's own posterior
                        # is calibrated to; zero crossings of the marginal
                        # drop one fast reversal in eight that the model
                        # already found
REV_GAP_PRIOR = True   # fit the viterbi emission prior separately on STILL
                       # and MOVING rows. Same ONE-definition rule as
                       # REV_SOURCE: argparse and export_bundle both read it.
                       # Only reachable on rev_source "viterbi". A single
                       # global fit takes its count target from the head's
                       # sum over the WHOLE clip, unscripted gaps included,
                       # and the refractory pins the fast band -- so the
                       # surplus lands in slow sections as spikes. Splitting
                       # the fit keeps gap mass out of the moving prior
STILL_EPS = 22.0   # pos/s: the composed-styling stillness gate, and the ONE
                   # definition -- jepa_infer's argparse default and
                   # export_bundle's both read it. Target holds read ~11
                   # pos/s against ~70 moving. 22 is the floor the gap-prior
                   # graft is scored at: it holds the slow sections the
                   # faithful event rate now writes into, while 30 collapses
                   # the correlation tail
EXT_SNAP = 0.0     # pos units: endpoints this close to a predicted band
                   # edge stretch to it (composed styling, ext-head only).
                   # 0 = the stretch is OFF in the deploy decode: at 20 it
                   # wrote slow-section strokes at 2x+ the authored speed
                   # (amplitude inflated at unchanged duration -- the
                   # queue-8 spike class), buying band recall the user
                   # priced below the artifact. The flag stays for
                   # measurement; the band rails still feed the dwell lock
AMP_CAP_X = 4.0    # x the marginal's own travel: the bound on a stroke's
                   # written excursion (composed styling). The deploy dose;
                   # the bundle manifest carries it and goblinscript reads
                   # it there. 0 = off.
# The dwell-lock operating point, and the ONE definition -- jepa_infer's
# argparse defaults and the bundle manifest both read these. They are a
# per-CHECKPOINT calibration, not physics: they threshold the dwell
# head's PUBLISHED posterior, so they move whenever that head's prior
# fold changes. Cut for the checkpoint whose head folds its
# inverse-sqrt class prior out (2026-08-13); the pre-fold rebalanced
# scale wanted 0.5 / 0.3 / 0.65, which is the same operating point in
# the units that head published.
PLAT_THR = 0.25    # probability that SEEDS a dwell
PLAT_LO = 0.15     # hysteresis floor a seeded dwell extends through
PLAT_PEAK = 0.45   # consolidated calls below this peak are dropped
PLAT_SOFT = (0.5, 1.0)  # the dwell lock's confidence ramp, and the ONE
                   # definition -- jepa_infer's argparse default and
                   # export_bundle's both read it. START and TOP are separate
                   # axes and each is measured on its own: the top belongs at
                   # 1.0, because ending the ramp early spends reversal timing
                   # for nothing (kappa -0.0135 pooled at 0.85, -0.0173 at
                   # 0.75, at the same corr), while the start trades corr
                   # against kappa and 0.5 is where corr wins 19 clips of 24
                   # with kappa still even. Both tails improve there, so the
                   # worst clips are not paying for the middle ones

# Durations, in SECONDS, restated on the live grid via ``common.rows_at``
# where they are used -- like the shared ``*_S`` constants in common.py. A
# bare row literal would mean whatever the grid made of it.
DWELL_GAP_S = 0.266667        # dwell-call hole-fill
DWELL_MIN_CALL_S = 0.266667   # shortest surviving dwell call
VETO_FLANK_S = 2.0            # stroke-veto flank window
STILL_SMOOTH_S = 1.0          # stillness-metric |v| box
STILL_TGT_EPS = 5.0           # pos/s: a target row under this, box-smoothed,
                              # is STILL (the stillness metric and the hold
                              # oracle share the definition)
SPEED_REF_S = 0.6             # harness local-speed reference
MIN_SPAN_S = 2.0              # shortest span metrics admit
BIAS_FIT_S = 8000.0           # emission-prior bias-fit slice cap
RDP_EPS = 1.0                 # pos units: Douglas-Peucker eps on the WRITTEN
                              # action list. Not a duration -- it is here
                              # because style.rs carries its own copy, and
                              # grid_check binds the two
POS_TEMP = 0.25               # level-decode temperature (<1 sharpens the
                              # expectation decode, which compresses level
                              # extremes)
PLAT_VETO = 15.0              # pos/s: a consolidated dwell call whose both
                              # ~2 s flanks read below this mean predicted
                              # |vmarg| is dropped
PLAT_SHIFT_CAP = 25.0         # pos units: the cap on a dwell lock's mean
                              # correction
SUBFRAME = "rev"              # sub-frame reversal times off the rev head
CUT_EASE = 250.0              # pos/s: the seam rate at shot cuts -- above
                              # it the forced vertices drop (ease) and the
                              # written speed at the seam is clamped to it
                              # by depth (slew). Same footing as RDP_EPS --
                              # artifact layer, mirrored in style.rs,
                              # bound by grid_check


# The DECODE's crossing smoother (--rev-smooth-s rebinds it in main). Its
# width bounds the fastest reversal the action writer can see -- a 3.75 Hz
# half-stroke spans 133 ms, so a 0.233 s box erases it. Metrics (_extrema)
# stay on common.EXTREMUM_SMOOTH_S so arms score alike whatever this is set to.
REV_SMOOTH_RUN_S = common.REV_SMOOTH_S
REV_SUPPORT_S = 0.0667        # the reach around a called apex the phase
#                               reading tests the crossing in, the scorer's
#                               own reversal tolerance
PHASE_RUN_K = 3               # the run length of the run-share phase reading
#                               the written funscript records


corr = scoring.corr        # THE correlation every panel number reads


def _runs(m):
    """Row spans where boolean ``m`` is True -> [(a, b)], b exclusive."""
    d = np.diff(np.concatenate(([0], m.view(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def dwell_kind(plat, thr, thr_lo=PLAT_LO, gap=None, min_rows=None, peak=0.0):
    """Dwell-head tracks -> per-row predicted dwell kind in {0, +1, -1}.

    The head's raw calls are FRAGMENTARY: it fires somewhere inside ~3/4
    of script dwells but covers half of only ~2/3 of them. That is the
    worst case for the level lock, which pays for partial coverage -- the
    step between locked and free track adds excursion -- so the call is
    consolidated into whole dwells
    before the decode sees it, not merely thresholded: a run is SEEDED
    where the probability clears ``thr`` and extends while it stays above
    ``thr_lo`` (hysteresis). A plateau the head is sure about in the
    middle and unsure about at the corners comes out whole.

    ``thr_lo`` is the operating knob and the whole coverage/precision
    trade lives on it -- 0.3 is the eyeballed optimum (0.2 wins the dwell
    metric outright and reads WORSE: past a point extra parking is false
    holds freezing strokes that should move). ``gap`` (short holes filled)
    is measured INERT -- 2/4/8 are identical, because hysteresis already
    bridges every dip that stays above the floor.

    Runs shorter than ``min_rows`` are dropped (a dwell briefer than
    ~0.25 s is indistinguishable from a normal reversal), and where both
    kinds survive on a row the larger probability wins.

    ``peak`` (> ``thr`` to bite): a consolidated call whose probability
    never reaches it is dropped whole. False calls are LOW-peak (measured
    median 0.60-0.69 vs 0.76-0.83 for calls inside a real dwell), so the
    filter sheds misplaced rail pins while the dwell response stays put.
    0 = off."""
    gap = common.rows_at(DWELL_GAP_S, FPS) if gap is None else gap
    min_rows = common.rows_at(DWELL_MIN_CALL_S, FPS) \
        if min_rows is None else min_rows
    pt = np.nan_to_num(plat[0], nan=0.0)
    pb = np.nan_to_num(plat[1], nan=0.0)
    masks = []
    for p in (pt, pb):
        m = np.zeros(len(p), dtype=bool)
        for a, b in _runs(p >= thr_lo):        # hysteresis: seed then grow
            if (p[a:b] >= thr).any():
                m[a:b] = True
        for a, b in _runs(~m):                 # close short holes
            if a > 0 and b < len(m) and b - a <= gap:
                m[a:b] = True
        for a, b in _runs(m):
            if b - a < min_rows:
                m[a:b] = False
        masks.append(m)
    top, bot = masks
    kind = np.zeros(len(pt), dtype=np.int8)
    kind[top & (~bot | (pt >= pb))] = 1
    kind[bot & (~top | (pb > pt))] = -1
    if peak > 0:
        for k, p in ((1, pt), (-1, pb)):
            for a, b in _runs(kind == k):
                if p[a:b].max() < peak:
                    kind[a:b] = 0
    return kind


def stroke_veto(kind, vmarg, thr, flank=None):
    """Drop consolidated dwell calls that have no adjoining stroke.

    ``common.dwell_spans`` DEFINES a dwell by its adjoining stroke (its
    tolerance scales with it), but the head reads rows, not that guard,
    so it can lock stroke-free regions -- on unseen material it
    over-commits at low-motion intros/outros. A
    call is vetoed when the predicted |vmarg| of BOTH ~2 s flanks stays
    under ``thr`` (pos/s): nothing arrives at or leaves the plateau, so
    there is no plateau. A clip edge counts as a still flank -- that IS
    the intro/outro case. In-corpus dwells adjoin strokes (moving |v|
    ~107 pos/s vs ~13 during holds), so in-sample calls clear any
    threshold between those by a wide margin. |vmarg| as the
    motion-vs-still instrument is the queue-1 probe verdict (AUC ~0.78
    out of clip)."""
    flank = common.rows_at(VETO_FLANK_S, FPS) if flank is None else flank
    av = np.abs(np.nan_to_num(vmarg))
    out = kind.copy()
    d = np.diff(np.concatenate(([0], (kind != 0).view(np.int8), [0])))
    for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
        lo = av[max(0, a - flank):a]
        hi = av[b:b + flank]
        if max(lo.mean() if len(lo) else 0.0,
               hi.mean() if len(hi) else 0.0) < thr:
            out[a:b] = 0
    return out


def level_lock(p, kind, rail_lo, rail_hi, smooth=None, edge=None, plat=None,
               soft=None, rail_track=False, shift_cap=0.0):
    """Pin the styled track's LOCAL MEAN to the extremum-side rail inside
    each predicted dwell, leaving its residual ripple untouched.

    A dwell is a level regime, not a stillness one: 95% of scripted
    plateaus oscillate at the extreme, and the composed draft already
    generates about the right amount of that ripple (texture ratio ~1.0).
    What it gets wrong is residency -- its local mean sweeps through the
    plateau, deleting the dwell's duration as a triangle apex. So the fix
    is subtraction, not replacement: decompose ``p`` into local mean +
    residual, replace the mean with the rail level, add the residual back.
    The traverse lives in the mean and dies; the ripple lives in the
    residual and survives. (Overwriting the span with a constant would
    take the ripple with it -- and a range-based dwell metric would score
    that as a hold.)

    The rail carries the level: it predicts plateau levels within ~0-6
    units where the level head sits ~15 off (its ~2 s window is too coarse
    to speak for a 0.5 s plateau).

    The correction runs at FULL strength across the whole dwell and ramps
    in over ``edge`` free rows on either SIDE of it. The ramp has to live
    in the flanking strokes, not in the dwell: a plateau's corner rows are
    part of the plateau -- the script's own mean is already parked there --
    so tapering inside would leave exactly those rows sweeping. The rows
    the ramp borrows are the trapezoid's arriving/leaving edges, which is
    where a ramp belongs.

    Variant knobs (all default to the promoted behavior):
    ``rail_track`` follows the smoothed PER-ROW rail inside the dwell
    instead of pinning one constant level (long/drifting plateaus);
    ``soft=(p0, p1)`` scales the whole correction by the call's head-peak
    confidence, ramping 0->1 over [p0, p1] (false calls are separably
    low-peak -- the precision autopsy); ``shift_cap`` bounds the
    mean-correction magnitude in position units (the 900005 93-unit
    boundary artifact class). ``plat`` is the (top, bot) probability
    tuple, needed by ``soft``.

    ``smooth`` and ``edge`` are DURATIONS (7 and 3 rows at 15 rows/s), taken
    on this clip's row grid unless a caller pins them."""
    smooth = common.rows_at(common.LOCK_SMOOTH_S, FPS, odd=True) \
        if smooth is None else smooth
    edge = common.rows_at(common.LOCK_EDGE_S, FPS) if edge is None else edge
    m = common.local_mean(p, smooth)
    out = p.copy()
    free = kind == 0
    claimed = np.zeros(len(p), dtype=bool)      # ramp rows are taken once
    d = np.diff(np.concatenate(([0], (~free).view(np.int8), [0])))
    for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
        rail = rail_hi[a:b] if kind[a] > 0 else rail_lo[a:b]
        if rail_track:
            tgt_in = np.clip(common.local_mean(rail, smooth), 0.0, 100.0)
        else:
            tgt_in = np.full(b - a, np.clip(rail.mean(), 0.0, 100.0))
        lo = a
        while lo > 0 and a - lo < edge and free[lo - 1] and not claimed[lo - 1]:
            lo -= 1
        hi = b
        while hi < len(p) and hi - b < edge and free[hi] and not claimed[hi]:
            hi += 1
        claimed[lo:a] = claimed[b:hi] = True
        w = np.ones(hi - lo)
        if a > lo:      # w = 0 at the first borrowed row: no seam
            w[:a - lo] = np.linspace(0.0, 1.0, a - lo + 1)[:-1]
        if hi > b:
            w[b - lo:] = np.linspace(1.0, 0.0, hi - b + 1)[1:]
        target = np.empty(hi - lo)
        target[a - lo:b - lo] = tgt_in
        target[:a - lo] = tgt_in[0]        # ramp rows carry the corner level
        target[b - lo:] = tgt_in[-1]
        corr = target - m[lo:hi]
        if shift_cap > 0:
            corr = np.clip(corr, -shift_cap, shift_cap)
        if soft is not None and plat is not None:
            pk = float(np.nanmax((plat[0] if kind[a] > 0
                                  else plat[1])[a:b]))
            p0, p1 = soft
            corr = corr * np.clip((pk - p0) / max(p1 - p0, 1e-9), 0.0, 1.0)
        out[lo:hi] = np.clip(p[lo:hi] + w * corr, 0.0, 100.0)
    return out


def artifact_events(acts, prior):
    """Rarity events of a WRITTEN action list under the corpus stroke
    bigram (artifact_prior.json -- fit on authored scripts, grounded
    against the user's eye). An
    artifact is a non-sensical action SEQUENCE, i.e. one that almost
    never occurs in the training data, so the instrument is windowed
    bigram NLL over quantized stroke tokens with a threshold anchored
    on the authors' own rarity. Action-domain and reference-free: it
    scores the artifact layer the grid metrics cannot see, on any clip.
    An INSTRUMENT, never a gate: the panel prints events/min next to
    the authors' anchor rate; nothing is filtered or replaced.

    Returns ``(spans, per_min)``: hot spans in seconds and their rate
    per minute of scripted time (goblinscript ports this function
    unchanged from the same JSON)."""
    a = [(t / 1000.0, p) for t, p in acts]
    strokes = []
    i = 0
    while i + 1 < len(a):
        j = i + 1
        d0 = np.sign(a[j][1] - a[i][1])
        while j + 1 < len(a) and (np.sign(a[j + 1][1] - a[j][1]) == d0
                                  or a[j + 1][1] == a[j][1]):
            j += 1
        strokes.append((a[i][0], a[j][0] - a[i][0],
                        abs(a[j][1] - a[i][1])))
        i = j
    win = int(prior["win"])
    if len(strokes) < win + 1:
        return [], 0.0
    logp = np.asarray(prior["logp"])
    tok = (np.digitize([s[1] for s in strokes], prior["dur_edges_s"]) * 6
           + np.digitize([s[2] for s in strokes], prior["amp_edges"]))
    w = np.convolve(-logp[tok[:-1], tok[1:]],
                    np.ones(win) / win, "valid")
    t0 = np.array([s[0] for s in strokes])
    hot = w >= prior["thr"]
    spans = []
    i = 0
    while i < len(hot):
        if not hot[i]:
            i += 1
            continue
        j = i
        while j < len(hot) and hot[j]:
            j += 1
        spans.append((float(t0[i]),
                      float(t0[min(j + win, len(t0) - 1)])))
        i = j
    dur_min = (strokes[-1][0] + strokes[-1][1] - strokes[0][0]) / 60.0
    return spans, len(spans) / max(dur_min, 1e-9)


def subrow_offset(y, gl, half=1, clamp=0.5):
    """Sub-row placement of an event head's peak near row ``gl``, in ROWS:
    the 3-point parabola through the stencil ``half`` rows either side,
    moved at most ``clamp`` rows. Both are row counts, so both are
    DURATIONS on a given grid. A stencil that leaves the track, or a fit
    with no interior maximum, places the event on its row.
    """
    lo, hi = gl - half, gl + half
    if lo < 0 or hi >= len(y):
        return 0.0
    w = np.nan_to_num(np.asarray(y[lo:hi + 1], dtype=np.float64))
    y0, y1, y2 = w[half - 1], w[half], w[half + 1]
    den = y0 - 2 * y1 + y2
    dlt = 0.0 if abs(den) < 1e-9 else float(0.5 * (y0 - y2) / den)
    return float(np.clip(dlt, -clamp, clamp))


def amp_bound(end, cur, tr_raw, n_rows, dt, amp_cap_x):
    """The marginal's own travel bounds a stroke's written excursion.

    A segment of ``n_rows`` rows that the marginal integrates to ``tr_raw``
    position units may move the written position at most ``amp_cap_x``
    times that far, so the written speed stays under ``amp_cap_x`` times
    the speed the model reads on those rows. The slow-section spike column
    reads a written half-stroke against the SCRIPT's local speed; this is
    the same bound in the one quantity the decode has without a script.
    Depth only: no time moves and no reversal is deleted. ``amp_cap_x``
    0 is off. goblinscript's ``style::amp_bound`` is this rule word for
    word, and ``grid_check`` pins both to one fixture."""
    if amp_cap_x <= 0.0 or n_rows <= 0:
        return end
    lim_x = amp_cap_x * tr_raw
    if abs(end - cur) > lim_x:
        end = float(np.clip(cur + np.sign(end - cur) * lim_x, 0.0, 100.0))
    return end


def contradicting_runs(vrows, vkinds, sg, w, k):
    """The called apexes whose kind contradicts the smoothed marginal's
    sign track ``sg`` (a crossing of the opposite direction within ``w``
    rows of the apex row and none of its own direction), as the spans
    ``[i, j)`` of runs of at least ``k`` consecutive ones, with the
    marginal's peak rows and valley rows."""
    down = np.flatnonzero((sg[:-1] > 0) & (sg[1:] < 0))   # a peak's row
    up = np.flatnonzero((sg[:-1] < 0) & (sg[1:] > 0))     # a valley's row

    def near(c, f):
        return bool(len(c)) and bool(np.abs(c - f).min() <= w)
    contra = np.array([near(up if kd > 0 else down, f)
                       and not near(down if kd > 0 else up, f)
                       for f, kd in zip(vrows, vkinds)], dtype=bool)
    runs, i, m = [], 0, len(vrows)
    while i < m:
        if not contra[i]:
            i += 1
            continue
        j = i
        while j < m and contra[j]:
            j += 1
        if j - i >= k:
            runs.append((i, j))
        i = j
    return runs, down, up


def alternating(rows, kinds):
    """The list made strictly increasing and alternating again by
    dropping the later of two same-kind neighbours -> int arrays."""
    order = np.argsort(rows, kind="stable")
    out_r, out_k = [], []
    for q in order:
        f, kd = int(rows[q]), int(kinds[q])
        if out_r and (f <= out_r[-1] or kd == out_k[-1]):
            continue
        out_r.append(f)
        out_k.append(kd)
    return np.asarray(out_r, dtype=int), np.asarray(out_k, dtype=np.int64)


def phase_runs_field(phase):
    """The written ``metadata.phase_runs``: the called apex count, the
    count inside contradicting runs and their share (null with no
    apex)."""
    n, m = int(phase["apexes"]), int(phase["in_runs"])
    return {"apexes": n, "in_runs": m, "share": m / n if n else None}


def style_positions_composed(vmarg, level, env_script, shot_edges,
                             band=None, still_eps=0.0,
                             plat=None, plat_thr=PLAT_THR, plat_lo=PLAT_LO,
                             plat_peak=0.0, plat_veto=0.0, plat_soft=None,
                             plat_rail_track=False, plat_shift_cap=0.0,
                             rev=None, rev_snap=0, times_ms=None,
                             subframe="off", sub_out=None,
                             ext_snap=EXT_SNAP,
                             rev_source="cross", rev_bias=0.0,
                             rev_gap=None, force_out=None, amp_cap_x=0.0,
                             phase_out=None):
    """Composed styling (production) -- each head contributes the axis it is
    best at: reversal times from the marginal velocity's zero crossings,
    stroke LEVEL from the position head, stroke AMPLITUDE from the
    generated envelope (calibrated script units). ``band`` (optional,
    ext-head checkpoints): (floor, ceiling) tracks; a stroke endpoint
    within EXT_SNAP of the predicted edge stretches to it -- scripters
    tap the extremes at reversals, level +- env/2 stops short.
    ``still_eps`` (pos/s): segments whose mean |vmarg| sits below this
    hold the predicted level instead of minting a stroke -- during target
    holds the marginal's magnitude collapses ~6x (corpus: 11 vs 70 pos/s,
    hold_analysis.py) while its sign still wiggles and the AR envelope
    never quiets, so sub-threshold crossings are phantom strokes.
    Below the gate the segment emits the marginal's OWN predicted
    travel (min(tr_raw, tr_env), no band snap) instead of parking. A
    phantom is the envelope minting amplitude where the marginal reads
    quiet (tr_env >> tr_raw); a real micro-stroke's tr_raw is its
    video-predicted excursion (the queue-1 excursion probe: smoothed
    |vmarg| ranks micro vs still at AUC ~0.78 out of clip). True holds
    collapse to sub-texture ripple (~5 pos units, under the corpus's
    8-unit parked/authored trough); authored micro-strokes keep their
    predicted 10-20.
    ``plat`` (optional, dwell-head checkpoints): (P(top), P(bottom))
    tracks; runs above ``plat_thr`` are LEVEL-LOCKED to the matching band
    rail (``level_lock``) once the strokes are built -- their local mean
    is pinned to the plateau level and their ripple is kept, so the dwell
    stops being traversed without being flattened. The lock is worth only
    as much as the dwell call under it: on rows it does not cover, the
    step between locked and free track ADDS excursion, so a detector that
    fires on part of a plateau scores worse than one that never fires.
    The 3-frame box on the
    crossing signal is a measured optimum: 1 is a wash, 5 merges fast
    reversals AND worsens slow-band timing.
    ``rev`` (optional, rev-head checkpoints): (P(peak), P(valley))
    tracks; with ``rev_snap`` > 0 each vmarg crossing moves to the
    head's local argmax within +-rev_snap frames -- direction-aware (a
    rising segment ends at a PEAK), order-preserving. The stroke
    STRUCTURE stays the marginal's (density is measured right); only
    the boundary POSITION is re-localized, targeting the slow-band
    2-5-frame crossing scatter.

    ``rev_source="viterbi"`` (rev-head checkpoints) replaces the crossing
    SEGMENTATION itself: reversal rows and directions come from the
    alternating event Viterbi over the rev head's probabilities
    (``common.alternating_events`` -- threshold-free, same-type
    transitions forbidden), while the CARRIER is untouched: level, band
    rails, envelope amplitude, stillness gate and the dwell lock place
    positions exactly as in cross mode. Two structural consequences: a
    CALLED reversal reverses (when the level slope would swallow the
    stroke -- the sum-monotonic loss -- the endpoint
    anchors to the stroke's own start with the envelope's amplitude; the
    next unswallowed segment re-anchors to the level, bounding drift),
    and the apex rows are handed to ``extrema_actions`` via ``force_out``
    so the written artifact carries every called vertex (RDP still prunes
    sub-unit prominence). ``rev_snap`` is subsumed. A shot with no
    decoded events falls back to cross segmentation.

    ``amp_cap_x`` (0 = off) bounds every stroke's written excursion by
    ``amp_cap_x`` times the marginal's OWN travel over the segment,
    ``sum |vmarg| * dt``. A written half-stroke's speed is its excursion
    over its duration, and the marginal's mean magnitude over the segment
    is the model's own reading of the local speed, so the bound holds the
    written speed under ``amp_cap_x`` times the speed the model predicts
    there. This is the shape of the slow-section spike column, which reads
    a written half-stroke against the SCRIPT's local speed, taken in the
    one quantity the decode has without a script. The still-soft path
    already writes ``min(tr_raw, tr_env)`` below the stillness gate; this
    carries the same bound, with slack, to every segment above it. Depth
    only: no time moves and no reversal is deleted, so a called reversal
    still reverses, at a smaller excursion.

    ``phase_out`` (optional dict with ``apexes`` and ``in_runs``) sums,
    over shots, the called apexes and the
    apexes inside their contradicting runs of ``PHASE_RUN_K``
    (``contradicting_runs``): the run share the written funscript
    records for the human to read beside the polarity and drift alarms.
    It is an observation; nothing reads it back."""
    p = np.full(len(vmarg), 50.0)
    # WALL-CLOCK low-pass: taken on this clip's row grid, so the level track
    # and the rails carry the same duration at every row rate
    _k = common.rows_at(common.DECODE_SMOOTH_S, FPS, odd=True)
    _kr = common.rows_at(REV_SMOOTH_RUN_S, FPS, odd=True)

    def _lp(x, nan):
        """The decode's level/rail low-pass, run across the whole clip.

        Kept deliberately: taking it INSIDE each shot is the obvious
        reading of a cut, and it measures worse. The level head's step at
        a cut correlates 0.13 with the scripter's, so the box straddling a
        seam is low-passing noise -- which is what a low-pass is for."""
        return np.convolve(np.nan_to_num(x, nan=nan),
                           np.ones(_k) / _k, mode="same")

    lv = _lp(level, 50.0)
    ev = np.nan_to_num(env_script, nan=0.0)
    if band is not None:
        blo = _lp(band[0], 0.0)
        bhi = _lp(band[1], 100.0)
    dk = dwell_kind(plat, plat_thr, plat_lo, peak=plat_peak) \
        if plat is not None else None
    if dk is not None and plat_veto > 0:
        dk = stroke_veto(dk, vmarg, plat_veto)
    for lo, hi in zip(shot_edges[:-1], shot_edges[1:]):
        v = vmarg[lo:hi]
        lvl = lv[lo:hi]
        n = len(v)
        if n < 2:
            continue
        s = np.convolve(v, np.ones(_kr) / _kr, mode="same")
        sg = np.sign(s)
        sg[sg == 0] = 1
        cross = np.where(sg[1:] * sg[:-1] < 0)[0] + 1
        seg_dirs = None
        if rev_source == "viterbi" and rev is not None:
            if rev_gap is None:
                rev_gap = common.rows_at(common.EVENT_GAP_S, FPS)
            rtv = np.nan_to_num(rev[0][lo:hi])
            rbv = np.nan_to_num(rev[1][lo:hi])
            # the per-band prior is per ROW (an array over the full
            # track); a shot's decode gets its own slice of it
            vrows, vkinds = common.alternating_events(
                rtv, rbv,
                bias=rev_bias[lo:hi] if np.ndim(rev_bias) else rev_bias,
                min_gap=rev_gap[lo:hi] if np.ndim(rev_gap) else rev_gap)
            keep = (vrows >= 0) & (vrows + 1 <= n - 1)
            vrows, vkinds = vrows[keep], vkinds[keep]
            if phase_out is not None and len(vrows):
                runs, _d, _u = contradicting_runs(
                    vrows, vkinds, sg, common.rows_at(REV_SUPPORT_S, FPS),
                    PHASE_RUN_K)
                phase_out["apexes"] += len(vrows)
                phase_out["in_runs"] += sum(j - i for i, j in runs)
            if len(vrows):
                # apex on the segment's LAST row: event row f -> boundary
                # f + 1, same convention as rev_snap. Segment i ends at
                # its event, so an up-stroke's direction IS the event's
                # kind (+1 peak); the tail segment continues the
                # alternation.
                cross = (vrows + 1).astype(int)
                seg_dirs = np.concatenate(
                    (vkinds, [-vkinds[-1]])).astype(np.float64)
        if seg_dirs is None and rev is not None and rev_snap > 0 \
                and len(cross):
            # the composed stroke's apex lands on its segment's LAST row
            # (c - 1), so a head max at frame f puts the crossing at
            # f + 1. Direction from the ORIGINAL segmentation; snapped
            # boundaries stay strictly ordered (collisions drop, merging
            # the stroke -- measured noise-weight in the attribution)
            rt = np.nan_to_num(rev[0][lo:hi])
            rb = np.nan_to_num(rev[1][lo:hi])
            b0 = np.concatenate(([0], cross)).astype(int)
            snapped, prev = [], 0
            for a, c in zip(b0[:-1], cross):
                d = np.sign(s[a:c].mean()) if c > a else 0.0
                track = rt if d > 0 else rb
                w0 = max(prev, c - 1 - rev_snap)
                w1 = min(n - 2, c - 1 + rev_snap)
                c2 = c if w1 < w0 else \
                    w0 + int(np.argmax(track[w0:w1 + 1])) + 1
                if prev < c2 < n:
                    snapped.append(c2)
                    prev = c2
            cross = np.asarray(snapped, dtype=int)
        bounds = np.concatenate(([0], cross, [n])).astype(int)
        if force_out is not None and seg_dirs is not None:
            force_out.extend(int(lo + c - 1) for c in cross)
        cur = float(lvl[0])
        for si, (a, b) in enumerate(zip(bounds[:-1], bounds[1:])):
            d = seg_dirs[si] if seg_dirs is not None else \
                (np.sign(s[a:b].mean()) if b > a else 0.0)
            tr_env = ev[lo + a:lo + b].mean() * (b - a) * DT
            tr_raw = np.abs(v[a:b]).sum() * DT
            is_still = np.abs(v[a:b]).mean() < still_eps
            if tr_raw <= 1e-9 or d == 0 or tr_env <= 1e-9 or is_still:
                if d != 0 and tr_raw > 1e-9 and tr_env > 1e-9:
                    # the marginal's own travel IS the predicted local
                    # excursion; no band snap -- EXT_SNAP would stretch
                    # a ripple to the band edge
                    end = float(np.clip(
                        lvl[b - 1] + d * min(tr_raw, tr_env, 100.0) / 2.0,
                        0.0, 100.0))
                    frac = np.clip(np.cumsum(np.abs(v[a:b])) * DT
                                   / tr_raw, 0.0, 1.0)
                    p[lo + a:lo + b] = cur + (end - cur) * frac
                    cur = end
                else:
                    p[lo + a:lo + b] = lvl[a:b]    # hold the predicted level
                    cur = float(lvl[b - 1]) if b > a else cur
                continue
            end = float(np.clip(lvl[b - 1] + d * min(tr_env, 100.0) / 2.0,
                                0.0, 100.0))
            if band is not None:
                if d < 0 and end < blo[lo + b - 1] + ext_snap:
                    end = float(np.clip(blo[lo + b - 1], 0.0, 100.0))
                elif d > 0 and end > bhi[lo + b - 1] - ext_snap:
                    end = float(np.clip(bhi[lo + b - 1], 0.0, 100.0))
            if seg_dirs is not None and np.sign(end - cur) != d:
                # a CALLED reversal reverses: when the level slope swallows
                # the stroke (the sum-monotonic loss), the
                # endpoint anchors to the stroke's own start with the
                # envelope's amplitude; the next unswallowed segment
                # re-anchors to the level, so drift stays bounded
                end = float(np.clip(cur + d * min(tr_env, 100.0) / 2.0,
                                    0.0, 100.0))
            end = amp_bound(end, cur, tr_raw, b - a, DT, amp_cap_x)
            # the device cannot play a steeper transition than
            # common.MAX_POS_RATE and the emitted actions ARE this
            # segment's endpoints, so bound its depth exactly as the
            # targets are bounded (common.clamp_speed)
            lim = common.MAX_POS_RATE * (b - a) * DT
            if abs(end - cur) > lim:
                end = float(np.clip(cur + np.sign(end - cur) * lim,
                                    0.0, 100.0))
            frac = np.clip(np.cumsum(np.abs(v[a:b])) * DT / tr_raw, 0.0, 1.0)
            p[lo + a:lo + b] = cur + (end - cur) * frac
            cur = end
        if sub_out is not None and subframe != "off" and times_ms is not None:
            # queue-8 lever (a): SUB-FRAME reversal times for the emitted
            # actions. 'rev' places the event between rows from the
            # event head's probability around the (snapped) apex row
            # (``subrow_offset``); 'cross'
            # linear-interpolates the smoothed marginal's zero crossing
            # (valid only where the signal still straddles zero -- the
            # snap can move the boundary off it). Grid metrics cannot see
            # this; the artifact's timing read (scoring.py) is the scorer.
            for si, (a, b) in enumerate(zip(bounds[:-2], bounds[1:-1])):
                f = b - 1                      # the stroke's apex row
                gl = lo + f
                d = seg_dirs[si] if seg_dirs is not None else \
                    (np.sign(s[a:b].mean()) if b > a else 0.0)
                if d == 0:
                    continue
                dt_ms = (times_ms[gl + 1] - times_ms[gl]) \
                    if gl + 1 < len(times_ms) else 1000.0 * DT
                if subframe == "rev" and rev is not None:
                    y = rev[0] if d > 0 else rev[1]
                    if 1 <= gl < len(y) - 1:
                        dlt = subrow_offset(y, gl)
                        sub_out[gl] = float(times_ms[gl]) + dlt * dt_ms
                elif subframe == "cross":
                    if f + 1 < n and s[f] != 0 \
                            and np.sign(s[f]) != np.sign(s[f + 1]):
                        dlt = float(np.clip(s[f] / (s[f] - s[f + 1]),
                                            0.0, 1.0))
                        sub_out[gl] = float(times_ms[gl]) + dlt * dt_ms
    if dk is not None:
        # the strokes are built; now park the ones called a plateau,
        # keeping the ripple they already carry
        p = level_lock(p, dk, blo if band is not None else lv,
                       bhi if band is not None else lv, plat=plat,
                       soft=plat_soft, rail_track=plat_rail_track,
                       shift_cap=plat_shift_cap)
    return p


def extrema_actions(p, times_ms, shot_edges, sub=None, force=None,
                    cut_ease=0.0, rdp_eps=0.0, stats=None):
    """Funscript actions: shot endpoints + position extrema (smoothed).
    ``sub`` (row -> ms): sub-frame reversal times from the composed
    decode; an extremum row present in it emits at the refined time.
    ``force`` (global rows): decode-called apex rows (the Viterbi graft)
    emitted as vertices even where the smoothed track shows no flip --
    the composed track reverses there by construction, and the smoother
    would otherwise erase exactly the fast strokes the call recovered.

    ``cut_ease`` (pos/s, 0 = off) eases the SEAM at a shot cut. Styling
    runs per shot, so consecutive shots each force an endpoint and every
    internal cut carries two vertices about one row apart. The level
    genuinely moves across a cut -- V-JEPA sees a new pose -- but the
    pair asks for that move in ~33 ms, which is a full-speed device
    slam: measured on the OOS drafts, seams are 1.15% of transitions and
    14.9% of the ones at the device cap, at a median seam dt of 81 ms
    against 264 ms away from cuts. Scripters do the opposite, spending
    LONGER at a cut than elsewhere (401 ms median, and their fastest
    transitions are under-represented at cuts).

    So above ``cut_ease`` the outgoing shot's closing vertex -- which
    exists only to close the segment, never because a reversal is there
    -- is dropped, and the move runs from that shot's last real reversal
    to the incoming shot's opening instead; after RDP, ``slew_cut_seams``
    holds whatever remains at the seam to the same rate by depth. Timing
    never moves and no reversal is deleted; this is artifact-layer pruning
    on the same footing as RDP, and the device cap still backstops it.

    ``rdp_eps`` (pos units, 0 = off) runs ``common.rdp_actions`` HERE, not
    at the caller, because the seam ceiling must land after it and the
    chain must be impossible to assemble wrong -- this returns the finished
    written list in deploy order (ease, RDP, slew), the same order
    style.rs owns. ``stats`` (optional dict out) reports the vertex counts
    before and after RDP as ``raw``/``rdp``."""
    actions = []
    _kr = common.rows_at(REV_SMOOTH_RUN_S, FPS, odd=True)
    fr = np.asarray(sorted(force), dtype=int) if force else None
    for si, (lo, hi) in enumerate(zip(shot_edges[:-1], shot_edges[1:])):
        seg = p[lo:hi]
        if len(seg) < 2:
            continue
        s = np.convolve(seg, np.ones(_kr) / _kr, mode="same")
        d = np.sign(np.diff(s))
        d[d == 0] = 1
        flips = np.where(d[1:] * d[:-1] < 0)[0] + 1
        parts = [np.asarray([0]), flips, np.asarray([len(seg) - 1])]
        if fr is not None:
            parts.append(fr[(fr >= lo) & (fr < hi)] - lo)
        idx = np.unique(np.concatenate(parts))
        for i in idx:
            t = times_ms[lo + i]
            lvl_i = seg[i]
            if sub is not None:
                # the smoothed-extremum row can sit +-1 off the composed
                # apex row that carries the refined time -- same reversal
                for r in (lo + i, lo + i - 1, lo + i + 1):
                    if r in sub:
                        t = sub[r]
                        break
            actions.append({"at": int(round(t)),
                            "pos": int(round(np.clip(lvl_i, 0, 100))),
                            "_shot": si,
                            # closes a shot that another shot follows
                            "_seam": bool(i == len(seg) - 1
                                          and hi < shot_edges[-1]),
                            # opens a shot that another shot precedes
                            "_open": bool(i == 0
                                          and lo > shot_edges[0])})
    actions.sort(key=lambda a: a["at"])
    out = [a for i, a in enumerate(actions)
           if i == 0 or a["at"] > actions[i - 1]["at"]]
    if cut_ease > 0:
        out = ease_cut_seams(out, cut_ease)
    for a in out:
        del a["_shot"], a["_seam"], a["_open"]
    if stats is not None:
        stats["raw"] = len(out)
    if rdp_eps > 0:
        out = common.rdp_actions(out, rdp_eps)
    if stats is not None:
        stats["rdp"] = len(out)
    if cut_ease > 0:
        # the seam ceiling, AFTER RDP: sub-frame emission and RDP both
        # relocate vertices around a cut, so this is the first point at
        # which the written seam is the one a device plays
        slew_cut_seams(out,
                       [float(times_ms[e]) for e in shot_edges[1:-1]],
                       cut_ease, 1000.0 / FPS)
    return out


def ease_cut_seams(actions, limit):
    """Ease the seam at each shot cut: drop the FORCED boundary vertices
    whose transition is faster than ``limit`` pos/s.

    Styling runs per shot, so a cut carries two vertices about one row
    apart -- the outgoing shot's forced closing one and the incoming
    shot's forced opening one -- and the whole level change across the cut
    is asked for between them. Dropping the closer alone starts the move
    at the outgoing shot's last real reversal, which is most of the fix;
    where that is STILL too fast the opener goes too and the move runs to
    the incoming shot's first real reversal, spanning the cut the way a
    scripter's stroke does.

    A vertex only goes when its own shot keeps another, so no shot is left
    unrepresented and the move always runs between real reversals. At most
    one vertex per side per cut: each drop leaves a longer seam than it
    removed, so nothing here can compound.
    """
    keep = [True] * len(actions)

    def prev_kept(i):
        j = i - 1
        while j >= 0 and not keep[j]:
            j -= 1
        return j

    def next_kept(i):
        j = i + 1
        while j < len(actions) and not keep[j]:
            j += 1
        return j

    def too_fast(j, k):
        if j < 0 or k >= len(actions):
            return False
        dt = actions[k]["at"] - actions[j]["at"]
        return dt > 0 and (
            abs(actions[k]["pos"] - actions[j]["pos"]) * 1000.0 / dt > limit)

    for i, a in enumerate(actions):
        if not a["_seam"]:
            continue
        k = next_kept(i)                      # the incoming shot's opener
        j = prev_kept(i)                      # this shot's last real vertex
        if k >= len(actions) or j < 0:
            continue
        if actions[j]["_shot"] != a["_shot"] or not too_fast(i, k):
            continue                          # nothing of this shot survives
        keep[i] = False
        m = next_kept(k)
        if (actions[k].get("_open") and m < len(actions)
                and actions[m]["_shot"] == actions[k]["_shot"]
                and too_fast(j, k)):
            keep[k] = False
    return [a for a, k in zip(actions, keep) if k]


SEAM_BACK_ROWS = 2.5   # the seam neighbourhood, in rows either side of a
SEAM_FWD_ROWS = 1.5    # cut: half-row bounds, so no vertex sits on one


def slew_cut_seams(actions, cut_times_ms, limit, row_ms):
    """The hard ceiling under the ease: no written transition at a shot
    cut runs faster than ``limit`` pos/s. Runs AFTER RDP, on the list that
    ships, because the seam neighbourhood is not stable before it:
    sub-frame emission can pull the incoming shot's first reversal to the
    outgoing side of the cut, and RDP can delete a midpoint and leave a
    steeper transition between the survivors -- both relocate a slam the
    per-vertex ease already judged clean.

    The ease drops the forced boundary vertices, which is most of the fix
    and all the vertex-dropping that is safe -- what remains around the cut
    is real reversals, and where they sit close to it with a large level
    step between them the move is still a device slam. A one-frame slam
    across a scene change is never a stroke a scripter writes, so the
    residual is clamped the way the device cap clamps: DEPTH truncation
    toward the predecessor (``common.clamp_speed``'s rule at this limit),
    walked causally from the last vertex before each cut's seam
    neighbourhood until the seam is behind it and the model's own positions
    are reachable again. Each ``c`` is the INCOMING shot's first row time,
    so the forced pair spans the row ending there; the neighbourhood runs
    2.5 rows back and 1.5 forward -- a row of slack around the pair for
    sub-frame emission and RDP relocating its vertices, on HALF-ROW
    boundaries because a whole-row bound lands exactly on a row time and
    float error would decide which side a vertex falls. Times never move,
    no reversal is deleted -- a clamped reversal keeps its instant and
    gives up depth, and the level re-anchors at ``limit`` instead of
    instantly. In place."""
    if len(actions) < 2:
        return
    ats = [a["at"] for a in actions]
    for c in cut_times_ms:
        j = max(bisect.bisect_left(ats, c - SEAM_BACK_ROWS * row_ms) - 1, 0)
        while j + 1 < len(actions):
            a, b = actions[j], actions[j + 1]
            dt = b["at"] - a["at"]
            if dt > 0:
                d = b["pos"] - a["pos"]
                lim = limit * dt / 1000.0
                if abs(d) > lim:
                    step = int(lim)             # truncate: rounding could
                    b["pos"] = int(np.clip(    # round UP past the limit
                        a["pos"] + (step if d > 0 else -step), 0, 100))
                elif b["at"] > c + SEAM_FWD_ROWS * row_ms:  # past the seam
                    break
            j += 1


def pos_metrics(p, tgt_pos, ok, vmask, shot_edges, subset_shots=False):
    """(concat corr, length-weighted per-shot mean corr, MAE) on the held-out
    rows -- ``vmask`` is common.val_mask over common.val_regions, the same
    rows training desupervised. ``subset_shots`` scores each shot on its
    KEPT rows instead of requiring the whole shot in ``vmask`` -- the
    --clean-contradicted convention, where exclusions scatter row-wise and
    the strict rule would leave no whole shot standing. The bare panel
    never sets it, so the historical per-shot numbers stand."""
    val = ok & vmask
    cat = corr(p[val], tgt_pos[val])
    mae = float(np.mean(np.abs(p[val] - tgt_pos[val])))
    cs, ws = [], []
    _ms = common.rows_at(MIN_SPAN_S, FPS)
    for lo, hi in zip(shot_edges[:-1], shot_edges[1:]):
        if subset_shots:
            keep = val[lo:hi]
            if int(keep.sum()) < _ms:
                continue
            c = corr(p[lo:hi][keep], tgt_pos[lo:hi][keep])
            n = int(keep.sum())
        else:
            if not vmask[lo:hi].all() or hi - lo < _ms \
                    or not ok[lo:hi].all():
                continue
            c = corr(p[lo:hi], tgt_pos[lo:hi])
            n = hi - lo
        if np.isfinite(c):
            cs.append(c)
            ws.append(n)
    w = np.array(ws, dtype=np.float64)
    mean_shot = float(np.sum(w * cs) / w.sum()) if len(w) else float("nan")
    return cat, mean_shot, mae


box = scoring.box          # zero-padded by default: the panel's own smoother


def still_metrics(p, tgt_vel, val, eps=STILL_TGT_EPS, k=None):
    """Stillness confusion on the frame grid: a row is STILL when its
    ~1 s box-smoothed |velocity| sits under ``eps`` pos/s. Recall = the
    share of target-still time the draft also holds (phantom strokes
    lower it); precision = the share of draft-still time the target is
    really still (over-stilling lowers it). Returns (precision, recall,
    target still share)."""
    k = common.rows_at(STILL_SMOOTH_S, FPS, odd=True) if k is None else k
    dv = np.abs(np.diff(p, prepend=p[:1])) * FPS
    still_p = (box(dv, k) < eps) & val
    still_t = (box(np.abs(tgt_vel), k) < eps) & val
    both = float((still_p & still_t).sum())
    return (both / max(still_p.sum(), 1), both / max(still_t.sum(), 1),
            float(still_t.sum() / max(val.sum(), 1)))


def _extrema(track, shot_edges, val):
    """The row-grid reversal definition (``scoring.extrema``) on the live
    grid: (row indices, prominence weights) on ``val`` rows."""
    return scoring.extrema(track, shot_edges, val, FPS)


def reversal_kappa(p, tgt_pos, shot_edges, val, tol=None):
    """``scoring.reversal_kappa`` on the live grid -> (kappa, precision,
    recall): amplitude-weighted reversal-timing agreement at REV_TOL_S,
    chance-corrected so density alone earns nothing."""
    return scoring.reversal_kappa(p, tgt_pos, shot_edges, val, FPS, tol)
def env_slopes(p, tgt_vel, val, k=None):
    """Through-origin regression of the draft's amplitude envelope on the
    target's, per target-intensity tercile (low/mid/high). Slope << 1 in
    the HIGH tercile is amplitude hedging exactly where it matters --
    travel is one global number and cannot localize it."""
    k = common.rows_at(SPEED_REF_S, FPS, odd=True) if k is None else k
    dv = np.abs(np.diff(p, prepend=p[:1])) * FPS
    ed, et = box(dv, k)[val], box(np.abs(tgt_vel), k)[val]
    q1, q2 = np.percentile(et, [33.3, 66.7])
    out = []
    for m in (et <= q1, (et > q1) & (et <= q2), et > q2):
        denom = float((et[m] ** 2).sum())
        out.append(float((ed[m] * et[m]).sum() / denom)
                   if denom > 1e-9 else float("nan"))
    return out


def outlier_metrics(p, tgt_pos, val, win_s=10.0, err_thr=30.0, hold_s=0.2):
    """OUTLIER companions to this panel's means. A draft is judged by its
    worst moments, not its average one, and every headline number here is a
    mean or a global sum that cannot see them: MAE 12.7 with occasional
    60-unit excursions and MAE 12.7 that never leaves 20 are different
    products, and a global travel ratio of 1.00 is equally consistent with
    matching everywhere and with hedging half the clip while overshooting
    the other half.

      p99 err  -- the tail of |draft - script|, against MAE's mean
      bad/min  -- SUSTAINED gross position errors: runs above ``err_thr``
                  lasting ``hold_s`` or more, per minute. Sustained,
                  because a one-row spike is a timing artifact at a
                  reversal while a held error is a visibly wrong position
      trv out  -- share of ``win_s`` windows whose LOCAL travel ratio falls
                  outside [0.5, 2.0], against travel's global sum
    """
    e = np.abs(p - tgt_pos)[val]
    if len(e) < 10:
        return None
    mins = max(val.sum() / FPS / 60.0, 1e-9)
    run = max(int(round(hold_s * FPS)), 1)
    over = np.concatenate(([0], (e > err_thr).view(np.int8), [0]))
    d = np.diff(over)
    a0, b0 = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    bad = int(np.sum((b0 - a0) >= run))
    # local travel ratio over non-overlapping windows
    w = max(int(round(win_s * FPS)), 2)
    dp = np.abs(np.diff(p))[val[1:]]
    dt_ = np.abs(np.diff(tgt_pos))[val[1:]]
    n = len(dp) // w
    out = float("nan")
    if n >= 2:
        r = (dp[:n * w].reshape(n, w).sum(1)
             / np.maximum(dt_[:n * w].reshape(n, w).sum(1), 1e-9))
        keep = dt_[:n * w].reshape(n, w).sum(1) > 1.0   # windows that move
        if keep.sum() >= 2:
            rr = r[keep]
            out = float(np.mean((rr < 0.5) | (rr > 2.0)))
    return float(np.percentile(e, 99)), bad / mins, out


def speed_error(p, tgt_pos, val, k=None):
    """SPEED-ERROR TAIL: draft half-strokes per minute whose speed exceeds
    2x / 3x the script's LOCAL speed, plus the p99 ratio.

    Every other speed-adjacent metric here is a mean. ``env_slopes`` gives
    the bias (a through-origin slope per tercile) and ``travel`` gives it
    integrated over the val region, so a draft that runs far too fast in
    bursts and correct on average scores clean on both -- measured: a
    retired emitter arm beat composed styling on env slopes while writing
    9x more over-speed strokes. A jarring transition is an EVENT, not a
    distribution: one stroke at several times the authored speed is noticed
    whatever the median does, so this counts EVENTS PER MINUTE. Run it on
    the target itself for the reference rate -- the script's own tail is
    what makes the multiples non-arbitrary rather than tuned, and it is not
    zero (authors do write the occasional flick).

    The device CAP is a different question and already answered elsewhere:
    that asks whether a transition is playable, this asks whether it is
    right. A draft can be fully cap-compliant and still move at 3x the
    speed the script asked for."""
    k = common.rows_at(SPEED_REF_S, FPS, odd=True) if k is None else k
    ref = box(np.abs(np.diff(tgt_pos, prepend=tgt_pos[:1])) * FPS, k)
    d = np.sign(np.diff(p))
    d[d == 0] = 1
    fl = np.where(d[1:] * d[:-1] < 0)[0] + 1
    if not len(fl):
        return None
    idx = np.concatenate(([0], fl, [len(p) - 1]))
    a0, a1 = idx[:-1], idx[1:]
    amp = np.abs(p[a1] - p[a0])
    spd = amp / (np.maximum(a1 - a0, 1) / FPS)
    mid = (a0 + a1) // 2
    # only where the script is actually moving: below the ~20 pos/s device
    # floor the ratio divides by noise
    sel = val[mid] & (ref[mid] > 20.0)
    if sel.sum() < 10:
        return None
    r = spd[sel] / np.maximum(ref[mid][sel], 1e-9)
    mins = max(val.sum() / FPS / 60.0, 1e-9)
    # FAST-STROKE DENSITY is returned with the tail because the tail is
    # uninterpretable without it: a decode that writes no fast strokes
    # cannot write an over-speed one, so a LOW absolute count can mean
    # "clean" or can mean "absent". Measured: a coarser-grid decode's
    # over-speed count ran a third below this one's while capturing a
    # third as much of the script's fast content -- per fast stroke
    # actually delivered it was twice as dirty, the opposite reading.
    # GRID-LOCAL, like every other number on this panel: both this and the
    # k-row reference smoother are row-indexed, and the script's own fast
    # content aliases away when it is resampled onto a coarse grid. Compare
    # styles WITHIN a run freely; for any 15-vs-30 verdict read the written
    # artifact instead, where the clock is milliseconds.
    dur = (a1 - a0)[sel] / FPS
    fast = dur <= 1.0 / (2 * 3.75)          # >=3.75 Hz half-stroke
    big = fast & (amp[sel] >= 20.0)
    return (float(np.sum(r > 2) / mins), float(np.sum(r > 3) / mins),
            float(np.percentile(r, 99)),
            float(fast.sum() / mins), float(big.sum() / mins))


def speed_metrics(p, tgt_pos, tgt_vel, shot_edges, val, k=None, tol=None):
    """Slow/mid/fast asymmetry panel: the core product metrics stratified
    by the SCRIPT's local speed -- box-smoothed |target velocity|, the
    same signal env_slopes terciles on (a SECTION's speed; instantaneous
    |v| is zero at every reversal and would misfile them). Terciles are
    per clip; the returned band edges (pos/s) anchor the slow band
    against the ~20 pos/s device floor. Per band:

      corr / MAE -- position agreement on the band's rows
      travel     -- draft/script travel ratio INSIDE the band: hedging,
                    localized (the global ratio can hide a fast-band
                    deficit behind slow-band overshoot)
      kappa      -- reversal-timing kappa for reversals whose frame falls
                    in the band; ONE global greedy matching (identical to
                    reversal_kappa), credit split by band, chance
                    correction computed within the band. NaN when the
                    band's chance F1 saturates (>0.85): above ~2.5 Hz a
                    reversal lands every ~3 frames, a uniform predictor
                    matches within +-1 by chance, and kappa reads 0 for
                    PERFECT timing -- undefined, not bad
      dt_ms      -- mean |offset| of the band's MATCHED reversals, ms:
                    the density-free timing readout that stays valid
                    exactly where kappa saturates

    Every aggregate metric can read fine while the bands disagree; this
    is the instrument for exactly that suspicion."""
    k = common.rows_at(SPEED_REF_S, FPS, odd=True) if k is None else k
    tol = common.rows_at(common.REV_TOL_S, FPS) if tol is None else tol
    et = box(np.abs(tgt_vel), k)
    q1, q2 = np.percentile(et[val], [33.3, 66.7])
    band_of = np.digitize(et, [q1, q2])          # 0 slow / 1 mid / 2 fast
    ti, tw = _extrema(tgt_pos, shot_edges, val)
    pi, pw = _extrema(p, shot_edges, val)
    hit_t = np.zeros(len(ti), dtype=bool)
    hit_p = np.zeros(len(pi), dtype=bool)
    off_t = np.full(len(ti), np.nan)             # matched offset, frames
    if len(ti) and len(pi):
        used = np.zeros(len(pi), dtype=bool)
        for j in np.argsort(-tw):
            d = np.abs(pi - ti[j]).astype(np.float64)
            d[used] = tol + 1
            m = int(np.argmin(d))
            if d[m] <= tol:
                used[m] = hit_t[j] = hit_p[m] = True
                off_t[j] = d[m]
    tb = band_of[ti] if len(ti) else np.zeros(0, dtype=int)
    pb = band_of[pi] if len(pi) else np.zeros(0, dtype=int)
    dv = np.abs(np.diff(p, prepend=p[:1]))
    tv = np.abs(np.diff(tgt_pos, prepend=tgt_pos[:1]))
    bands = []
    _ms = common.rows_at(MIN_SPAN_S, FPS)
    for b in range(3):
        vm = val & (band_of == b)
        n = int(vm.sum())
        if n < _ms:
            bands.append(None)
            continue
        c = corr(p[vm], tgt_pos[vm])
        mae = float(np.abs(p[vm] - tgt_pos[vm]).mean())
        trv = float(dv[vm].sum() / max(tv[vm].sum(), 1e-9))
        tm, pm = tb == b, pb == b
        kap = dt = float("nan")
        if tm.any() and pm.any():
            rec = float((tw[tm] * hit_t[tm]).sum() / max(tw[tm].sum(), 1e-9))
            prec = float((pw[pm] * hit_p[pm]).sum() / max(pw[pm].sum(), 1e-9))
            f1 = 2 * prec * rec / max(prec + rec, 1e-9)
            win = (2 * tol + 1) / max(n, 1)
            rc, pc = min(1.0, int(pm.sum()) * win), min(1.0, int(tm.sum()) * win)
            f1c = 2 * pc * rc / max(pc + rc, 1e-9)
            if f1c <= 0.85:              # else chance-saturated: undefined
                kap = (f1 - f1c) / max(1 - f1c, 1e-9)
            mo = off_t[tm & hit_t]
            if len(mo):
                dt = float(np.nanmean(mo) * 1000.0 / FPS)
        bands.append({"corr": c, "mae": mae, "travel": trv, "kappa": kap,
                      "dt_ms": dt, "n_rev": int(tm.sum())})
    return {"q1": float(q1), "q2": float(q2), "bands": bands}


def dwell_windows(tgt_pos, shot_edges, scripted, val):
    """Script trapezoid dwells measurable on val rows ->
    (a, b, kind, level, tol, stroke) windows (``common.dwell_spans``, the
    same detector the dwell-head labels use). Windows keep only
    fully-scripted, fully-val spans inside one shot (cut seams have
    their own hold rules)."""
    edges = np.asarray(shot_edges)
    wins = []
    for a, b, kind, level, tol, stroke in common.dwell_spans(tgt_pos,
                                                             row_hz=FPS):
        if not (val[a:b].all() and scripted[a:b].all()):
            continue
        if ((edges > a) & (edges < b)).any():
            continue
        wins.append((a, b, kind, level, tol, stroke))
    return wins


def dwell_response(p, tgt_pos, wins, smooth=None):
    """Draft behavior inside script dwells, per kind (+1 top / -1 bottom).
    A dwell asks the draft to PARK at an extremum, not to freeze there:
    the script's own track oscillates inside 95% of them. So the shape
    test runs on the local mean, and texture is scored separately:

      PARKED    -- the draft's local mean stays inside the dwell's own
                   ``tol`` (the same bound the script's mean obeys there)
      TRAVERSED -- it sweeps >= half the adjoining stroke: the draft rode
                   through the dwell and reversed as a triangle apex,
                   deleting the plateau's duration
      TEXTURE   -- median ratio of the draft's residual (raw minus local
                   mean) std to the script's. 1.0 = the right amount of
                   ripple; ~0 = the plateau was flattened, which a
                   range-based "held" test would have scored as a WIN
      LEVEL     -- median |draft local mean - the script's parked value|

    Both shape tests are scale-relative (a plateau's texture scales with
    its stroke), so neither can be passed by hedging amplitude. Position
    corr/MAE/travel cannot see any of this: dwell deletion barely moves
    them. Returns {kind: (n, parked, traversed, texture, level)}."""
    smooth = common.rows_at(common.DWELL_SMOOTH_S, FPS, odd=True) \
        if smooth is None else smooth
    ps = common.local_mean(p, smooth)
    ts = common.local_mean(tgt_pos, smooth)
    out = {}
    for kind in (+1, -1):
        w = [x for x in wins if x[2] == kind]
        if not w:
            continue
        parked = trav = 0
        tex, lvl = [], []
        for a, b, _k, level, tol, stroke in w:
            exc = float(ps[a:b].max() - ps[a:b].min())
            parked += exc <= tol
            trav += exc >= 0.5 * stroke
            t_s = float(np.std(tgt_pos[a:b] - ts[a:b]))
            if t_s >= 1.0:      # a genuinely flat dwell has no ripple to
                tex.append(     # reproduce -- it cannot fail the test
                    float(np.std(p[a:b] - ps[a:b])) / t_s)
            lvl.append(abs(float(ps[a:b].mean()) - level))
        out[kind] = (len(w), parked / len(w), trav / len(w),
                     float(np.median(tex)) if tex else float("nan"),
                     float(np.median(lvl)))
    return out
def band_metrics(p, tgt, val):
    """Extreme-band MAE, band recall (target >=80 / <=20), travel ratio
    (styled |dp| sum / target's -- amplitude hedging shows as << 1)."""
    hi_t, lo_t = tgt >= 80, tgt <= 20
    ext = (hi_t | lo_t) & val
    mae_e = float(np.abs(p[ext] - tgt[ext]).mean()) if ext.any() else float("nan")
    rec_hi = float(((p >= 80) & hi_t & val).sum() / max((hi_t & val).sum(), 1))
    rec_lo = float(((p <= 20) & lo_t & val).sum() / max((lo_t & val).sum(), 1))
    travel = float(np.abs(np.diff(p))[val[1:]].sum()
                   / max(np.abs(np.diff(tgt))[val[1:]].sum(), 1e-9))
    return mae_e, rec_hi, rec_lo, travel


def fit_rev_bias(tracks, vel_pred, vid_id="", verbose=True):
    """The viterbi decode's emission prior, fitted to the head's own summed
    posterior, per row: one prior for the still rows and one for the moving.

    The alternating decode's event RATE is the head's class prior, which
    under-emits at bias 0 (``common.alternating_events`` docstring) -- slow
    strokes merge into held segments. The count-calibrated head's summed
    posterior IS the count, and it is the one target that exists with and
    without a script, so the scored configuration is the deploy
    configuration. Lives apart from ``main`` because a decode sweep restyles
    cached tracks and has to fit the prior the same way the decode does.
    """
    rt_a = np.nan_to_num(tracks["rev_top"])
    rb_a = np.nan_to_num(tracks["rev_bot"])
    a0, b0 = 0, min(len(rt_a), common.rows_at(BIAS_FIT_S, FPS))
    # fit THROUGH the decode that will run: the refractory changes the
    # emitted rate at a given prior (the emitter's cap lesson)
    _mg = common.rows_at(common.EVENT_GAP_S, FPS)

    def _vdec(pp, vv, bias=0.0):
        return common.alternating_events(pp, vv, bias=bias, min_gap=_mg)

    def _banded(band_of, n_bands, label):
        wants = [int(round(float((rt_a + rb_a)[a0:b0][band_of[a0:b0] == k]
                                 .sum()))) for k in range(n_bands)]
        bb = common.fit_emission_bias_bands(rt_a[a0:b0], rb_a[a0:b0],
                                            band_of[a0:b0], wants,
                                            decode=_vdec)
        bias = bb[band_of]               # per-row, full track
        if verbose:
            grows, _ = _vdec(rt_a[a0:b0], rb_a[a0:b0], bias=bias[a0:b0])
            got = [int((band_of[a0:b0][grows] == k).sum())
                   for k in range(n_bands)]
            print(f"[{vid_id}] viterbi emission prior {label} "
                  f"{np.round(bb, 2).tolist()} ({got} vs targets {wants})",
                  flush=True)
        return bias

    _k = common.rows_at(SPEED_REF_S, FPS, odd=True)
    # An UNSCRIPTED gap carries real posterior mass -- the head finds
    # reversals there and the script asserts nothing -- and a global fit
    # turns that mass into a prior the decode force-emits over the WHOLE
    # clip, landing the surplus in slow scripted sections as speed
    # spikes. Fitting still and moving rows separately decouples them, so
    # the moving rows' prior is their own posterior sum whatever the gaps
    # hold. Membership is the styling's own stillness gate on smoothed
    # |vmarg|, so no script is involved and the fit deploys.
    _mv = (box(np.abs(vel_pred), _k) >= STILL_EPS).astype(int)
    return _banded(_mv, 2, "per gap band")


def decode_stamp(args):
    """The decode that RAN, as the record stamps it: every setting that
    changes what a number means, so a record carries its own decode."""
    return {
        "style": "composed", "pos_temp": POS_TEMP,
        "env_seed": args.env_seed,
        "rev_source": REV_SOURCE,
        "rev_smooth_s": common.REV_SMOOTH_S,
        "rev_gap_s": common.EVENT_GAP_S,
        "rev_snap_s": common.REV_SNAP_S,
        "rev_gap_prior": REV_GAP_PRIOR,
        "still_eps": STILL_EPS,
        # the dwell-lock thresholds are per-CHECKPOINT calibration: they
        # read the dwell head's published scale, which moves whenever that
        # head's prior fold changes
        "plat_thr": PLAT_THR, "plat_lo": PLAT_LO, "plat_peak": PLAT_PEAK,
        "plat_veto": PLAT_VETO, "plat_soft": list(PLAT_SOFT),
        "plat_rail_track": True, "plat_shift_cap": PLAT_SHIFT_CAP,
        "rdp_eps": RDP_EPS, "cut_ease": CUT_EASE,
        "chunk_s": common.DECODE_CHUNK_S, "ctx_s": common.DECODE_CTX_S,
        "subframe": SUBFRAME, "ext_snap": EXT_SNAP, "amp_cap_x": AMP_CAP_X,
    }


def build_parser():
    """The CLI, built apart from ``main`` so the eval and draft commands
    fill the same namespace."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=None, help="the project directory")
    ap.add_argument("--ids", nargs="*", default=["holdout"],
                    help="clip ids or roster names (default: holdout)")
    ap.add_argument("--ckpt", default=common.DEFAULT_CKPT)
    ap.add_argument("--out", default=None,
                    help="the directory the drafts and metrics.json land in")
    ap.add_argument("--no-lag", action="store_true",
                    help="score against the RAW script clock (no lag "
                         "sidecar applied); numbers are then not comparable "
                         "to a lag-applied read")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--val-frac", type=float, default=None,
                    help="held-out fraction. Default follows the "
                         "CHECKPOINT's stamp so the scorer measures exactly "
                         "the rows training desupervised; 1.0 scores a whole "
                         "clip, which is the honest split for a clip the "
                         "trunk never trained on")
    ap.add_argument("--env-seed", type=int,
                    default=common.ENV_DRAW_SEED,
                    help="the flow envelope's AUTHORSHIP seed. It shifts "
                         "the whole base draw and nothing else: a flow "
                         "head samples an envelope rather than publishing "
                         "a mean, so this is the one-to-many axis stated "
                         "as a knob. The draw is a pure function of (row, "
                         "seed), so a seed reproduces exactly and reaches "
                         "goblinscript unchanged. No effect without a "
                         "flow head")
    ap.add_argument("--h0-cache", action="store_true",
                    help="forward through the refit's h0 store "
                         "(h0_store.py; built on first use, reused by "
                         "every tool on the same frozen trunk): the "
                         "whole-clip forward pays the TCN alone. "
                         "BEHAVIORAL against the default live path (the "
                         "store carries the autocast-bf16 frontend; live "
                         "here is fp32)")
    return ap


def run(args):
    """Draft and score under a parsed ``build_parser()`` namespace."""
    if args.dataset is None or args.out is None:
        raise SystemExit("--dataset and --out are required")
    args.ids = common.resolve_ids(args.ids, args.dataset)

    model, ck = load_model(args.ckpt, args.device)
    has_ext = ck["arch"].get("ext_head", False)
    has_plat = ck["arch"].get("plat_head", False)
    has_rev = ck["arch"].get("rev_head", False)
    if not (ck["arch"].get("pos_head", False)
            and ck["arch"].get("gen_env", False)):
        raise SystemExit("composed styling needs a checkpoint with the "
                         "position and envelope heads")
    model.pos_temp = POS_TEMP
    model.env_seed = args.env_seed
    print(f"checkpoint {args.ckpt} (epoch {ck['epoch']})", flush=True)
    subframe = SUBFRAME
    if subframe == "rev" and not has_rev:
        print("note: sub-frame reversal times need a rev-head checkpoint "
              "-- actions stay on the frame grid", flush=True)
        subframe = "off"

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    masks_dir = ck.get("masks_dir")
    # score exactly what the trunk held out: the checkpoint carries its own
    # val geometry, so the scorer cannot drift from the training split.
    # Pre-stamp checkpoints fall back to the recipe every run so far used.
    val_frac = args.val_frac if args.val_frac is not None \
        else float(ck.get("val_frac", 0.15))
    if "val_frac" not in ck:
        print(f"note: checkpoint predates the val stamp -- assuming "
              f"val_frac {val_frac}", flush=True)
    # a trunk trained under another held-out geometry did NOT hold out the
    # rows scored here, so its numbers read inflated rather than neutral --
    # the overlap is large enough to dominate them
    ckpt_split = ck.get("split", "tail")
    if ckpt_split != common.VAL_SPLIT:
        print(f"WARNING: checkpoint trained under the '{ckpt_split}' split "
              f"but scored on '{common.VAL_SPLIT}' -- it TRAINED on much of "
              f"what is scored below, so these numbers are partly IN-SAMPLE "
              f"and read high. Only a trunk trained under "
              f"'{common.VAL_SPLIT}' is honestly measured here.", flush=True)
    art_prior = None
    if Path(common.ARTIFACT_PRIOR).exists():
        art_prior = json.load(open(common.ARTIFACT_PRIOR, encoding="utf-8"))
    else:
        print(f"note: {common.ARTIFACT_PRIOR} missing -- artifact event "
              f"line skipped", flush=True)
    metrics = {}        # per-clip panel numbers, written to out/metrics.json
    # so downstream tools (gate re-cuts, sweep comparisons) read numbers
    # instead of parsing the panel's printed lines
    for vid_id in args.ids:
        ds = JepaClip(args.dataset, vid_id, 384, 192,
                      feat_dir=ck.get("feat_dir", common.LATENTS_DIR),
                      masks_dir=masks_dir,
                      apply_lag=not args.no_lag,
                      row_hz=ck.get("row_hz"))
        if args.h0_cache:
            h0_store.attach(model, ck, [ds], args.dataset, args.device)
        # The row grid is a property of the CACHE, not a constant: a k=2
        # a=4 tree runs 30 rows/s. Every DT-scaled quantity here is a
        # per-row duration (envelope travel, marginal travel, the
        # MAX_POS_RATE depth clamp), so a hardcoded 1/15 doubles them on
        # a 30 Hz cache and makes the speed clamp twice too lenient.
        global FPS, DT
        DT = float(np.median(np.diff(ds.times_ms))) / 1000.0
        FPS = 1.0 / DT
        chunk_rows = common.rows_at(common.DECODE_CHUNK_S, FPS)
        ctx_rows = common.rows_at(common.DECODE_CTX_S, FPS)
        print(f"[{vid_id}] row grid {FPS:.1f} rows/s ({1000 * DT:.1f} ms)"
              f", decode chunk {chunk_rows} rows + ctx {ctx_rows}",
              flush=True)
        T = len(ds.vel)
        vregions = common.val_regions(T, FPS, val_frac)
        vmask = common.val_mask(T, vregions)
        print(f"[{vid_id}] scoring "
              + (f"the WHOLE clip, {vmask.sum() / FPS:.0f}s (val_frac "
                 f"{val_frac:g}: nothing was held back)" if val_frac >= 1.0
                 else f"{len(vregions)} held-out region(s) spread across "
                      f"the clip, {vmask.sum() / FPS:.0f}s"), flush=True)
        shots = list(zip(ds.shot_edges[:-1], ds.shot_edges[1:]))
        pred, tracks = predict_shots(model, ds, shots, args.device,
                                     chunk_rows, collect_tracks=True,
                                     ctx=ctx_rows)
        pred = tracks["vmarg"]     # phase track: velocity metrics and
                                   # crossings always use the marginal
        ok = np.isfinite(pred)
        # the last predicted frame of every shot sits on a static tail pair
        # (zero observed motion) -- hold the previous velocity so cuts never
        # mint phantom strokes in the draft
        for lo, hi in shots:
            if hi - lo >= 2 and np.isfinite(pred[hi - 2]):
                pred[hi - 1] = pred[hi - 2]
        vel_pred = np.nan_to_num(pred) * ck["v_std"]     # pos-units / s
        tgt_vel = ds.vel.numpy().astype(np.float64)
        tgt_pos = ds.pos.numpy().astype(np.float64)

        has_script = (Path(args.dataset) / "scripts"
                      / f"{vid_id}.json").exists()
        # The heads' own outputs are the decode's inputs, uncorrected. A
        # quantile map onto the script's distribution buys the metric that
        # rewards distribution match (band recall) while MAE degrades and
        # travel overshoots -- it rewrites an uncertain "probably 55" into a
        # confident "92", which looks like a script without being right.
        # The heads DO under-reach, because collapsing a predicted
        # distribution to its expectation compresses it; that is fixed by
        # supervising the collapsed value, not by stretching it afterwards.
        sub = {}     # row -> sub-frame ms, filled by the composed decode
        # global apex rows the viterbi graft calls; extrema_actions emits
        # them as vertices so the crossing smoother cannot erase them
        frc = [] if has_rev else None
        phase = {"apexes": 0, "in_runs": 0} if frc is not None else None
        rev_bias = 0.0
        if frc is not None:
            rev_bias = fit_rev_bias(tracks, vel_pred, vid_id)
        p = style_positions_composed(
            vel_pred, tracks["level"], tracks["env"] * ck["v_std"],
            ds.shot_edges,
            band=(tracks["blo"], tracks["bhi"]) if has_ext else None,
            still_eps=STILL_EPS,
            plat=((tracks["plat_top"], tracks["plat_bot"])
                  if has_plat else None),
            plat_thr=PLAT_THR, plat_lo=PLAT_LO, plat_peak=PLAT_PEAK,
            plat_veto=PLAT_VETO, plat_soft=PLAT_SOFT, plat_rail_track=True,
            plat_shift_cap=PLAT_SHIFT_CAP,
            rev=((tracks["rev_top"], tracks["rev_bot"])
                 if has_rev else None),
            rev_snap=common.rows_at(common.REV_SNAP_S, FPS)
            if common.REV_SNAP_S > 0 else 0,
            times_ms=ds.times_ms, subframe=subframe, sub_out=sub,
            ext_snap=EXT_SNAP, amp_cap_x=AMP_CAP_X,
            rev_source=REV_SOURCE, rev_bias=rev_bias,
            rev_gap=common.rows_at(common.EVENT_GAP_S, FPS),
            force_out=frc, phase_out=phase)
        syntheses = {"composed styling": p}

        st = {}
        actions = extrema_actions(
            p, ds.times_ms, ds.shot_edges, sub=sub if sub else None,
            force=frc, cut_ease=CUT_EASE, rdp_eps=RDP_EPS, stats=st)
        n_raw, n_rdp = st["raw"], st["rdp"]
        # DEVICE CAP, on the artifact that ships. Every earlier bound is
        # computed on the ROW grid, but sub-frame emission moves the real
        # timestamps and RDP can delete a midpoint and leave a steeper
        # transition between the survivors -- so the only place the written
        # list is provably playable is here, after both. Depth only; timing
        # never moves.
        spd = [abs(y["pos"] - x["pos"]) * 1000.0 / (y["at"] - x["at"])
               for x, y in zip(actions, actions[1:]) if y["at"] > x["at"]]
        n_over = sum(1 for s in spd if s > common.MAX_POS_RATE)
        if n_over:
            cl = common.clamp_speed([(a["at"], a["pos"]) for a in actions])
            for a, (_t, pos) in zip(actions, cl):
                a["pos"] = int(pos)
        # ALWAYS printed, including the 0 case: this share is the only
        # readout of whether the decode PLACED playable vertices, and no
        # grid metric can see it -- they all read the frame-grid track and
        # never the action list, which is how a 5.2% violation rate reached
        # a user eyeball before it reached the harness.
        print(f"[{vid_id}] speed cap: {n_over}/{len(spd)} transitions "
              f"({n_over / max(len(spd), 1):.2%}) over "
              f"{common.MAX_POS_RATE:g} pos/s, peak {max(spd or [0]):.0f}"
              + (" -- bounded by depth" if n_over else ""), flush=True)
        fpath = out / f"{vid_id}_jepa.funscript"
        fs = {"version": "1.0", "inverted": False, "range": 100}
        a_spans, a_rate = ([], 0.0) if art_prior is None else \
            artifact_events([(a["at"], a["pos"]) for a in actions],
                            art_prior)
        meta_fs = {}
        if a_spans:
            # rarity spans, same field goblinscript stamps -- the review
            # page draws them as the amber band. An instrument, not a
            # gate: nothing in the action list changes
            meta_fs["artifacts"] = [[int(round(a * 1000)),
                                     int(round(b * 1000))]
                                    for a, b in a_spans]
        if phase is not None:
            # the run-share phase reading, same field goblinscript stamps:
            # an observation of the decode, not a gate or a threshold
            meta_fs["phase_runs"] = phase_runs_field(phase)
        if meta_fs:
            fs["metadata"] = meta_fs
        fs["actions"] = actions
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(fs, f)
        print(f"[{vid_id}] wrote {fpath} ({len(actions)} actions, "
              f"{len(actions) / (T / FPS):.2f}/s, rdp kept "
              f"{n_rdp / max(n_raw, 1):.0%})", flush=True)
        if art_prior is not None:
            a_time = sum(b - a for a, b in a_spans)
            print(f"[{vid_id}] artifacts: {len(a_spans)} hot events "
                  f"({a_rate:.2f}/min vs authors "
                  f"{art_prior['anchor_per_min']:.2f}/min), "
                  f"hot time {a_time:.0f}s", flush=True)

        if not has_script:
            print(f"[{vid_id}] no reference script -- metrics skipped",
                  flush=True)
            del ds
            continue
        # an unscripted row has no target: between two actions minutes
        # apart the interpolated "position" is a line through no-man's
        # land, and a corr/MAE against it scores the draft's gap-filling
        # style, not its accuracy. Every target-referenced metric reads
        # scripted rows only; gap behavior is gap_analysis.py's subject.
        sc_rows = ds.scripted.numpy().astype(bool)
        val = ok & vmask & sc_rows
        # the clip's row clock and held-out rows, stamped so a later read
        # of the written draft scores exactly these rows
        clock = scoring.Clock.from_times(ds.times_ms, vmask, ds.shot_edges)
        rec = metrics.setdefault(vid_id, {})
        rec["clock"] = clock.stamp()
        trk_all = rec.setdefault("track", {})
        print(f"[{vid_id}] val velocity corr "
              f"{corr(pred[val], tgt_vel[val]):.3f}", flush=True)
        dwells = dwell_windows(tgt_pos, ds.shot_edges,
                               ds.scripted.numpy(), val)
        if not dwells:
            print(f"[{vid_id}] no measurable val dwells -- dwell "
                  f"response skipped", flush=True)
        for label, pp in syntheses.items():
            cat, mean_shot, mae = pos_metrics(
                pp, tgt_pos, ok & sc_rows, vmask, ds.shot_edges)
            mae_e, rh, rl, trv = band_metrics(pp, tgt_pos, val)
            kap, rp, rr = reversal_kappa(pp, tgt_pos, ds.shot_edges, val)
            stp, str_, sts = still_metrics(pp, tgt_vel, val)
            s1, s2, s3 = env_slopes(pp, tgt_vel, val)
            trk = trk_all.setdefault(label.replace(" styling", ""), {})
            trk.update({
                "corr": cat, "per_shot": mean_shot, "mae": mae,
                "mae_extreme": mae_e, "band_recall_hi": rh,
                "band_recall_lo": rl, "travel": trv, "kappa": kap,
                "rev_p": rp, "rev_r": rr, "still_p": stp, "still_r": str_,
                "still_target": sts, "env_slope": [s1, s2, s3]})
            print(f"[{vid_id}]   {label:<18} position corr concat {cat:.3f} "
                  f"/ per-shot mean {mean_shot:.3f}  MAE {mae:.1f} "
                  f"(extreme {mae_e:.1f})  band recall hi {rh:.2f} "
                  f"lo {rl:.2f}  travel {trv:.2f}  (0 anchors)", flush=True)
            print(f"[{vid_id}]   {'':<18} rev kappa {kap:.2f} (P {rp:.2f} "
                  f"R {rr:.2f})  still P/R {stp:.2f}/{str_:.2f} "
                  f"(target {sts * 100:.0f}%)  env slope lo/mid/hi "
                  f"{s1:.2f}/{s2:.2f}/{s3:.2f}", flush=True)
            # TAILS. Every number on the two lines above is a mean or a
            # global sum; these are the same axes read at the extreme,
            # where the eye actually judges. Script's own rates in
            # parentheses -- they are not zero, so they are what makes
            # "too many" mean something.
            se, se_t = speed_error(pp, tgt_pos, val), \
                speed_error(tgt_pos, tgt_pos, val)
            om = outlier_metrics(pp, tgt_pos, val)
            if se:
                trk["speed_tail"] = {"x2_min": se[0], "x3_min": se[1],
                                     "p99": se[2], "fast_min": se[3],
                                     "amp20_min": se[4]}
                if se_t:
                    trk["script_speed_tail"] = {
                        "x2_min": se_t[0], "x3_min": se_t[1],
                        "p99": se_t[2], "fast_min": se_t[3],
                        "amp20_min": se_t[4]}
            if om:
                trk["outliers"] = {"p99_err": om[0], "gross_min": om[1],
                                   "trv_out": om[2]}
            if se or om:
                bits = []
                if se:
                    # per FAST STROKE DELIVERED, the only cross-grid-safe
                    # reading: the absolute count scales with how much fast
                    # content the decode writes at all
                    per = se[0] / se[3] if se[3] > 0 else float("nan")
                    b = (f"speed >2x/>3x {se[0]:.1f}/{se[1]:.1f} per min "
                         f"p99 {se[2]:.1f}  fast {se[3]:.1f}/min "
                         f"(amp>=20 {se[4]:.1f}) -> {per:.2f} "
                         f"over-speed per fast stroke")
                    if se_t:
                        b += (f"  [script {se_t[0]:.1f}/{se_t[1]:.1f} "
                              f"fast {se_t[3]:.1f}/{se_t[4]:.1f}]")
                    bits.append(b)
                if om:
                    bits.append(f"p99 err {om[0]:.0f}  gross {om[1]:.1f}/min"
                                f"  trv out {om[2]:.0%}")
                print(f"[{vid_id}]   {'':<18} tails: " + "  ".join(bits),
                      flush=True)
            if label == "composed styling":
                sb = speed_metrics(pp, tgt_pos, tgt_vel, ds.shot_edges, val)
                trk["speed_bands"] = sb
                def _f(key, fmt):
                    return "/".join(
                        "--" if b is None or
                        (isinstance(b[key], float) and np.isnan(b[key]))
                        else fmt % b[key] for b in sb["bands"])
                print(f"[{vid_id}]   {'':<18} speed slow/mid/fast "
                      f"(edges {sb['q1']:.0f}/{sb['q2']:.0f} pos/s): "
                      f"corr {_f('corr', '%.3f')}  mae {_f('mae', '%.1f')}  "
                      f"travel {_f('travel', '%.2f')}  "
                      f"kappa {_f('kappa', '%.2f')}  "
                      f"dt {_f('dt_ms', '%.0f')} ms  "
                      f"(rev n {_f('n_rev', '%d')})", flush=True)
            dr = dwell_response(pp, tgt_pos, dwells)
            if dr:
                trk["dwell"] = {("top" if k > 0 else "bot"):
                                {"n": n, "parked": pk, "traversed": tv,
                                 "texture": tx, "level": lv}
                                for k, (n, pk, tv, tx, lv) in dr.items()}
                seg = "  ".join(
                    f"{'top' if k > 0 else 'bot'} parked {pk:.0%} "
                    f"trav {tv:.0%} tex {tx:.2f} lvl {lv:.0f} (n {n})"
                    for k, (n, pk, tv, tx, lv) in sorted(dr.items(),
                                                         reverse=True))
                print(f"[{vid_id}]   {'':<18} dwell >=0.5s: {seg}",
                      flush=True)
        # THE ARTIFACT READ: the written action list against the script,
        # on the stamped clock -- the columns every arm is ranked on
        # (scoring.py; read.py reproduces them from disk). The device-cap
        # and rarity lines ride beside it as provenance.
        art = scoring.score_artifact(scoring.script_from_clip(ds), actions,
                                     clock)
        if art is not None:
            art["cap"] = {"over": n_over, "transitions": len(spd),
                          "peak": max(spd or [0])}
            if art_prior is not None:
                art["events"] = {"n": len(a_spans), "per_min": a_rate,
                                 "hot_s": sum(b - a for a, b in a_spans)}
            rec["artifact"] = art
            po, ti, sp = art["position"], art["timing"], art["speed"]
            if po:
                print(f"[{vid_id}]   artifact  corr {po['corr']:.3f}  "
                      f"MAE {po['mae']:.1f}  travel {po['travel']:.2f}  "
                      f"kappa {po['kappa']:.3f} (P {po['rev_prec']:.3f} "
                      f"R {po['rev_rec']:.3f})  {po['actions_per_s']:.2f} "
                      f"act/s", flush=True)
            if ti:
                print(f"[{vid_id}]   timing    "
                      + "  ".join(
                          f"@{k}ms recall {ti[k]['recall']:.3f} prec "
                          f"{ti[k]['prec']:.3f} |dt| {ti[k]['dt_ms']:.1f} "
                          f"p95 {ti[k]['p95']:.1f}"
                          for k in scoring.TOL_KEYS if k in ti),
                      flush=True)
            if sp:
                ss = art["script_speed"]
                print(f"[{vid_id}]   speed     fast {sp['fast']:.1f}/min "
                      f"(script {ss['fast']:.1f})  >2x/>3x "
                      f"{sp['x2']:.1f}/{sp['x3']:.1f}  sl>2x/sl>3x "
                      f"{sp['sl2x']:.1f}/{sp['sl3x']:.1f} per slow min "
                      f"({sp['slow_min']:.1f} min, script "
                      f"{ss['sl3x']:.1f}, smooth {sp['sl3x_smooth']:.1f})  "
                      f"stub {sp['stub']:.1f}%  "
                      f"brok {sp['brok']:.1f}/min", flush=True)
        del ds
    # the run's own decode configuration, stamped so downstream readers
    # record what RAN rather than
    # restating a config that can go stale; every flag that changes what
    # a number means is in it
    rec = {"ckpt": str(args.ckpt), "epoch": ck.get("epoch"),
           "basis_id": ck.get("basis_id"), "row_hz": ck.get("row_hz"),
           "dataset": args.dataset, "ids": list(args.ids),
           "val_frac": val_frac, "split": common.VAL_SPLIT,
           "masks_dir": masks_dir, "speed_clamp": ck.get("speed_clamp", True),
           "no_lag": args.no_lag, "h0_cache": args.h0_cache,
           **decode_stamp(args),
           "clips": metrics,
           "pooled": scoring.pool_arm(metrics, args.ids)}
    scoring.write_record(out, rec)
    return rec


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()

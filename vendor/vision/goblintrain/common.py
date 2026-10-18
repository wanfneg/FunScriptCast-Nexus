"""Shared funscript data handling: sanitization, fuzzy-dedup signature, split.

Each raw sample is a JSON array of actions ``{"at": <ms>, "pos": <0..100>}``.
The source data is NOT sanitized -- it contains negative timestamps, timestamps
that go backwards or repeat, pos values outside 0..100, and duplicate files.

Time stays **absolute** (video-aligned): actions live in the video's own time frame,
synced to the flow frames and shot boundaries, so ``sanitize_aligned`` never re-bases
to t=0.
"""

import hashlib
import json
import struct
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import numpy as np

# Fuzzy-dedup signature defaults (see ``script_signature``). The grid coarseness
# IS the fuzziness: larger bins / bigger pos steps collapse more near-duplicates.
DEDUP_BIN_MS = 250
DEDUP_POS_STEP = 10

# Peak playback speed of the device, pos units per second. A transition
# steeper than this is not something a funscript can perform, so targets are
# held to it by DEPTH (``clamp_speed``) and so is the composed decode.
MAX_POS_RATE = 600.0


def sanitize_aligned(raw, duration_ms=None, max_speed=MAX_POS_RATE):
    """Sanitize a funscript while preserving absolute (video-aligned) time.

    Keeps timestamps in the video's own time frame so actions stay synced to the
    paired video (and to the precomputed signal grid). It:
      * clamps pos to [0, 100] (rounded to int),
      * drops actions with negative timestamps,
      * drops actions past the video duration (if ``duration_ms`` is given),
      * keeps only strictly increasing timestamps (drops backwards/duplicates),
      * holds strokes to the device's ``max_speed`` pos/s by reducing their
        DEPTH (:func:`clamp_speed`; ``None`` disables, for ablation).

    Accepts either dicts (``{"at":..,"pos":..}``) or ``(at, pos)`` pairs.
    Returns a list of (at_ms, pos) tuples (absolute time), possibly empty.
    """
    cleaned = []
    for a in raw:
        if isinstance(a, dict):
            at = a.get("at")
            pos = a.get("pos")
        elif isinstance(a, (tuple, list)) and len(a) == 2:
            at, pos = a
        else:
            continue
        if isinstance(at, bool) or isinstance(pos, bool):
            continue
        if not isinstance(at, (int, float)) or not isinstance(pos, (int, float)):
            continue
        if at != at or pos != pos:  # NaN guard
            continue
        if at < 0:
            continue
        if duration_ms is not None and at > duration_ms:
            continue
        p = int(round(pos))
        p = 0 if p < 0 else (100 if p > 100 else p)
        cleaned.append((float(at), p))

    if not cleaned:
        return []

    cleaned.sort(key=lambda x: x[0])
    out = []
    last_at = None
    for at, pos in cleaned:
        if last_at is None or at > last_at:
            out.append((at, pos))
            last_at = at
    return clamp_speed(out, max_speed)


def clamp_speed(clean, max_speed=MAX_POS_RATE):
    """Hold every stroke to the device's peak speed by reducing its DEPTH.

    A funscript cannot be played faster than ``max_speed`` pos/s, so a
    stroke asking for more depth than the time allows is not a stroke the
    device performs -- it is a target the model would be trained to hedge
    against. Timing is never moved (phase is the supervised axis): the
    endpoint is pulled toward its predecessor until the transition is
    playable. Causal single pass, so a clamped extremum correctly shortens
    the stroke that leaves it.

    Corpus scale at 600 pos/s: 1.12% of half-strokes clamp, costing 0.16%
    of total travel, and the p99 stroke is 602 pos/s -- the cap sits where
    the corpus's own distribution ends. It also removes outright
    pathologies (a p100 of 71000 pos/s: large dpos across a ~1 ms dt),
    which otherwise land in the extreme velocity bins.
    """
    if max_speed is None or max_speed <= 0 or len(clean) < 2:
        return clean
    out = [clean[0]]
    for at, pos in clean[1:]:
        p_at, p_pos = out[-1]
        lim = max_speed * (at - p_at) / 1000.0
        d = pos - p_pos
        if abs(d) > lim:
            # TRUNCATE toward the predecessor -- rounding could round UP
            # past the limit, and across a 1-2 ms gap half a unit is
            # hundreds of pos/s
            step = int(lim)
            pos = p_pos + (step if d > 0 else -step)
            pos = 0 if pos < 0 else (100 if pos > 100 else pos)
        out.append((at, pos))
    return out


def resample_envelope(clean, bin_ms=DEDUP_BIN_MS):
    """Time-weighted mean pos per ``bin_ms`` bin over [0, last_at].

    ``clean`` is the output of :func:`sanitize_aligned` (sorted, strictly
    increasing ``(at_ms, pos)``). The pos is treated as a step signal -- each
    action's value is held until the next -- and each bin gets the *average* of
    that signal across the bin window (before the first action the first pos is
    held). Averaging (rather than point-sampling on grid lines) is what makes
    the fingerprint robust to ms-level jitter: a transition that straddles a bin
    boundary shifts the mean by only a sliver, which the later pos quantization
    rounds away. Returns a list of float means, or ``[]`` for fewer than 2
    actions (nothing distinctive to fingerprint).
    """
    if len(clean) < 2 or bin_ms <= 0:
        return []
    last_at = clean[-1][0]
    n_bins = int(last_at // bin_ms) + 1
    out = []
    j = 0
    cur = clean[0][1]
    # advance to the value in effect at t=0 (handles actions at/<= 0)
    while j < len(clean) and clean[j][0] <= 0:
        cur = clean[j][1]
        j += 1
    for i in range(n_bins):
        lo = i * bin_ms
        hi = lo + bin_ms
        t = lo
        acc = 0.0
        # integrate the step signal across [lo, hi): add area of each held
        # segment, switching value at every action time inside the bin.
        while j < len(clean) and clean[j][0] < hi:
            nt = clean[j][0]
            if nt > t:
                acc += cur * (nt - t)
                t = nt
            cur = clean[j][1]
            j += 1
        acc += cur * (hi - t)
        out.append(acc / bin_ms)
    return out


def script_signature(clean, bin_ms=DEDUP_BIN_MS, pos_step=DEDUP_POS_STEP):
    """Fuzzy content fingerprint of a sanitized funscript.

    Resamples the pos envelope on a ``bin_ms`` grid (see
    :func:`resample_envelope`) and quantizes each pos into ``pos_step`` buckets,
    then hashes the result. Two scripts collapse to the same signature when they
    describe the same motion even if they differ in whitespace / key order,
    carry ms-level timestamp jitter, or add/drop a few intermediate actions --
    the grid + bucket coarseness absorbs those differences.

    Returns a hex digest, or ``""`` when the script is too short to fingerprint
    (callers treat an empty signature as "not dedupable" and always keep it).
    """
    env = resample_envelope(clean, bin_ms)
    if not env:
        return ""
    step = max(1, int(pos_step))
    quant = bytes(min(255, int((p + step / 2) // step)) for p in env)
    # Tag with the params so signatures from different settings never collide.
    payload = f"{int(bin_ms)}:{step}:".encode() + quant
    return hashlib.md5(payload).hexdigest()


def funscript_position(clean, t_ms, interp="linear", ease=1.0):
    """Sample the funscript position (0..100) at ``t_ms`` (absolute ms array).

    ``clean`` is :func:`sanitize_aligned` output. Funscript convention is
    linear interpolation between actions; before the first / after the last
    action the edge value is held. Returns a float array like ``t_ms``.

    ``interp="cosine"`` eases each segment with a raised cosine instead:
    the same endpoints at the same times, so every reversal (and the
    harness's reading of the script) is unchanged, but the velocity inside
    a half-stroke is a half-sine -- zero at each action, peaking at pi/2
    times the segment's mean speed mid-stroke -- rather than the constant
    a straight segment gives. ``ease`` blends the two within-segment
    curves (1 = the full raised cosine, 0 = the straight segment), so the
    velocity at an action is (1 - ease) of the segment's mean speed. A
    training-target shape choice, never a scoring one: the harness reads
    scripts linearly.
    """
    t_ms = np.asarray(t_ms, dtype=np.float64)
    if not clean:
        return np.zeros_like(t_ms)
    at = np.array([a for a, _ in clean], dtype=np.float64)
    pos = np.array([p for _, p in clean], dtype=np.float64)
    if interp == "linear" or ease <= 0 or len(at) < 2:
        return np.interp(t_ms, at, pos)
    if interp != "cosine":
        raise ValueError(f"unknown interp {interp!r}")
    i = np.clip(np.searchsorted(at, t_ms, side="right") - 1, 0, len(at) - 2)
    u = (t_ms - at[i]) / np.maximum(at[i + 1] - at[i], 1e-9)
    u = np.clip(u, 0.0, 1.0)             # edge hold outside the actions
    w = 0.5 - 0.5 * np.cos(np.pi * u)
    w = ease * w + (1.0 - ease) * u
    return pos[i] + (pos[i + 1] - pos[i]) * w


def funscript_velocity(clean, t_ms, interp="linear", ease=1.0):
    """Differentiated funscript on the ``t_ms`` grid -> (velocity, position).

    Velocity is in pos-units per second (central differences), matching what
    optical flow measures; this is the training target -- flow is velocity,
    so supervision is on the derivative, never absolute position.
    """
    t_ms = np.asarray(t_ms, dtype=np.float64)
    pos = funscript_position(clean, t_ms, interp, ease)
    if len(t_ms) < 2:
        return np.zeros_like(pos), pos
    vel = np.gradient(pos, t_ms / 1000.0)
    return vel, pos


SRC_PROBE_VERSION = 1     # detector identity, stamped on every record so a
                          # changed rule is a rescan and not a silent mixture


def _ffprobe_json(args, timeout=180):
    """One ffprobe call -> parsed JSON, or None. Metadata and timestamps
    only; never decodes a frame, so nothing about the CONTENT is read."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json"],
                             capture_output=True, text=True, timeout=timeout)
        return json.loads(out.stdout or "{}")
    except Exception:
        return None


def _rate_value(text):
    """ffprobe rational ('30000/1001') -> float, or None for 0/0 and junk."""
    if not text:
        return None
    try:
        if "/" in str(text):
            num, den = str(text).split("/", 1)
            num, den = float(num), float(den)
            return num / den if den else None
        v = float(text)
        return v or None
    except (TypeError, ValueError):
        return None


def probe_source_rate(path, samples=5, per_sample=120):
    """Frame-rate character of a SOURCE video: its nominal and average rates
    and whether its timestamps are VARIABLE.

    Why it is recorded: a funscript is authored against the clock of the
    file its author watched. Ours are normalized to a fixed CFR grid, and
    where those two clocks disagree the script's offset is a FUNCTION OF
    TIME -- drift, which no single per-clip lag can express and which is
    therefore tossed rather than corrected. This is the source-side
    covariate that drift screening correlates against; it is evidence, not
    a verdict, because ffmpeg's ``fps=`` filter resamples onto CFR while
    preserving presentation time, so a VFR source is a suspect and not a
    conviction.

    Two ffprobe passes, neither decoding a frame: container metadata, then
    packet timestamps sampled at ``samples`` points spread across the file
    (``per_sample`` packets each). Sampling beats reading every timestamp
    because VFR that appears nowhere in five spread windows cannot move a
    script's timing meaningfully, and the cost stays flat in clip length.

    Returns a dict of ``src_*`` fields; ``src_fps_mode`` is
    ``cfr``/``vfr``/``mixed``/``unknown``. Never raises and never reveals
    ``path`` -- an unreadable source comes back ``unknown``.
    """
    rec = {"src_probe": SRC_PROBE_VERSION, "src_fps_mode": "unknown",
           "src_fps_nominal": None, "src_fps_avg": None,
           "src_vfr_frac": None, "src_n_intervals": 0}
    meta = _ffprobe_json(
        ["-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,avg_frame_rate,nb_frames,duration,time_base:"
         "format=duration", path])
    if meta:
        st = (meta.get("streams") or [{}])[0]
        rec["src_fps_nominal"] = _rate_value(st.get("r_frame_rate"))
        rec["src_fps_avg"] = _rate_value(st.get("avg_frame_rate"))
        dur = (st.get("duration")
               or (meta.get("format") or {}).get("duration"))
        try:
            rec["src_duration_ms"] = round(float(dur) * 1000.0, 3)
        except (TypeError, ValueError):
            rec["src_duration_ms"] = None
        try:
            rec["src_nb_frames"] = int(st.get("nb_frames"))
        except (TypeError, ValueError):
            rec["src_nb_frames"] = None
    else:
        rec["src_duration_ms"] = rec["src_nb_frames"] = None
        return rec
    # sampled packet timestamps -> the interval distribution
    dur_s = (rec["src_duration_ms"] or 0) / 1000.0
    starts = ([0.0] if dur_s <= 0 else
              [dur_s * f for f in np.linspace(0.02, 0.9, max(1, samples))])
    deltas = []
    for start in starts:
        blk = _ffprobe_json(
            ["-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-read_intervals", f"{start:.3f}%+#{int(per_sample)}", path])
        pts = sorted(
            v for v in (_rate_value(p.get("pts_time"))
                        for p in (blk or {}).get("packets", []))
            if v is not None)                      # sort: decode != display
        deltas.extend(np.diff(pts).tolist())
    d = np.asarray([x for x in deltas if x > 0], dtype=np.float64)
    rec["src_n_intervals"] = int(d.size)
    if d.size < 8:
        return rec
    med = float(np.median(d))
    # tolerance floor absorbs millisecond-quantized container timestamps,
    # which jitter a CFR stream without making it variable
    tol = max(0.01 * med, 0.0011)
    frac = float(np.mean(np.abs(d - med) > tol))
    rec["src_vfr_frac"] = round(frac, 4)
    rec["src_fps_sampled"] = round(1.0 / med, 6) if med > 0 else None
    rec["src_fps_mode"] = ("cfr" if frac <= 0.01 else
                           "vfr" if frac >= 0.05 else "mixed")
    return rec


def npz_member(path, name):
    """Locate one member of an UNCOMPRESSED .npz: (offset, shape, dtype).

    The offset is the first data byte inside the zip, so the member is a
    plain C-ordered array at a known place in the file and row ``i`` starts
    at ``offset + i * itemsize * prod(shape[1:])``."""
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(name + ".npy")
        if info.compress_type != zipfile.ZIP_STORED:
            raise ValueError(f"{name} in {path} is compressed; "
                             "cannot read rows directly")
    with open(path, "rb") as f:
        f.seek(info.header_offset + 26)         # zip local file header
        n_name, n_extra = struct.unpack("<HH", f.read(4))
        f.seek(info.header_offset + 30 + n_name + n_extra)
        version = np.lib.format.read_magic(f)
        read_header = (np.lib.format.read_array_header_1_0
                       if version == (1, 0)
                       else np.lib.format.read_array_header_2_0)
        shape, fortran, dtype = read_header(f)
        if fortran:
            raise ValueError(f"{name} in {path} is fortran-order; "
                             "cannot row-slice it")
        return f.tell(), shape, dtype


def _torch():
    """torch on demand: this module stays importable by the script-only
    tools (gather, source_rate_scan, ...) that never load a model."""
    import torch
    return torch


class RowStream:
    """Rows of an uncompressed .npz member, READ FROM DISK per access.

    The latent corpus is an order of magnitude larger than RAM (389 GB of
    caches against 62 GB) and an epoch is a SHUFFLED single pass, so no
    resident set survives to be reused and a page cache can only ever hold
    a few percent of the tree. Nothing is mapped: a window is pread into
    the caller's buffer and the process holds only the buffers in flight,
    so RAM is a function of batch size and stays flat as the corpus grows.

    Reads are one contiguous range each. The member is C-ordered, so rows
    [a:e) are exactly bytes [a*row_bytes, e*row_bytes) -- a 1536-row dim-64
    window is a single 25 MB sequential read, the shape an NVMe wants.

    The handle is raw and unbuffered -- readinto lands in the destination
    with no intermediate copy -- and guarded by a lock, since the prefetch
    worker and the main thread share one instance."""

    def __init__(self, path, name):
        self.path = str(path)
        self._offset, full_shape, self._dtype = npz_member(path, name)
        self._itemsize = int(self._dtype.itemsize)
        self._row_bytes = int(np.prod(full_shape[1:])) * self._itemsize
        self.shape = tuple(full_shape)
        self._tls = threading.local()
        self._handles = []
        self._handles_lock = threading.Lock()

    def _handle(self):
        """A file object private to THIS thread. Concurrent reads on one
        clip must not share a file position, and a lock around a 56 MB read
        would serialize precisely the batch a reader pool is trying to
        overlap -- an eval batch is 8 segs of the SAME clip. The drive
        wants the queue depth: 2.2 GB/s at depth 1, 6.2 at depth 8."""
        f = getattr(self._tls, "f", None)
        if f is None:
            f = open(self.path, "rb", buffering=0)
            self._tls.f = f
            with self._handles_lock:
                self._handles.append(f)
        return f

    @property
    def dtype(self):
        return _torch().from_numpy(np.empty(0, dtype=self._dtype)).dtype

    def _read_rows(self, a, e):
        """The raw [a:e) rows of the UNDERLYING array as bytes."""
        n = (e - a) * self._row_bytes
        buf = bytearray(n)
        f = self._handle()
        f.seek(self._offset + a * self._row_bytes)
        got = f.readinto(memoryview(buf))
        if got != n:
            raise IOError(f"{self.path}: short read at row {a} "
                          f"({got} of {n} bytes)")
        return buf

    def read_into(self, a, e, out):
        """Rows [a:e) straight into ``out`` (contiguous, right shape/dtype).
        The hot path: disk -> the pinned staging buffer, one copy."""
        arr = out.numpy()
        if not arr.flags["C_CONTIGUOUS"]:
            raise ValueError("read_into needs a contiguous destination")
        n = (e - a) * self._row_bytes
        mv = memoryview(arr).cast("B")
        if mv.nbytes != n:
            raise ValueError(f"destination is {mv.nbytes} bytes, "
                             f"rows [{a}:{e}) are {n}")
        f = self._handle()
        f.seek(self._offset + a * self._row_bytes)
        got = f.readinto(mv)
        if got != n:
            raise IOError(f"{self.path}: short read at row {a} "
                          f"({got} of {n} bytes)")
        return out

    def __len__(self):
        return self.shape[0]

    def __getitem__(self, key):
        """``[a:e)`` -> a fresh CPU tensor of those rows. Row slices only:
        every consumer takes a time span, and anything else would hide a
        full-array read behind an innocent-looking index."""
        if not isinstance(key, slice) or key.step not in (None, 1):
            raise TypeError("RowStream supports contiguous row slices only")
        a, e, _ = key.indices(self.shape[0])
        a, e = max(0, a), max(a, e)
        raw = self._read_rows(a, e)
        arr = np.frombuffer(raw, dtype=self._dtype).reshape(
            (e - a,) + self.shape[1:])
        return _torch().from_numpy(arr)

    def close(self):
        with self._handles_lock:
            handles, self._handles = self._handles, []
        for f in handles:
            try:
                f.close()
            except Exception:
                pass

    def __del__(self):
        self.close()


def npz_memmap(path, name):
    """Memory-map one member of an UNCOMPRESSED .npz. The array stays on
    disk; slices materialize on access and the OS page cache decides what
    lives in RAM -- a cache never has to fit.

    The map is READ-ONLY. Copy-on-write (mode "c") would reserve commit
    charge for the whole mapping on Windows -- at corpus scale that is the
    entire cache in the pagefile budget, which saturates commit and stalls
    everything on pagefile churn. Read-only pages are file-backed, shared,
    and evictable for free. Consumers must copy before mutating.

    For the training corpus use ``RowStream`` instead: mapped pages
    accumulate in the PROCESS working set, and a tree many times the size
    of RAM has no resident set worth keeping. This suits the whole-file
    sequential passes (extraction, requantization) where one clip is
    consumed front to back and the map is the simplest spelling."""
    offset, shape, dtype = npz_member(path, name)
    return np.memmap(path, dtype=dtype, mode="r", shape=shape, offset=offset)


# The repository's weights directory: the ONE frozen basis every latent cache
# projects on, and the released checkpoints.
WEIGHTS_DIR = Path(__file__).resolve().parent.parent / "weights"
BASIS_PATH = str(WEIGHTS_DIR / "pca_basis.npz")

# Fixed directory names inside a project. The row grid, basis and windowing
# are stamped inside every cache file and checked on read, so the names
# carry no identity of their own.
LATENTS_DIR = "latents"
LAG_DIR = "lag"
MASKS_DIR = "masks"
H0_DIR = "h0"

# The recipes the releases trained with; ``train`` uses the default one
# when neither --from nor --init-from names a release.
RECIPES_DIR = WEIGHTS_DIR.parent / "recipes"
DEFAULT_RECIPE = "v0.6.0"


def recipe_window_s(recipe, trunk=True):
    """The longest training window that ``recipe`` runs, in seconds. A clip
    shorter than it gives that stage no training window. ``trunk`` False:
    the heads stage alone (``train --from``)."""
    t = int(recipe["trunk"].get("win", 1536))     # jepa_train's default
    h = int(recipe["heads"].get("win") or t)      # jepa_refit: the trunk's
    return max(t if trunk else 0, h) / ROW_HZ


def default_recipe_window_s():
    """``recipe_window_s`` of the default recipe."""
    p = RECIPES_DIR / f"{DEFAULT_RECIPE}.json"
    return recipe_window_s(json.loads(p.read_text("utf-8")))


def level_pool_rows(times_ms):
    """Odd row width of the ~2 s level low-pass on THIS cache's row grid.

    The level target is a wall-clock smoother, so its row width follows the
    row rate the cache's own timestamps carry: 63 rows at 30 rows/s. Odd width
    keeps avg_pool1d's symmetric padding length-preserving (an even kernel
    returns L+1 rows).
    """
    step = float(np.median(np.diff(np.asarray(times_ms, dtype=float))))
    n = int(round(LEVEL_SMOOTH_S * (1000.0 / step)))
    return n + 1 if n % 2 == 0 else n


EXTRACT_BLOCK_ROWS = 64          # rows per extraction context block, counted
                                 # from cache row 0: the encoder forwards the
                                 # timeline in fixed blocks, and adjacent rows
                                 # straddling a block edge are ~2x as far
                                 # apart as neighbours inside one.
ROW_HZ = 30.0                    # THE row grid: one latent row per 33.333 ms.
# Every cache, checkpoint and lag sidecar carries its own ``row_hz`` stamp and
# code reads THAT (``row_hz_of`` / ``check_row_hz``); this constant is the
# default for the handful of entry points that have to name a grid before they
# have opened anything. There is no second grid -- a cache that disagrees is a
# hard error, never a silent reinterpretation.

# Shape constants, in SECONDS. A dwell is half a second of parked stroke, a
# refractory is 133 ms: these are durations, and seconds is the unit that says
# so. ``rows_at`` states them on the row grid a cache actually carries, which
# is the only place a row count is ever formed.
#
# Every value here is exact to the millisecond it means. Do not "tidy" one to a
# rounder number: they were tuned as kernel widths and a neighbouring value is
# a different kernel, which is a measured change and not a spelling change.
DWELL_SMOOTH_S = 0.466667        # local-mean width of the dwell park test
DWELL_MIN_S = 0.533333           # shortest span that counts as a dwell
LOCK_SMOOTH_S = 0.466667         # level-lock local mean

# Two DIFFERENT smoothers, kept apart on purpose -- one constant used to serve
# both, and moving it for the decode silently redefined the labels.
EXTREMUM_SMOOTH_S = 0.2          # the DEFINITION of a reversal: the box the
                                 # position track is smoothed with before a
                                 # sign flip of its derivative counts as one.
                                 # The METRIC's width -- jepa_infer
                                 # ``_extrema``, hold_analysis and the
                                 # ms-timing probes all read it, so every arm
                                 # is scored against ONE definition. Not a
                                 # knob: moving it rebases every kappa and
                                 # ms-timing number ever recorded.
LABEL_SMOOTH_S = 0.2             # the box ``reversal_labels`` smooths with,
                                 # which is what the reversal-event head is
                                 # TARGETED on. It sits at the metric's width
                                 # and is a separate constant because the two
                                 # answer different questions: a label may be
                                 # placed by a narrower box without moving the
                                 # definition every score is read at. The
                                 # decode reads NEITHER -- the action writer
                                 # runs on ``REV_SMOOTH_S``.
REV_SMOOTH_S = 0.1               # the DECODE's crossing smoother, what
                                 # ``--rev-smooth-s`` overrides. Bounds the
                                 # fastest reversal the artifact can carry --
                                 # a box wider than a half-stroke erases its
                                 # reversal -- so it is an operating point,
                                 # and 0.1 s is the one this line ships.
LOCK_EDGE_S = 0.2                # seconds the lock's ramp borrows per side
REV_TOL_S = 0.066667             # reversal-timing match tolerance (the
                                 # harness's "±1 frame")
REV_SNAP_S = 0.066667            # radius the decode may move a crossing to
                                 # the reversal head's argmax (--rev-snap-s).
                                 # The optimum is INTERIOR and this is it: at
                                 # 0 the decode loses 0.011 kappa and 2.3 ms
                                 # of |dt| (the snap earns its keep), and
                                 # every wider radius trades corr, kappa and
                                 # fast-band recall for precision it does not
                                 # need. Read in seconds on both sides --
                                 # jepa_infer converts per grid and the
                                 # bundle manifest carries the duration, so
                                 # goblinscript follows without a second copy
EVENT_GAP_S = 0.066667           # the alternating decode's refractory
                                 # (--rev-gap-s)
DECODE_CHUNK_S = 34.133333       # head forward chunk. NOT a free batch size:
                                 # the envelope's AR buffer reseeds at each
                                 # chunk boundary, so the length is part of
                                 # the decode (measured: 1024 vs 2048 rows on
                                 # one clip agreed on 41% of actions). Every
                                 # decoder -- jepa_infer, the bundle manifest,
                                 # goblinscript -- reads THIS, so they cannot
                                 # drift into decoding differently.
DECODE_CTX_S = 34.133333         # real context forwarded on BOTH sides of a
                                 # decode chunk and discarded from the output.
                                 # A kept row then owns a warm TCN receptive
                                 # field and a warm envelope AR history where
                                 # a bare chunk edge sees zero padding and a
                                 # reseeded buffer -- the chunk boundary
                                 # stops being part of the decode. Shared
                                 # with the bundle manifest for the same
                                 # no-drift reason as DECODE_CHUNK_S.
DECODE_SMOOTH_S = 1.0            # the styling decode's level + band-rail
                                 # low-pass. A WALL-CLOCK smoother: a level
                                 # track that wobbles twice as fast makes
                                 # every composed transition steeper
                                 # (measured as speed tail)
LEVEL_SMOOTH_S = 2.066667        # the level target's low-pass

ENV_BASE_HI = 6.0                # the generative envelope's base support,
                                 # the span its bin centres already cover
ENV_DRAW_SEED = 0                # the AUTHORSHIP seed: one integer that
                                 # shifts the whole envelope draw and
                                 # changes nothing else about the decode


def env_base_draw(rows, seed=ENV_DRAW_SEED, hi=ENV_BASE_HI):
    """The flow envelope's base sample for ABSOLUTE row indices ``rows``,
    uniform on [0, hi). The head's sampler is an ODE, so its output is a
    pure function of this draw -- which makes the draw the one place
    ``jepa_infer`` and goblinscript could decode differently, and the
    reason it is defined here rather than drawn from an RNG on each side.

    Row index is the seed material because time is ABSOLUTE in this tree:
    a row means the same instant to both decoders, so both reach the same
    sample without exchanging state, and a decode chunk that reseeds its
    AR buffer does not reseed this. Uniform rather than Gaussian so the
    two languages agree EXACTLY: an integer mix, a shift and one multiply,
    with no ``log`` or ``cos`` to disagree in the last few ULP. The
    envelope is a non-negative speed, so a Gaussian base would also spend
    half its mass in a region the quantity never occupies.

    SplitMix64's finalizer over ``row`` mixed with ``seed``; the top 53
    bits become the mantissa, which is the standard exact construction.
    """
    g = np.uint64(0x9E3779B97F4A7C15)
    with np.errstate(over="ignore"):    # uint64 wrapping IS the mixer
        z = np.asarray(rows, dtype=np.uint64) + np.uint64(seed) * g + g
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        z = z ^ (z >> np.uint64(31))
    return (z >> np.uint64(11)).astype(np.float64) * (2.0 ** -53) * hi


# The released checkpoint every tool reads by default: what `draft` writes
# with, what lag fitting measures a new clip against, what fine-tuning
# starts from.
DEFAULT_CKPT = str(WEIGHTS_DIR / "checkpoints" / "v0.6.0.pt")
ARTIFACT_PRIOR = str(WEIGHTS_DIR / "artifact_prior.json")


def roster_path(project, name):
    return Path(project) / "rosters" / f"{name}.json"


def load_roster(project, name):
    """The clip IDs of a project's roster file, or None when there is none.
    A roster is ``rosters/<name>.json`` holding ``{"ids": [...]}``; it is
    WHAT a number is pooled over, so it lives in one file and every tool
    resolves it by name. An ID that is not in the manifest (a removed clip)
    is skipped. The file keeps it, so ``remove --undo`` puts it back
    (IDs are never reissued)."""
    p = roster_path(project, name)
    if not p.is_file():
        return None
    ids = [str(i) for i in json.loads(p.read_text("utf-8"))["ids"]]
    m = Path(project) / "manifest.jsonl"
    if m.is_file():
        have = {json.loads(ln)["id"] for ln in
                m.read_text("utf-8").splitlines() if ln.strip()}
        ids = [i for i in ids if i in have]
    return ids


def resolve_ids(ids, project):
    """Expand any roster name in an --ids list to its clips, keeping order
    and dropping duplicates. Bare IDs pass through untouched, so a roster
    and a hand-picked clip can be mixed in one call."""
    if isinstance(ids, str):
        ids = [ids]
    out = []
    for spec in ids or []:
        found = load_roster(project, spec) if not str(spec).isdigit() else None
        if found is None and not str(spec).isdigit():
            raise SystemExit(f"{spec!r} is neither a clip id nor a roster in "
                             f"{roster_path(project, spec).parent}")
        for v in (found if found is not None else [spec]):
            if v not in out:
                out.append(v)
    return out


def clip_sigs(project, ids):
    """The script signature of each clip of ``ids``, from the project's
    manifest: the content identity a checkpoint records for the clips it
    trained on (``trained_sigs``)."""
    want, out = set(ids), {}
    p = Path(project) / "manifest.jsonl"
    if p.is_file():
        for line in p.read_text("utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("id") in want and r.get("sig"):
                    out[r["id"]] = r["sig"]
    return out


def trained_ids(ck, project, ids):
    """The clips of ``ids`` that the checkpoint trained on. A checkpoint
    records its training clips by script content (``trained_sigs``), so
    the match holds across projects, renames and clip IDs. A checkpoint
    without that record (the shipped releases, older runs) matches its
    ``corrs0`` IDs only when it trained on this project directory:
    IDs are per project, and the releases' IDs name clips of a corpus no
    project here has."""
    if "trained_sigs" in ck:
        sigs = set(ck["trained_sigs"])
        return {i for i, s in clip_sigs(project, ids).items() if s in sigs}
    ds = ck.get("dataset")
    if ds and Path(ds).resolve() == Path(project).resolve():
        return set(ids) & set(ck.get("corrs0") or {})
    return set()


def ckpt_sha(path):
    """The first 16 hex digits of a checkpoint's SHA-256. A drafts
    directory records it, because a run name or a file name can refer to
    another model later."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def rows_at(seconds, row_hz, odd=False):
    """A duration as a row count on a given row grid (at least one row).
    ``odd`` for symmetric kernels, which need an odd width to stay centered."""
    n = max(1, int(round(float(seconds) * float(row_hz))))
    return n | 1 if odd else n


def cap_working_set(max_gib=44.0):
    """Win32: hard-cap this process's physical working set
    (kernel-enforced, QUOTA_LIMITS_HARDWS_MAX_ENABLE). Long streaming
    passes read the int8 caches through npz_memmap, and Windows keeps
    every touched mapped page in the working set until global pressure
    -- at corpus scale (300+ GiB of caches vs 64 GiB RAM) the process
    climbs toward ALL of physical RAM and starves the machine. Epochs
    are shuffled single passes with near-zero intra-epoch page reuse,
    so the balloon buys no locality: capping is measured
    performance-neutral-to-positive (the epoch right after a full trim
    was the run's fastest). Evicted MAPPED pages go to the OS standby
    list and soft-fault back in microseconds, which is what makes the
    cap cheap.

    The cap must therefore exceed the process's PRIVATE COMMIT -- CUDA-
    pinned staging buffers, heap, and (on WDDM) the system-memory
    backing of device allocations. Private pages have no standby list:
    trimming them writes to the pagefile and faulting them back is a
    disk read, so a cap below private commit leaves the streaming cache
    a zero resident budget and the run thrashes. The stall signature is
    a 3x epoch at near-zero loader io -- the time is hard faults, which
    the io counter cannot see.

    Private commit scales with WINDOW BYTES (a 1536-row dim-64 window
    stages 56.6 MB, and the headroom a recipe needs tracks that) AND with
    CORPUS SIZE: the 311-clip trunk
    commits 38.2 GB where its 154-clip half-corpus cross-fits, identical
    in every other respect, stayed under 32 and never stalled. 44 GiB
    seats that commit resident and leaves ~17 GiB of a 64 GiB box for
    the OS, the page cache and the desktop.

    No-op off Windows. Call ONCE at the start of any long-streaming CLI
    (jepa_train, jepa_refit)."""
    if sys.platform != "win32":
        return
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetProcessWorkingSetSizeEx.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.c_uint32]
    k32.SetProcessWorkingSetSizeEx(
        k32.GetCurrentProcess(), 64 << 20, int(max_gib * (1 << 30)),
        0x2 | 0x4)          # HARDWS_MIN_DISABLE | HARDWS_MAX_ENABLE


def basis_id(mean, components, evals):
    """16-hex identity of a PCA basis: hash of the full projection config.
    ``evals`` set the whitening scale, so two bases sharing components but
    not evals produce different features and get different ids."""
    h = hashlib.sha256()
    for a in (mean, components, evals):
        h.update(np.ascontiguousarray(a, dtype=np.float32).tobytes())
    return h.hexdigest()[:16]


_basis_id_cache = {}


def basis_id_of(basis_path=BASIS_PATH):
    """basis_id of a basis npz on disk; None if the file is absent."""
    if basis_path not in _basis_id_cache:
        try:
            with np.load(basis_path) as z:
                _basis_id_cache[basis_path] = basis_id(
                    z["mean"], z["components"], z["evals"])
        except FileNotFoundError:
            _basis_id_cache[basis_path] = None
    return _basis_id_cache[basis_path]


def stamped_basis_id(npz_path):
    """basis_id stamp of a cache npz; None if unstamped (legacy)."""
    with np.load(npz_path) as z:
        return str(z["basis_id"]) if "basis_id" in z.files else None


def check_basis(cache_path, what="latent cache"):
    """Assert a cache npz was produced by the frozen basis. A mismatch is a
    crash: the features no longer mean what the consumer thinks."""
    expect = basis_id_of(BASIS_PATH)
    if expect is None:
        raise SystemExit(f"the frozen basis is missing at {BASIS_PATH}; "
                         "run: goblintrain fetch")
    got = stamped_basis_id(cache_path)
    if got != expect:
        raise SystemExit(
            f"{what} {cache_path} was produced by basis {got}, but the "
            f"frozen basis is {expect} -- it cannot be read here")


def row_hz_of(times_ms):
    """Row rate of a latent cache in Hz, from its OWN row spacing.

    The grid is a property of the cache, never a module constant: a consumer
    that assumes a rate misreads every DT-scaled quantity by whatever ratio it
    got wrong, silently. Read it here, or assert it with ``check_row_hz``."""
    t = np.asarray(times_ms, dtype=np.float64)
    if t.size < 2:
        return float("nan")
    return 1000.0 / float(np.median(np.diff(t)))


def stamped_row_hz(npz_path):
    """Row rate of a cache npz: its ``row_hz`` stamp, or the rate its own
    row times imply (caches written before the stamp existed)."""
    with np.load(npz_path) as z:
        if "row_hz" in z.files:
            return float(z["row_hz"])
        return row_hz_of(z["times_ms"])


def lag_dir_name(row_hz):
    """The lag sidecar directory. Lag never carries across row grids, and a
    project has exactly one grid (``ROW_HZ``), which every sidecar stamps;
    a clip without a sidecar trains unlagged (lag 0)."""
    check_row_hz(float(row_hz), ROW_HZ, "lag sidecar grid")
    return LAG_DIR


def check_row_hz(got, expect, what="latent cache"):
    """Assert a cache's row grid is the one a checkpoint was trained on.

    Rates apart by more than 1% are DIFFERENT GRIDS: every row-indexed
    constant -- the TCN receptive field, dwell/reversal label spans, level
    pooling, rev-snap -- then means a different duration than it did in
    training, and the trunk is fed a signal it has never seen. ``expect``
    None = a checkpoint from before the stamp: nothing to check against."""
    if expect is None or not np.isfinite(got):
        return
    if abs(got - expect) > 0.01 * expect:
        raise SystemExit(
            f"{what} is a {got:g} rows/s grid but the checkpoint was trained "
            f"at {expect:g} rows/s -- the two are not interchangeable "
            f"(--row-stride subsamples a denser cache onto a coarser grid)")


def local_mean(x, k=7):
    """Centered moving mean, edge-padded (length preserved). Oscillation
    averages out of it; a real traverse does not."""
    k = int(k) | 1
    return np.convolve(np.pad(np.asarray(x, dtype=np.float64), k // 2,
                              mode="edge"), np.ones(k) / k, mode="valid")


def segment_pool1d(x, edges, k, kind="mean"):
    """``avg``/``max``/``min`` pool of width ``k`` taken INSIDE each shot.

    Pooling taken per shot, for the LABELS rather than the decode. The level target is a ~2 s box of the script position and the
    band targets are its rolling min/max, so pooled across a cut every one
    of them ramps for a second either side of the seam -- and a head cannot
    learn a step its target never takes. Pooled inside the shot the target
    steps where the scene does.

    ``x`` is (T,), ``edges`` the clip's shot bounds. Rows outside the edge
    list keep the whole-track pooling.
    """
    import torch.nn.functional as _F
    pool = {"mean": lambda v: _F.avg_pool1d(v, k, stride=1, padding=k // 2,
                                            count_include_pad=False),
            "max": lambda v: _F.max_pool1d(v, k, stride=1, padding=k // 2),
            "min": lambda v: -_F.max_pool1d(-v, k, stride=1,
                                            padding=k // 2)}[kind]
    out = pool(x[None])[0].clone()
    for lo, hi in zip(edges[:-1], edges[1:]):
        lo, hi = int(lo), int(hi)
        if hi - lo > 0:
            out[lo:hi] = pool(x[lo:hi][None])[0]
    return out


def alternating_events(p_peak, p_valley, p_none=None, bias=0.0,
                       min_gap=1):
    """Most-likely ALTERNATING reversal sequence from per-row event
    probabilities -> ``(rows, kinds)``, kind +1 peak / -1 valley.

    A funscript alternates by construction: every peak is followed by a
    valley. That structure is the decode, so this is a 3-state Viterbi over
    the reversal head's own classes (emit nothing / emit peak / emit valley)
    with same-type transitions forbidden -- NOT a probability threshold. The
    head's calibration decides how many events it emits; there is no knob.

    ``p_none`` defaults to ``1 - p_peak - p_valley`` (the head's third class).

    ``min_gap`` (rows) is a refractory (EVENT_GAP_S): after an event the
    next needs ``min_gap`` event-free rows, so consecutive events sit
    ``min_gap + 1`` rows apart (2 rows on the 30 Hz grid = 100 ms). Without
    it a fitted emission prior's surplus lands as adjacent peak/valley pairs
    no author writes -- 33 ms half-strokes at 30 rows/s -- chopping
    sustained fast sections into stubs (the artifact_speed ``stub%``
    defect). 1 = the unconstrained machine, where adjacent rows are legal.
    """
    if np.ndim(min_gap) > 0:
        return _alternating_gapped_var(p_peak, p_valley, p_none, bias,
                                       np.asarray(min_gap))
    if min_gap > 1:
        return _alternating_gapped(p_peak, p_valley, p_none, bias,
                                   int(min_gap))
    pk = np.clip(np.asarray(p_peak, dtype=np.float64), 1e-9, 1.0)
    vl = np.clip(np.asarray(p_valley, dtype=np.float64), 1e-9, 1.0)
    nn_ = np.clip(1.0 - pk - vl if p_none is None
                  else np.asarray(p_none, dtype=np.float64), 1e-9, 1.0)
    # ``bias`` is an EMISSION PRIOR in log-odds, added to both event classes.
    # The MAP sequence's event RATE is set by the head's class prior, which no
    # training weighting calibrates for emission (unweighted CE under-emits
    # badly, inverse-sqrt weighting over-corrects). jepa_infer FITS this scalar
    # on the train region so the emitted rate matches the script's -- the same
    # fit-from-the-train-region precedent as the velocity gain and the level
    # quantile calibration, not a tuned knob.
    lpk, lvl, lnn = np.log(pk) + bias, np.log(vl) + bias, np.log(nn_)
    T = len(pk)
    NEG = -np.inf
    # score[s] = best total log-likelihood with last emitted event of type s
    # (0 = none emitted yet, 1 = peak, 2 = valley)
    score = np.array([0.0, NEG, NEG])
    back = np.zeros((T, 3), dtype=np.int8)      # previous state per state
    emit = np.zeros((T, 3), dtype=np.int8)      # what was emitted to get here
    for i in range(T):
        stay = score + lnn[i]                   # emit nothing this row
        new = stay.copy()
        b = np.array([0, 1, 2], dtype=np.int8)
        e = np.zeros(3, dtype=np.int8)
        # -> peak, legal from "none yet" and from valley
        cand = max(score[0], score[2])
        src = 0 if score[0] >= score[2] else 2
        if cand + lpk[i] > new[1]:
            new[1], b[1], e[1] = cand + lpk[i], src, 1
        # -> valley, legal from "none yet" and from peak
        cand = max(score[0], score[1])
        src = 0 if score[0] >= score[1] else 1
        if cand + lvl[i] > new[2]:
            new[2], b[2], e[2] = cand + lvl[i], src, 2
        score, back[i], emit[i] = new, b, e
    rows, kinds = [], []
    s = int(np.argmax(score))
    for i in range(T - 1, -1, -1):
        if emit[i, s]:
            rows.append(i)
            kinds.append(1 if emit[i, s] == 1 else -1)
        s = int(back[i, s])
    return np.array(rows[::-1], dtype=np.int64), \
        np.array(kinds[::-1], dtype=np.int64)


def _alternating_gapped(p_peak, p_valley, p_none, bias, g):
    """:func:`alternating_events` with a refractory: ``g`` event-free rows
    after every event, so consecutive events sit at least ``g + 1`` rows
    apart. State = (kind of last event, rows since it, saturated at
    ``g``), so the count is ``1 + 2 * (g + 1)``. Exact Viterbi, one
    pass."""
    pk = np.clip(np.asarray(p_peak, dtype=np.float64), 1e-9, 1.0)
    vl = np.clip(np.asarray(p_valley, dtype=np.float64), 1e-9, 1.0)
    nn_ = np.clip(1.0 - pk - vl if p_none is None
                  else np.asarray(p_none, dtype=np.float64), 1e-9, 1.0)
    lpk, lvl_, lnn = np.log(pk) + bias, np.log(vl) + bias, np.log(nn_)
    T = len(pk)
    nA = g + 1                      # ages 0..g-1, then FREE at index g
    NS = 1 + 2 * nA
    free = g
    NEG = -np.inf

    def base(k):                    # k: 0 = peak, 1 = valley
        return 1 + k * nA

    score = np.full(NS, NEG)
    score[0] = 0.0
    back = np.zeros((T, NS), dtype=np.int16)
    emit = np.zeros((T, NS), dtype=np.int8)
    for i in range(T):
        new = np.full(NS, NEG)
        nb = np.zeros(NS, dtype=np.int16)
        ne = np.zeros(NS, dtype=np.int8)
        # emit nothing: every state ages one row (FREE absorbs)
        new[0], nb[0] = score[0] + lnn[i], 0
        for k in (0, 1):
            b0 = base(k)
            new[b0 + 1:b0 + nA] = score[b0:b0 + nA - 1] + lnn[i]
            nb[b0 + 1:b0 + nA] = np.arange(b0, b0 + nA - 1)
            stay = score[b0 + free] + lnn[i]
            if stay > new[b0 + free]:
                new[b0 + free], nb[b0 + free] = stay, b0 + free
        # emit: legal from "none yet" and from the OPPOSITE kind at FREE age
        for k, lp in ((0, lpk[i]), (1, lvl_[i])):
            src_free = base(1 - k) + free
            src = 0 if score[0] >= score[src_free] else src_free
            c = max(score[0], score[src_free]) + lp
            if c > new[base(k)]:
                new[base(k)], nb[base(k)] = c, src
                ne[base(k)] = 1 if k == 0 else 2
        score, back[i], emit[i] = new, nb, ne
    rows, kinds = [], []
    s = int(np.argmax(score))
    for i in range(T - 1, -1, -1):
        if emit[i, s]:
            rows.append(i)
            kinds.append(1 if emit[i, s] == 1 else -1)
        s = int(back[i, s])
    return np.array(rows[::-1], dtype=np.int64), \
        np.array(kinds[::-1], dtype=np.int64)


def _alternating_gapped_var(p_peak, p_valley, p_none, bias, gap_rows):
    """:func:`alternating_events` with a PER-ROW refractory: an event at
    row ``i`` needs ``gap_rows[i]`` event-free rows behind it, so the
    refractory is signed per window instead of one number for every
    clip. The state is (kind of last event, rows since it, saturated at
    the largest gap), and an emission at row ``i`` is legal from every
    age at or past ``gap_rows[i]``. Exact Viterbi, one pass."""
    pk = np.clip(np.asarray(p_peak, dtype=np.float64), 1e-9, 1.0)
    vl = np.clip(np.asarray(p_valley, dtype=np.float64), 1e-9, 1.0)
    nn_ = np.clip(1.0 - pk - vl if p_none is None
                  else np.asarray(p_none, dtype=np.float64), 1e-9, 1.0)
    lpk, lvl_, lnn = np.log(pk) + bias, np.log(vl) + bias, np.log(nn_)
    T = len(pk)
    g = np.clip(np.asarray(gap_rows, dtype=np.int64), 1, None)
    if len(g) != T:
        raise ValueError(f"gap_rows has {len(g)} rows for {T} rows of "
                         "probabilities")
    G = int(g.max())
    nA = G + 1                      # ages 0..G-1, then FREE at index G
    NS = 1 + 2 * nA
    free = G
    NEG = -np.inf

    def base(k):                    # k: 0 = peak, 1 = valley
        return 1 + k * nA

    score = np.full(NS, NEG)
    score[0] = 0.0
    back = np.zeros((T, NS), dtype=np.int16)
    emit = np.zeros((T, NS), dtype=np.int8)
    for i in range(T):
        new = np.full(NS, NEG)
        nb = np.zeros(NS, dtype=np.int16)
        ne = np.zeros(NS, dtype=np.int8)
        new[0], nb[0] = score[0] + lnn[i], 0
        for k in (0, 1):
            b0 = base(k)
            new[b0 + 1:b0 + nA] = score[b0:b0 + nA - 1] + lnn[i]
            nb[b0 + 1:b0 + nA] = np.arange(b0, b0 + nA - 1)
            stay = score[b0 + free] + lnn[i]
            if stay > new[b0 + free]:
                new[b0 + free], nb[b0 + free] = stay, b0 + free
        # emit: from "none yet", or from the OPPOSITE kind at any age at
        # or past this row's own gap
        gi = min(int(g[i]), G)
        for k, lp in ((0, lpk[i]), (1, lvl_[i])):
            ob = base(1 - k)
            span = score[ob + gi:ob + nA]
            j = int(np.argmax(span))
            src_old, c_old = ob + gi + j, span[j]
            src = 0 if score[0] >= c_old else src_old
            c = max(score[0], c_old) + lp
            if c > new[base(k)]:
                new[base(k)], nb[base(k)] = c, src
                ne[base(k)] = 1 if k == 0 else 2
        score, back[i], emit[i] = new, nb, ne
    rows, kinds = [], []
    s = int(np.argmax(score))
    for i in range(T - 1, -1, -1):
        if emit[i, s]:
            rows.append(i)
            kinds.append(1 if emit[i, s] == 1 else -1)
        s = int(back[i, s])
    return np.array(rows[::-1], dtype=np.int64), \
        np.array(kinds[::-1], dtype=np.int64)


def fit_emission_bias_bands(p_peak, p_valley, band_of, targets, lo=-6.0,
                            hi=6.0, iters=16, rounds=2, decode=None):
    """Per-band emission priors: one log-odds scalar per band, fit so the
    MAP alternating decode emits about ``targets[k]`` events on the rows
    of band ``k``. Both Viterbi variants take ``bias`` per row (numpy
    broadcasting), so the decode runs with ``b[band_of]``.

    Exists because the GLOBAL fit provably misplaces a global surplus:
    the refractory pins the fast band's emitted rate at the script's, so
    whatever the head's sum is over, the overflow lands in the slow band
    -- where the head's own band sums say it does not belong (measured:
    the head learns band rates; a uniform prior ignores them). The
    targets are the head's own band sums: script-free, so the fit
    deploys.

    Coordinate bisection, ``rounds`` passes over the bands: each
    coordinate is monotone (more bias in a band -> weakly more events in
    it), the coupling through the alternation constraint is what the
    second round absorbs. Exits a coordinate early on an exact integer
    hit."""
    b = np.zeros(len(targets))
    dec = alternating_events if decode is None else decode
    band_of = np.asarray(band_of)
    for _ in range(rounds):
        for k in range(len(targets)):
            lo_k, hi_k = lo, hi
            for _ in range(iters):
                mid = 0.5 * (lo_k + hi_k)
                b[k] = mid
                rows, _ = dec(p_peak, p_valley, bias=b[band_of])
                n = int((band_of[rows] == k).sum())
                if n == targets[k]:
                    lo_k = hi_k = mid
                    break
                if n < targets[k]:
                    lo_k = mid
                else:
                    hi_k = mid
                if hi_k - lo_k < 1e-3:
                    break
            b[k] = 0.5 * (lo_k + hi_k)
    return b


def dwell_spans(pos, amp=20.0, band=8.0, frac=0.25, smooth=None, min_rows=None,
                row_hz=ROW_HZ):
    """Trapezoid dwells of a position track ->
    ``[(a, b, kind, level, tol, stroke)]`` row spans (b exclusive),
    kind +1 top / -1 bottom.

    A dwell is a LEVEL regime, not a stillness one: the span around a
    major extremum where the stroke is PARKED -- its ``smooth``-row local
    mean stays within ``tol`` of the extreme value -- for >= ``min_rows``
    (~0.5 s). Scripted plateaus are rarely flat -- most carry
    oscillation at the extreme --
    so the test runs on the local mean, which oscillation does not move,
    and the raw track is free to ripple inside the span.

    ``tol`` scales with the smaller adjoining stroke amplitude
    (``max(band, frac * stroke)``) because a plateau's texture scales
    with its stroke: a fixed band admits only the flat ones. Major
    extrema come from a hysteresis walk with threshold ``amp``.

    ``level`` (the parked value), ``tol`` and ``stroke`` come back with
    each span: they are what a draft's behavior inside the dwell is
    judged against (jepa_infer's dwell response -- parked vs traversed,
    scale-relative), and a caller that only wants the spans can unpack
    ``a, b, kind, *_``. Shared by jepa_train (dwell-head labels) and
    jepa_infer (metric + styling windows).

    ``smooth`` and ``min_rows`` default to their tuned DURATIONS restated on
    ``row_hz``: 15 and 16 rows at 30 rows/s."""
    smooth = rows_at(DWELL_SMOOTH_S, row_hz, odd=True) \
        if smooth is None else smooth
    min_rows = rows_at(DWELL_MIN_S, row_hz) if min_rows is None else min_rows
    x = local_mean(pos, smooth)
    ext = []
    lo_i = hi_i = 0
    lo = hi = x[0]
    direction = 0
    for i in range(1, len(x)):
        v = x[i]
        if v > hi:
            hi, hi_i = v, i
        if v < lo:
            lo, lo_i = v, i
        if direction >= 0 and hi - v > amp:
            ext.append((hi_i, hi, +1))
            direction = -1
            lo, lo_i = v, i
        elif direction <= 0 and v - lo > amp:
            ext.append((lo_i, lo, -1))
            direction = +1
            hi, hi_i = v, i
    spans = []
    for j, (i, v, kind) in enumerate(ext):
        adj = [abs(v - ext[k][1]) for k in (j - 1, j + 1)
               if 0 <= k < len(ext) and abs(v - ext[k][1]) > 1e-9]
        stroke = min(adj) if adj else 0.0
        tol = max(band, frac * stroke)
        lo_b = ext[j - 1][0] if j > 0 else 0
        hi_b = ext[j + 1][0] if j + 1 < len(ext) else len(x) - 1
        a = i
        while a > lo_b and abs(x[a - 1] - v) <= tol:
            a -= 1
        b = i
        while b < hi_b and abs(x[b + 1] - v) <= tol:
            b += 1
        if b + 1 - a >= min_rows:
            spans.append((a, b + 1, kind, float(v), float(tol),
                          float(stroke)))
    return spans


def dwell_labels(pos, scripted, row_hz=ROW_HZ):
    """Per-row dwell class of a position track: 0 none / 1 top / 2 bottom.

    The dwell head's target, on SCRIPTED rows only -- a dwell interpolated
    across an unscripted gap is not a label. One definition, shared by the
    co-trained head (jepa_train) and the frozen-trunk refit (jepa_refit),
    so the two can never drift apart."""
    lab = np.zeros(len(pos), dtype=np.int64)
    for a, b, kind, *_ in dwell_spans(np.asarray(pos, dtype=np.float64),
                                      row_hz=row_hz):
        if scripted[a:b].all():
            lab[a:b] = 1 if kind > 0 else 2
    return lab


def reversal_labels(pos, scripted, row_hz=ROW_HZ, smooth_s=None,
                    row_kind=False):
    """Per-row reversal class of a position track: 0 none / 1 peak /
    2 valley -- the frame where the ``smooth_s``-smoothed track's
    derivative changes sign, on SCRIPTED rows only. The reversal-event
    head's target: the marginal's zero crossings localize SLOW reversals
    2-5 frames off (a shallow crossing's position wanders under vmarg
    noise -- the queue-8 attribution), and a per-frame event probability
    reads reversal TIME without going through velocity magnitude.

    ``smooth_s`` defaults to ``LABEL_SMOOTH_S``, which sits at the
    metric's ``EXTREMUM_SMOOTH_S``. A narrower box places an extremum on
    an asymmetric stroke closer to the script's own vertex; it changes
    what the head is TAUGHT and nothing about what any arm is SCORED at,
    which is what makes the width readable as a dose.

    A box of ``k`` rows answers alternation with a period between ``k/2``
    and ``k`` rows with the opposite sign, so at the default width every
    movement of 1.75 to 3.5 rows in sustained alternation is labelled with
    the other kind. ``row_kind`` keeps every label row and flips a label's
    kind when the unsmoothed row position turns the other way within
    ``REV_TOL_S`` of it and never its own way (a peak row sits above the
    row before and not below the row after; a valley likewise)."""
    pos = np.asarray(pos, dtype=np.float64)
    lab = np.zeros(len(pos), dtype=np.int64)
    if len(pos) < 3:
        return lab
    k = rows_at(LABEL_SMOOTH_S if smooth_s is None else smooth_s,
                row_hz, odd=True)                      # 7 rows at 30 rows/s
    # edge-padded so the box never reads past the clip as position 0 --
    # a zero-padded smooth dips toward 0 at both ends, which fabricates
    # an extremum in the first/last half-window of every clip
    s = np.convolve(np.pad(pos, k // 2, mode="edge"),
                    np.ones(k) / k, mode="valid")
    d = np.sign(np.diff(s))
    d[d == 0] = 1
    flips = np.where(d[1:] * d[:-1] < 0)[0] + 1
    for j in flips:
        if scripted[j]:
            lab[j] = 1 if d[j - 1] > 0 else 2
    if row_kind:
        turn = np.zeros(len(pos), dtype=np.int64)
        mid = pos[1:-1]
        turn[1:-1][(mid > pos[:-2]) & (mid >= pos[2:])] = 1
        turn[1:-1][(mid < pos[:-2]) & (mid <= pos[2:])] = 2
        w = rows_at(REV_TOL_S, row_hz)
        for j in np.flatnonzero(lab):
            near = turn[max(0, j - w):j + w + 1]
            other = 3 - lab[j]
            if (near == other).any() and not (near == lab[j]).any():
                lab[j] = other
    return lab


def rdp_actions(actions, eps=1.0):
    """Douglas-Peucker on a funscript action list (vertical deviation,
    pos units): drop every point within ``eps`` of the straight line
    between its kept neighbors. Output hygiene for GENERATED drafts
    (jepa_infer / goblinscript apply eps=1 at write time): a dense
    decode emits near-collinear runs on dwells -- sub-eps ripple wiggle
    and int-rounding plateaus -- that carry no device motion. A real
    reversal deviates by its amplitude and survives any eps below it.
    Hand-written scripts are typically RDP-clean already, so import
    leaves them untouched.
    ``actions`` = [{"at": ms, "pos": 0..100}, ...] sorted by at."""
    if len(actions) < 3:
        return list(actions)
    at = np.array([a["at"] for a in actions], dtype=np.float64)
    pos = np.array([a["pos"] for a in actions], dtype=np.float64)
    keep = np.zeros(len(actions), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(actions) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        t = at[a + 1:b]
        line = pos[a] + (pos[b] - pos[a]) * (t - at[a]) \
            / max(at[b] - at[a], 1e-9)
        d = np.abs(pos[a + 1:b] - line)
        k = int(np.argmax(d))
        if d[k] > eps:
            k += a + 1
            keep[k] = True
            stack.append((a, k))
            stack.append((k, b))
    return [act for act, kp in zip(actions, keep) if kp]


def script_gaps(clean, duration_ms, k=8.0, floor_ms=2000.0):
    """Unscripted spans of a funscript -> ``[(start_ms, end_ms), ...]``.

    A gap is a between-action interval longer than ``max(k * median action
    interval, floor_ms)``, plus the unscripted head/tail. Inside a gap the
    interpolated position/velocity are fabrications, not labels -- callers
    that supervise on the interpolation mask these spans. Same rule as
    ``select_candidates``' coverage feature.
    """
    if not clean:
        return [(0.0, float(duration_ms))]
    at = np.array([a for a, _ in clean], dtype=np.float64)
    dts = np.diff(at)
    thr = max(k * (float(np.median(dts)) if len(dts) else 0.0), floor_ms)
    gaps = []
    if at[0] > thr:
        gaps.append((0.0, float(at[0])))
    for i in np.nonzero(dts > thr)[0]:
        gaps.append((float(at[i]), float(at[i + 1])))
    if duration_ms - at[-1] > thr:
        gaps.append((float(at[-1]), float(duration_ms)))
    return gaps


def xcorr_lag(video_sig, script_sig, fps, max_lag_s=2.0):
    """Per-scene latency between a video motion proxy and the script.

    Cross-correlates (Pearson, per integer frame shift) and returns
    ``(lag_ms, peak_corr)`` where ``lag_ms`` is the offset to ADD to video
    times when sampling the script: positive lag means the script runs
    late relative to the video. Both inputs are 1-D arrays on the same
    ``fps`` grid.
    """
    a = np.asarray(video_sig, dtype=np.float64)
    b = np.asarray(script_sig, dtype=np.float64)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    max_shift = max(1, int(round(max_lag_s * fps)))
    if n < 4 * max_shift:
        return 0.0, 0.0
    best_lag, best_corr = 0, -np.inf
    for k in range(-max_shift, max_shift + 1):
        # corr(a[t], b[t+k]): b shifted k frames earlier into a's frame
        if k >= 0:
            x, y = a[: n - k], b[k:]
        else:
            x, y = a[-k:], b[: n + k]
        sx, sy = x.std(), y.std()
        if sx < 1e-9 or sy < 1e-9:
            continue
        c = float(np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy))
        if c > best_corr:
            best_corr, best_lag = c, k
    if not np.isfinite(best_corr):
        return 0.0, 0.0
    return best_lag / fps * 1000.0, best_corr


def xcorr_lag_subframe(video_sig, script_sig, fps, max_lag_s=1.0, mask=None):
    """Sub-frame latency between a video motion proxy and the script.

    Like :func:`xcorr_lag`, but parabolic-interpolates the correlation peak
    (fit a quadratic through the peak frame and its two neighbours, take the
    vertex) to resolve offsets WITHIN a frame -- integer shifts quantize to
    ``1000/fps`` ms and round sub-frame offsets to 0. Returns
    ``(lag_ms, peak_corr, corr0)``; ``corr0`` is the correlation at zero
    shift, so ``peak_corr - corr0`` is the gain a shift actually buys.

    ``mask`` (bool, len ``n``): rows that actually track the video (scripted
    and NOT review-flagged). Only pairs where BOTH rows are valid contribute
    to a shift's correlation -- freeform / unscripted spans do not track the
    video and would corrupt the timing fit. ``max_lag_s`` doubles as a
    plausibility bound: keep it near the largest real authoring offset so the
    search cannot alias onto a half-stroke-period shift (a polarity-inverted
    script otherwise reads as a ~half-period "lag").
    """
    a = np.asarray(video_sig, dtype=np.float64)
    b = np.asarray(script_sig, dtype=np.float64)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    valid = (np.ones(n, bool) if mask is None
             else np.asarray(mask, bool)[:n])
    ms = max(1, int(round(max_lag_s * fps)))
    if valid.sum() < 4 * ms:
        return 0.0, 0.0, 0.0
    c = {}
    for k in range(-ms, ms + 1):
        if k >= 0:
            x, y, mk = a[:n - k], b[k:], valid[:n - k] & valid[k:]
        else:
            x, y, mk = a[-k:], b[:n + k], valid[-k:] & valid[:n + k]
        x, y = x[mk], y[mk]        # only pairs valid at BOTH t and t+k
        sx, sy = (x.std(), y.std()) if len(x) >= 4 * ms else (0.0, 0.0)
        c[k] = (float(np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy))
                if sx > 1e-9 and sy > 1e-9 else np.nan)
    corr0 = c.get(0, 0.0)
    corr0 = corr0 if np.isfinite(corr0) else 0.0
    ks = [k for k in c if np.isfinite(c[k])]
    if not ks:
        return 0.0, 0.0, corr0
    kp = max(ks, key=lambda k: c[k])
    cm = c[kp]
    cl, cr = c.get(kp - 1), c.get(kp + 1)
    if cl is not None and cr is not None and np.isfinite(cl) and np.isfinite(cr):
        d = cl - 2 * cm + cr
        if d < -1e-9:                          # concave -> real interior max
            dd = max(-0.5, min(0.5, 0.5 * (cl - cr) / d))
            return (kp + dd) / fps * 1000.0, cm - 0.25 * (cl - cr) * dd, corr0
    return kp / fps * 1000.0, cm, corr0


VAL_SEG_S = 133.333333        # eval chunk length (a segment forwards in these)
VAL_SEG_TARGET_S = 64.0       # segment length the spread aims for
MIN_VAL_SEG_S = 16.0          # shortest useful segment: below the eval warmup
                              # context a segment is mostly warm-up, and
                              # stroke-rhythm metrics stop seeing whole dwells
VAL_MIN_SEGS = 3              # fewest segments, however short the clip
SEG_MARGIN_S = 2.0            # val-boundary leakage guard (supervision only)
# Names the held-out GEOMETRY. Stamped on checkpoints and on
# gate_reference.json so a reference measured under another geometry refuses
# to render a verdict instead of comparing numbers from different rows.
VAL_SPLIT = "spread"


def val_regions(T, row_hz, val_frac):
    """Rows a clip holds out from supervision -> ``[(lo, hi), ...]``, sorted.

    THE one definition of "val" for this branch. Training desupervises
    exactly these rows and every scorer measures exactly these rows, so a
    number can never describe material the trunk was fitted on.

    ``n`` equal segments, each centred on its share of the clip: segment k
    covers ``(k+0.5)/n`` of the duration, so head gap, tail gap and every
    interior gap are equal and the sample is unbiased in TIME. A clip is not
    stationary -- camera setup, framing and pace change through it, and the
    ending differs most of all -- so where the sample sits decides what a
    per-clip number means.

    Placement is time-based by design. Shots do not inform WHERE to
    validate: training is cut-blind, the supervision margin guards a
    boundary landing mid-shot, and the audit intersects shots with these
    regions rather than requiring alignment.

    Segments aim for ``VAL_SEG_TARGET_S`` -- long enough that the eval warmup
    context is a small share of each and that rhythm-level metrics see whole
    strokes and dwells, short enough that ``n`` grows with clip length instead
    of the segments becoming coarse blocks.

    ``val_frac >= 1`` holds out everything -- the honest split for a clip the
    trunk never trained on at all (``dataset_oos``, an adopted clip).

    The caller applies ``SEG_MARGIN_S`` when masking SUPERVISION; scoring
    takes these rows unexpanded.
    """
    T = int(T)
    if val_frac >= 1.0:
        return [(0, T)]
    budget = int(val_frac * T)
    floor = max(1, rows_at(MIN_VAL_SEG_S, row_hz))
    if budget < floor:                       # clip too short to sample twice
        return [(max(0, (T - budget) // 2), max(0, (T - budget) // 2) + budget)]
    n = max(VAL_MIN_SEGS, int(round(budget / rows_at(VAL_SEG_TARGET_S,
                                                     row_hz))))
    n = max(1, min(n, budget // floor))      # never below the length floor
    seg = budget // n
    segs = []
    for k in range(n):
        centre = (k + 0.5) / n * T
        lo = int(round(centre - seg / 2))
        lo = max(0, min(lo, T - seg))
        segs.append((lo, lo + seg))
    return segs


def val_mask(T, regions):
    """Boolean row mask, True inside ``regions`` -- the scorers' selector."""
    m = np.zeros(int(T), dtype=bool)
    for lo, hi in regions:
        m[max(0, int(lo)):min(int(T), int(hi))] = True
    return m

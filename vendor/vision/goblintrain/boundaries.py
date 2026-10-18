"""Shot-boundary detection: TransNetV2 per-frame transition probability.

Writes ``<project>/boundaries/<id>.json`` with sorted absolute cut times
(ms). TransNetV2 (a 7.6M-param dilated-3D-conv net) reads 27x48 frames on a
15 fps grid and emits a per-frame transition probability; a run of
``prob > TN_THR`` is one cut, placed at the run's peak frame. Decode
dominates the cost. The trunk reads only ``cuts_ms``, through the cut-flag
channel.
"""
import json
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from . import perception as P
from .project import atomic_write_text

MIN_GAP_S = 0.5         # minimum spacing between cuts
TN_THR = 0.5            # TransNetV2 per-frame transition threshold (standard)
TN_INPUT = (27, 48)     # TransNetV2 fixed input (H, W)


def load_transnet(device):
    """TransNetV2 with its bundled weights, ready for inference.

    The constructor flips on cudnn-deterministic (which forces conv3d onto a
    CPU-only slow kernel) and reseeds torch -- undo the former; the latter is
    harmless because nothing random follows in the same process before the
    next stage seeds itself.
    """
    import transnetv2_pytorch as _mod
    from transnetv2_pytorch import TransNetV2
    model = TransNetV2(device=str(device))
    weights = Path(_mod.__file__).parent / "transnetv2-pytorch-weights.pth"
    model.load_state_dict(torch.load(weights, map_location="cpu"))
    model.eval().to(device)
    torch.use_deterministic_algorithms(False)
    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.benchmark = True
    return model


def transnet_frames(video_path, max_s, device, batch=256):
    """Decode at P.FPS, STREAM-resize to TransNetV2's 27x48 (never hoard the
    full-res frames -- a clip can run hours) -> ((N,27,48,3) uint8 on
    device, times_ms)."""
    tiny, times, buf = [], [], []
    h, w = TN_INPUT

    def flush():
        if not buf:
            return
        x = torch.from_numpy(np.stack(buf)).to(device).permute(0, 3, 1, 2).float()
        x = F.interpolate(x, size=(h, w), mode="bilinear", align_corners=False)
        tiny.append(x.round().clamp(0, 255).to(torch.uint8).permute(0, 2, 3, 1))
        buf.clear()

    for t_ms, frame in P.decode_frames(video_path, fps=P.FPS, max_s=max_s):
        buf.append(frame)
        times.append(t_ms)
        if len(buf) >= batch:
            flush()
    flush()
    return torch.cat(tiny, 0), np.asarray(times, dtype=np.float64)


def transnet_cuts(preds, times_ms):
    """Runs of pred>TN_THR -> each run's peak frame -> cut times (ms), merged
    to MIN_GAP_S spacing."""
    mask = preds > TN_THR
    runs, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            runs.append(i + int(np.argmax(preds[i:j])))
            i = j
        else:
            i += 1
    cuts, min_gap = [], MIN_GAP_S * 1000.0
    for f in runs:
        t = float(times_ms[f])
        if cuts and t - cuts[-1] < min_gap:
            continue
        cuts.append(t)
    return cuts


def analyze_video(label, video_path, duration_ms, model, device):
    """TransNetV2 cut detection over ONE video file -> the payload dict."""
    max_s = duration_ms / 1000.0
    frames, times = transnet_frames(video_path, max_s, device)
    with torch.no_grad():
        single, _ = model.predict_frames(frames, quiet=True)
    preds = single.detach().cpu().numpy().ravel().astype(np.float64)
    cuts = transnet_cuts(preds, times)
    return {
        "id": label, "fps": P.FPS, "analyzed_ms": float(max_s * 1000.0),
        "n_cuts": len(cuts), "cuts_ms": cuts,
        "params": {"detector": "transnetv2", "thr": TN_THR,
                   "min_gap_s": MIN_GAP_S},
    }


def path(project, clip_id):
    return project.root / "boundaries" / f"{clip_id}.json"


def ensure(project, clip_id, model, device, log=print):
    """Write the clip's boundaries record unless it exists. Returns True
    when it was written."""
    out = path(project, clip_id)
    if out.is_file():
        return False
    meta = P.load_meta(project.root, clip_id)
    t0 = time.time()
    payload = analyze_video(clip_id, project.video_path(clip_id),
                            meta["duration_ms"], model, device)
    atomic_write_text(out, json.dumps(payload))
    mins = max(payload["analyzed_ms"] / 60000.0, 1e-9)
    log(f"  [{clip_id}] boundaries: {payload['n_cuts']} cuts "
        f"({payload['n_cuts'] / mins:.1f}/min) in {time.time() - t0:.0f} s")
    return True

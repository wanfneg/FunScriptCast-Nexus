"""Shared perception plumbing: the deploy frame grid + ffmpeg decode.

Single source of truth for how video becomes tool input, imported by
``boundaries.py``, ``extract.py`` and ``jepa_train.py``:

  * the deploy frame grid (15 fps -- strokes are ~1.66 Hz median, so 15 fps
    leaves margin; absolute video-clock times, never re-based),
  * ffmpeg rgb24 pipe decoding,
  * dataset helpers (video/meta/script access by sanitized ID).

The model's perception is V-JEPA 2, loaded and frozen in extract.py
together with the frozen PCA basis. The flow+DINO perception stack (RAFT,
camera compensation, common-grid resampling) lives on ``master``.
"""

import collections
import json
import subprocess
import threading
from pathlib import Path

import numpy as np

FPS = 15.0                       # the deploy frame grid
DECODE_W, DECODE_H = 512, 384    # default decode size

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


# --------------------------------------------------------------------------- #
# ffmpeg decode (raw rgb24 pipe)
# --------------------------------------------------------------------------- #

def decode_frames(video_path, w=DECODE_W, h=DECODE_H, fps=FPS,
                  start_s=0.0, max_s=None, crop=None):
    """Yield ``(t_ms, frame)`` -- frame is an (h, w, 3) uint8 RGB array.

    ``t_ms`` is absolute video time (never re-based): frame i of the fps grid
    sits at ``start_s + i/fps``. ``max_s`` bounds the decoded duration.
    ``crop`` ("W:H:X:Y", source pixels) crops each frame BEFORE the squash
    scale -- the decode-chain form of auto-cropping: same decoder, same
    timestamps, no intermediate encode.
    """
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(video_path)]
    if max_s is not None:
        cmd += ["-t", f"{max_s:.3f}"]
    vf = f"fps={fps},crop={crop}," if crop else f"fps={fps},"
    cmd += ["-vf", vf + f"scale={w}:{h}",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    frame_bytes = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=frame_bytes * 4)
    # stderr is drained on a thread (a corrupt stream can spew past the
    # pipe buffer and deadlock the frame reads); only the tail is kept,
    # and only for the error message below.
    err_tail = collections.deque(maxlen=64)
    drain = threading.Thread(
        target=lambda: err_tail.extend(iter(proc.stderr.readline, b"")),
        daemon=True)
    drain.start()
    try:
        i = 0
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                # natural EOF. A decoder that died mid-stream must not read
                # as a short clip: extraction would write a truncated but
                # structurally valid cache, and the done-marker resume
                # would trust it forever. Raising here is what makes a
                # decode error loud. (An abandoned generator never reaches
                # this branch -- its cleanup runs the finally instead.)
                drain.join(timeout=10)
                rc = proc.wait()
                if rc != 0:
                    # never leak the source path (gather feeds ORIGINAL
                    # files through here): ffmpeg prefixes messages with
                    # the input path, so it is scrubbed before raising
                    msg = b"".join(err_tail).decode("utf-8", "replace")
                    msg = msg.replace(str(video_path), "<video>").strip()
                    raise RuntimeError(
                        f"ffmpeg exited {rc} after {i} decoded frames: "
                        f"{msg[-1000:]}")
                break
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            yield (start_s + i / fps) * 1000.0, frame
            i += 1
    finally:
        proc.stdout.close()
        if proc.poll() is None:      # abandoned mid-decode: stop ffmpeg
            proc.kill()
        proc.wait()
        # the dead process closes the pipe's write end, so the drain
        # thread sees EOF and exits; only then is the read end closed
        # (closing it under the thread's readline is a race)
        drain.join(timeout=10)
        proc.stderr.close()


# --------------------------------------------------------------------------- #
# Dataset helpers
# --------------------------------------------------------------------------- #

def video_path(dataset_dir, vid_id):
    return Path(dataset_dir) / "videos" / f"{vid_id}.mp4"


def load_meta(dataset_dir, vid_id):
    with open(Path(dataset_dir) / "meta" / f"{vid_id}.json",
              encoding="utf-8") as f:
        return json.load(f)


def load_script(dataset_dir, vid_id):
    """Raw funscript actions list (callers sanitize via common.sanitize_aligned).

    Returns ``[]`` when the clip has no script (inference-only clips):
    downstream, velocity targets become zeros and lag fitting no-ops.
    """
    p = Path(dataset_dir) / "scripts" / f"{vid_id}.json"
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    return raw["actions"] if isinstance(raw, dict) else raw

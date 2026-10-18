"""ffprobe and ffmpeg around a source video. Nothing here prints a path:
every message a caller may show refers to the clip some other way, and an
ffmpeg failure comes back as a short generic reason.
"""
import json
import os
import shutil
import subprocess
import threading

import numpy as np

SRC_PROBE_VERSION = 1


def have_tools():
    """The names of the tools missing from PATH, empty when all are there."""
    return [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]


def _ffprobe_json(args, timeout=180):
    """One ffprobe call as parsed JSON, or None. Metadata and timestamps
    only; no frame is decoded."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json"],
                             capture_output=True, text=True, timeout=timeout)
        return json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def probe_duration_ms(path):
    """The container duration in ms, or None when unknown."""
    data = _ffprobe_json(["-show_entries", "format=duration", str(path)], 120)
    try:
        return float((data.get("format") or {})["duration"]) * 1000.0
    except (AttributeError, KeyError, ValueError, TypeError):
        return None


def _rate_value(text):
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
    """The frame-rate character of a source: nominal and average rates and
    whether its timestamps are variable. A funscript is authored against
    the clock of the file its author watched; the transcode normalizes onto
    one constant grid, and where the clocks disagree the script drifts.
    These fields are the evidence the lag fit's drift alarm correlates
    against. Two ffprobe passes, no frame decoded. Never raises."""
    rec = {"src_probe": SRC_PROBE_VERSION, "src_fps_mode": "unknown",
           "src_fps_nominal": None, "src_fps_avg": None,
           "src_vfr_frac": None, "src_n_intervals": 0}
    meta = _ffprobe_json(
        ["-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,avg_frame_rate,nb_frames,duration,time_base:"
         "format=duration", str(path)])
    if not meta:
        rec["src_duration_ms"] = rec["src_nb_frames"] = None
        return rec
    st = (meta.get("streams") or [{}])[0]
    rec["src_fps_nominal"] = _rate_value(st.get("r_frame_rate"))
    rec["src_fps_avg"] = _rate_value(st.get("avg_frame_rate"))
    dur = st.get("duration") or (meta.get("format") or {}).get("duration")
    try:
        rec["src_duration_ms"] = round(float(dur) * 1000.0, 3)
    except (TypeError, ValueError):
        rec["src_duration_ms"] = None
    try:
        rec["src_nb_frames"] = int(st.get("nb_frames"))
    except (TypeError, ValueError):
        rec["src_nb_frames"] = None
    dur_s = (rec["src_duration_ms"] or 0) / 1000.0
    starts = ([0.0] if dur_s <= 0 else
              [dur_s * f for f in np.linspace(0.02, 0.9, max(1, samples))])
    deltas = []
    for start in starts:
        blk = _ffprobe_json(
            ["-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-read_intervals", f"{start:.3f}%+#{int(per_sample)}", str(path)])
        pts = sorted(v for v in (_rate_value(p.get("pts_time"))
                                 for p in (blk or {}).get("packets", []))
                     if v is not None)
        deltas.extend(np.diff(pts).tolist())
    d = np.asarray([x for x in deltas if x > 0], dtype=np.float64)
    rec["src_n_intervals"] = int(d.size)
    if d.size < 8:
        return rec
    med = float(np.median(d))
    tol = max(0.01 * med, 0.0011)
    frac = float(np.mean(np.abs(d - med) > tol))
    rec["src_vfr_frac"] = round(frac, 4)
    rec["src_fps_sampled"] = round(1.0 / med, 6) if med > 0 else None
    rec["src_fps_mode"] = ("cfr" if frac <= 0.01 else
                           "vfr" if frac >= 0.05 else "mixed")
    return rec


def transcode(src, dst, cfg, duration_ms=None, progress=None):
    """Transcode ``src`` to the training grid at ``dst`` (an MP4 whatever the
    name). ``cfg`` is a project's ``transcode`` block. ``progress(fraction)``
    is called as ffmpeg advances. Returns None on success or a short generic
    reason on failure; the destination is removed on failure."""
    fps = float(cfg["fps"])
    gop = max(1, int(round(fps * 2)))
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:{int(cfg['height'])}:flags=bicubic,fps={fps}",
        "-c:v", "libx264", "-crf", str(cfg["crf"]), "-preset", cfg["preset"],
        "-pix_fmt", "yuv420p",
        "-g", str(gop), "-keyint_min", str(gop),
        "-x264-params", "scenecut=0:open-gop=0",
        "-c:a", "aac", "-ac", str(cfg["audio_channels"]),
        "-ar", str(cfg["audio_rate"]), "-b:a", str(cfg["audio_bitrate"]),
        "-movflags", "+faststart",
        "-f", "mp4",
        str(dst),
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except (OSError, subprocess.SubprocessError) as e:
        return f"ffmpeg could not start ({type(e).__name__})"

    def drain(stream):
        for _ in stream:
            pass

    err_t = threading.Thread(target=drain, args=(proc.stderr,), daemon=True)
    err_t.start()
    try:
        for line in proc.stdout:
            if not line.startswith("out_time_us=") or not duration_ms:
                continue
            try:
                cur_ms = int(line.split("=", 1)[1]) / 1000.0
            except ValueError:
                continue
            if progress is not None:
                progress(min(1.0, cur_ms / duration_ms))
        proc.wait(timeout=3600)
    except BaseException:
        proc.kill()
        proc.wait()
        _remove(dst)
        raise
    finally:
        proc.stdout.close()
    err_t.join()
    if proc.returncode != 0:
        _remove(dst)
        return f"ffmpeg failed (exit {proc.returncode})"
    return None


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass

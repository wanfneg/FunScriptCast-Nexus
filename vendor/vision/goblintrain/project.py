"""A project directory: one dataset with its caches, runs and drafts.

The repository holds code and weights; a project holds data. Everything a
command writes lands inside the project, and every change to its records
goes through :mod:`goblintrain.tx`. This module owns the layout, the config,
the manifest, the private map and the lock.

The private map (``private/map.tsv``) maps a clip ID to the original file
it was imported from. It is written by import transactions and read back
only to report duplicates and to answer the user's own question of where a
clip came from. Nothing in this package prints a map entry.
"""
import ctypes
import json
import os
import sys
from pathlib import Path

CONFIG_VERSION = 1

# The perception every cache, checkpoint and bundle is stamped with. It is
# frozen and shared by training and deploy; a project records it so a cache
# from another configuration is refused rather than read.
PERCEPTION = {
    "encoder": "vjepa2.1-vitb",
    "basis_id": "e3803d506540845d",
    "enc_res": 384,
    "grid": 24,
    "dim": 64,
    "clip_len": 32,
    "tubelet_stride": 2,
    "alignments": 4,
    "grid_fps": 30.0,
    "row_hz": 30.0,
}

# The grid a clip is transcoded onto at import.
TRANSCODE = {
    "height": 480,
    "fps": 30.0,
    "crf": 23,
    "preset": "medium",
    "audio_rate": 48000,
    "audio_bitrate": "128k",
    "audio_channels": 2,
}

# Directories a project has. The ones a clip's files live in are searched by
# ID when a clip is removed; the rest belong to runs, drafts and bookkeeping.
CLIP_DIRS = ["videos", "scripts", "meta", "boundaries", "latents", "lag",
             "h0", "masks", "vr"]
OTHER_DIRS = ["rosters", "runs", "drafts", "private", ".tx", ".trash"]

# A clip's video is the transcoded MP4; its script is the sanitized action
# list as JSON (``[{"at": ms, "pos": 0..100}, ...]``), not the source file.
VIDEO_EXT = ".mp4"
SCRIPT_EXT = ".json"
ID_WIDTH = 6
MAP_HEADER = "id\tsource\n"

# IDs count up from 000001. A record without a script (a video imported
# for drafting) carries ``"unscripted": true``.


class ProjectError(SystemExit):
    """A refusal with a message the user can act on."""

    def __init__(self, msg):
        super().__init__(f"goblintrain: {msg}")


# Every project of the command line is a directory in here, named by the
# user. Git ignores this directory.
PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"


def named(name):
    """The directory of the project called ``name``. A name is one plain
    directory name; a path is refused."""
    if (name in ("", ".", "..") or any(c in name for c in "/\\:")
            or Path(name).name != name):
        base = Path(name.rstrip("/\\")).name
        hint = (f"; did you mean: {base}"
                if base not in ("", ".", "..") else "")
        raise ProjectError(f"{name!r} is a path, not a project name. Give "
                           f"the name alone: every project is a directory "
                           f"in {PROJECTS_DIR}{hint}")
    return PROJECTS_DIR / name


class Tee:
    """stdout that also lands in a log file; ``sys.stdout = Tee(path)``
    for the length of a stage, then back."""

    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.out = sys.stdout

    def write(self, s):
        self.out.write(s)
        self.f.write(s)

    def flush(self):
        self.out.flush()
        self.f.flush()

    def isatty(self):
        return False

    def close(self):
        self.f.close()


def atomic_write_bytes(path, data):
    """Write ``data`` to ``path`` so that a reader sees the old file or the
    new one, never a partial one: temp file in the same directory, flush,
    fsync, rename over."""
    path = Path(path)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode("utf-8"))


def _pid_alive(pid):
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Project:
    """One project directory. Use :meth:`create` or :meth:`open`; ``open``
    returns a context manager that holds the project lock and has already
    run transaction recovery."""

    def __init__(self, root):
        self.root = Path(root)
        self.config_path = self.root / "config.json"
        self.manifest_path = self.root / "manifest.jsonl"
        self.map_path = self.root / "private" / "map.tsv"
        self.lock_path = self.root / ".lock"
        self.tx_dir = self.root / ".tx"
        self.trash_dir = self.root / ".trash"
        self._locked = False
        self.config = None

    # -- creation and opening -------------------------------------------

    @classmethod
    def create(cls, root):
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise ProjectError(f"{root} exists and is not empty")
        root.mkdir(parents=True, exist_ok=True)
        for d in CLIP_DIRS + OTHER_DIRS:
            (root / d).mkdir()
        p = cls(root)
        p.config = {"version": CONFIG_VERSION, "perception": dict(PERCEPTION),
                    "transcode": dict(TRANSCODE)}
        atomic_write_text(p.config_path,
                          json.dumps(p.config, indent=1) + "\n")
        atomic_write_bytes(p.manifest_path, b"")
        atomic_write_text(p.map_path, MAP_HEADER)
        # A project is never committed. This ignores everything in it even
        # if someone runs git init inside or copies it into a repository.
        atomic_write_text(root / ".gitignore", "*\n")
        return p

    @classmethod
    def open(cls, root):
        p = cls(root)
        if not p.config_path.is_file():
            raise ProjectError(f"{p.root} is not a project (no config.json); "
                               f"create one with: goblintrain init "
                               f"{p.root.name}")
        p.config = json.loads(p.config_path.read_text("utf-8"))
        if p.config.get("version") != CONFIG_VERSION:
            raise ProjectError(f"{p.root}: config version "
                               f"{p.config.get('version')!r}, this build "
                               f"reads {CONFIG_VERSION}")
        if p.config.get("perception") != PERCEPTION:
            raise ProjectError(f"{p.root}: the project's perception "
                               "configuration is not the one this build is "
                               "frozen to; its caches cannot be read here")
        for d in CLIP_DIRS + OTHER_DIRS:
            (p.root / d).mkdir(exist_ok=True)
        p._lock()
        try:
            from . import tx
            tx.recover(p)
        except BaseException:
            p._unlock()
            raise
        return p

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._unlock()
        return False

    def _lock(self):
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.lock_path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and _pid_alive(pid):
                    raise ProjectError(f"{self.root} is in use by another "
                                       f"goblintrain (pid {pid})")
                # stale lock from a process that is gone
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self._locked = True
            return
        raise ProjectError(f"{self.root}: could not take the project lock")

    def _unlock(self):
        if self._locked:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            self._locked = False

    # -- the manifest ---------------------------------------------------

    def manifest(self):
        """The clip records, in manifest order."""
        out = []
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    raise ProjectError(f"manifest.jsonl line {n} is not JSON; "
                                       "the file is only ever written whole, "
                                       "so this needs a look")
        return out

    def write_manifest(self, records):
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        atomic_write_text(self.manifest_path, body)

    def ids(self):
        return [r["id"] for r in self.manifest()]

    def retire_id(self, clip_id):
        """Record that ``clip_id`` left the manifest so :meth:`next_id` never
        issues it again: rosters, drafts and eval records still name it."""
        if int(clip_id) > int(self.config.get("removed_top", 0)):
            self.config["removed_top"] = int(clip_id)
            atomic_write_text(self.config_path,
                              json.dumps(self.config, indent=1) + "\n")

    def next_id(self, taken=()):
        """The next free ID: one past the highest in the manifest, the
        highest ever removed and ``taken`` (IDs a transaction in flight has
        claimed)."""
        top = int(self.config.get("removed_top", 0))
        for r in self.manifest():
            if not r.get("negative"):      # records of a retired ID band
                top = max(top, int(r["id"]))
        for i in taken:
            top = max(top, int(i))
        return f"{top + 1:0{ID_WIDTH}d}"

    @staticmethod
    def is_scripted(record):
        """A clip with a script: neither a negative nor a video imported
        for drafting alone."""
        return not (record.get("negative") or record.get("unscripted"))

    # -- the private map ------------------------------------------------

    def map_lines(self):
        """The map's data lines, each ``id\\tsource`` without newline.
        Callers keep these private."""
        text = self.map_path.read_text("utf-8")
        lines = text.split("\n")
        if lines and lines[0] + "\n" == MAP_HEADER:
            lines = lines[1:]
        return [ln for ln in lines if ln.strip()]

    def write_map(self, lines):
        atomic_write_text(self.map_path,
                          MAP_HEADER + "".join(ln + "\n" for ln in lines))

    def map_sources(self):
        """``{id: source}``. Private; never printed."""
        out = {}
        for ln in self.map_lines():
            i, _, src = ln.partition("\t")
            out[i] = src
        return out

    # -- clip files -----------------------------------------------------

    def video_path(self, clip_id):
        return self.root / "videos" / f"{clip_id}{VIDEO_EXT}"

    def script_path(self, clip_id):
        return self.root / "scripts" / f"{clip_id}{SCRIPT_EXT}"

    def clip_files(self, clip_id):
        """Every file that belongs to a clip: ``<id>.*`` and ``<id>_*``
        anywhere under the clip directories."""
        out = []
        for d in CLIP_DIRS:
            base = self.root / d
            if not base.is_dir():
                continue
            for pat in (f"**/{clip_id}.*", f"**/{clip_id}_*"):
                out.extend(p for p in base.glob(pat) if p.is_file())
        return sorted(set(out))

    # -- consistency ----------------------------------------------------

    def verify(self):
        """Problems between the manifest and the files, as messages. Empty
        means consistent. Only the files a clip cannot exist without are
        checked here (video and script); caches are optional by design."""
        problems = []
        recs = self.manifest()
        seen = set()
        for r in recs:
            i = r.get("id")
            if not isinstance(i, str) or len(i) != ID_WIDTH or not i.isdigit():
                problems.append(f"manifest record with a malformed id: {i!r}")
                continue
            if i in seen:
                problems.append(f"{i}: listed twice in the manifest")
            seen.add(i)
            need = [self.video_path(i)]
            if self.is_scripted(r):
                need.append(self.script_path(i))
            for p in need:
                if not p.is_file():
                    problems.append(f"{i}: manifest names it but "
                                    f"{p.relative_to(self.root)} is missing")
        for d, ext in (("videos", VIDEO_EXT), ("scripts", SCRIPT_EXT)):
            for p in sorted((self.root / d).glob(f"*{ext}")):
                if p.stem not in seen:
                    problems.append(f"{d}/{p.name}: present but not in the "
                                    "manifest (a finished import always "
                                    "writes the record; this file was put "
                                    "here some other way)")
        mapped = set(self.map_sources())
        for i in mapped - seen:
            problems.append(f"{i}: in the private map but not in the manifest")
        return problems


# -- the frozen perception on disk ------------------------------------------

def basis_id(mean, components, evals):
    """The identity of a PCA basis: a hash of its arrays as float32."""
    import hashlib
    import numpy as np
    h = hashlib.sha256()
    for a in (mean, components, evals):
        h.update(np.ascontiguousarray(a, dtype=np.float32).tobytes())
    return h.hexdigest()[:16]


def basis_id_of(path):
    import numpy as np
    with np.load(path) as z:
        return basis_id(z["mean"], z["components"], z["evals"])


def latent_stamp_problem(path, perception):
    """Why a latent cache cannot be read under ``perception``, or None.
    Reads the scalar stamps only, never the rows."""
    import numpy as np
    try:
        with np.load(path, allow_pickle=False) as z:
            names = set(z.files)
            need = {"feats", "times_ms", "basis_id", "row_hz", "alignments",
                    "tubelet_stride", "cut_blind"}
            if not need <= names:
                return f"missing stamp(s) {sorted(need - names)}"
            got = {
                "basis_id": str(z["basis_id"]),
                "row_hz": float(z["row_hz"]),
                "alignments": int(z["alignments"]),
                "tubelet_stride": int(z["tubelet_stride"]),
            }
            if not bool(z["cut_blind"]):
                return "not cut-blind"
    except (OSError, ValueError, KeyError) as e:
        return f"unreadable ({type(e).__name__})"
    for k, v in got.items():
        if v != perception[k]:
            return f"{k} is {v!r}, the project is frozen to {perception[k]!r}"
    return None

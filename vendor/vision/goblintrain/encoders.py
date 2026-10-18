"""Frozen video encoders behind one interface.

Extraction only ever needs four things from an encoder: the input resolution it
wants, the spatial grid and token dim it emits, and a forward that turns a window
of decoded frames into raw tokens. Everything downstream of the PCA basis is
128-dim int8 regardless, so the encoder's own dim never propagates past extraction.

Two families, and they do NOT share an API:

* ``hf``  -- V-JEPA 2 via ``transformers`` (the incumbent). Takes (B,T,C,H,W),
  returns ``.last_hidden_state``.
* ``hub`` -- V-JEPA 2.1 via ``torch.hub`` on facebookresearch/vjepa2. There is no
  transformers support and no HF checkpoint for the 2.1 line. The hub entrypoints
  take (B,C,T,H,W) and hand back ``(encoder, predictor)``; we keep the encoder.

Both flatten tokens temporal-major (t, gy, gx) -> ``reshape(-1, grid, grid, dim)``
is valid for either.

Only encoders at or below the incumbent's size are exposed: the encoder is paid once
per clip at extraction AND again on every user video at deploy, so a larger trunk is
permanently more expensive. The 2.1 ViT-g (1B) / ViT-G (2B) tiers are out of scope.
"""
import torch

# name -> (kind, identifier, input resolution, patch size, scope, layer)
#
# ``scope`` namespaces every filesystem artifact an extraction writes (cache
# dirs ``<scope>_pca<dim>cut*``, basis ``<scope>/pca_basis*.npz``, tokstats
# ``<scope>/tokstats*/``), so encoders coexist in ONE tree sharing videos/
# meta/scripts/boundaries. The incumbent's scope is the legacy "vjepa", so
# every existing dataset_v2 path is byte-identical to before scoping existed.
#
# The shipped 2.1 ViT-B is ``vjepa2_1_vitb_dist_vitG_384``: a DISTILLATION of the
# ViT-G (its predictor projects into 1664 channels, the teacher's width), and that
# recipe is not among the released configs. So the resolution it was trained at is
# not recoverable from configs/train_2_1/vitb16 -- those describe a from-scratch
# ViT-B that is not this one -- nor from the weights, which carry no ``pos_embed``
# at all because position is RoPE. What the checkpoint does say is ``384``, in its
# name and its hub entrypoint. 256px is the cheaper read and yields a 16x16 grid,
# 384px the model's own label and a 24x24 one; both are legitimate and which is
# BETTER is an open question (D4).
#
# ``layer`` is a BLOCK INDEX into the encoder's hierarchical tap set
# (None = the final output). The 2.1 ViTs expose taps at
# ``hierarchical_layers`` -- [2, 5, 8, 11] at depth 12 -- each through a
# per-tap LayerNorm (``norms_block``); the "final output" is itself just
# the last tap (block 11 + ``norms_block[-1]``). In the shipped ViT-B
# checkpoint only that FINAL norm carries a trained affine; the
# intermediate norms are affine-at-init to the bit, so an intermediate tap
# = plain normalized block output -- well-conditioned, and the whitened PCA
# basis (fit on the tapped tokens) absorbs any affine regardless. Those
# untouched affines also say that no loss ever reached blocks 2/5/8 in this
# student: whatever deep supervision the 2.1 PRETRAINING line applies, the
# distilled ViT-B was fit at its output alone. Rationale for tapping below
# the top: the last layer is shaped for the predictor/distillation
# interface (semantic, context-heavy) while stroke phase is low-level
# motion, which mid-depth carries more of. Hub family
# only; the tap is selected by setting ``model.out_layers`` (the forward
# then returns the normed tap; token stream stays pure patches, so the
# grid reshape contract is unchanged).
SPECS = {
    "vjepa2-vitl":       ("hf",  "facebook/vjepa2-vitl-fpc64-256", 256, 16,
                          "vjepa", None),
    "vjepa2.1-vitb":     ("hub", "vjepa2_1_vit_base_384",          384, 16,
                          "vjepa21b", None),
    "vjepa2.1-vitb-256": ("hub", "vjepa2_1_vit_base_384",          256, 16,
                          "vjepa21b256", None),
    "vjepa2.1-vitb-l9":  ("hub", "vjepa2_1_vit_base_384",          384, 16,
                          "vjepa21bl9", 8),   # block index 8 = 9th of 12
                          # blocks (2/3 depth), the model's own mid tap
    "vjepa2.1-vitl":     ("hub", "vjepa2_1_vit_large_384",         384, 16,
                          "vjepa21l", None),
}
DEFAULT = "vjepa2.1-vitb"   # the corpus encoder

# The weight file of each hub entrypoint, at Meta's download host. The hub
# repo's own base URL points at localhost (a debug setting left in
# upstream), so its entrypoints cannot download. ``_hub_weights`` puts the
# file into the torch.hub cache under the name the entrypoint asks for;
# the entrypoint then loads it from the cache and does not download.
#
# ``HUB_REPO`` pins the hub code to one commit, so a later upstream change
# cannot change the frozen perception. This commit is the one the shipped
# caches were extracted with (the V-JEPA 2.1 release plus two README
# images). torch.hub validates a ref only when it is a branch or tag head,
# so the load skips that validation.
HUB_REPO = "facebookresearch/vjepa2:204698b45b3712590f06245fbfba32d3be539812"
HUB_URL = "https://dl.fbaipublicfiles.com/vjepa2"
HUB_WEIGHTS = {
    "vjepa2_1_vit_base_384": "vjepa2_1_vitb_dist_vitG_384",
    "vjepa2_1_vit_large_384": "vjepa2_1_vitl_dist_vitG_384",
}


def _hub_weights(ident):
    """Download the weights of hub entrypoint ``ident`` into the torch.hub
    cache when the cache lacks them."""
    import os
    fname = HUB_WEIGHTS[ident] + ".pt"
    target = os.path.join(torch.hub.get_dir(), "checkpoints", fname)
    if not os.path.isfile(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        torch.hub.download_url_to_file(f"{HUB_URL}/{fname}", target)


def hub_encoder(ident):
    """The encoder of hub entrypoint ``ident`` at the pinned commit, on the
    CPU in fp32."""
    _hub_weights(ident)
    out = torch.hub.load(HUB_REPO, ident, trust_repo=True,
                         skip_validation=True)
    return out[0] if isinstance(out, (tuple, list)) else out  # (enc, pred)


def scope(name):
    """Filesystem scope of an encoder's artifacts -- resolvable from the
    name alone (no checkpoint load), so cache-only modes can use it too."""
    if name not in SPECS:
        raise SystemExit(f"unknown encoder {name!r}; "
                         f"choose from {sorted(SPECS)}")
    return SPECS[name][4]

# ImageNet statistics -- both families were trained with them.
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)


_ROPE_FLAG = "_rope_dtype_patched"


def _patch_rope_dtype(model):
    """Make the 2.1 RoPE fp16-safe. Without this, no 2.1 checkpoint runs in fp16.

    ``rotate_queries_or_keys`` builds its rotation from INTEGER positions, so the
    einsum promotes the rotated q/k to fp32 while v stays fp16 -- and SDPA then
    refuses the mixed dtypes ("Expected query, key, and value to have the same
    dtype"). Autocast does not help: it does not reach inside the function.

    The rotation SHOULD be computed in fp32 (sin/cos of large angles in fp16 loses
    real precision), so the fix is to keep the math and cast the RESULT back to the
    input dtype at the boundary.

    The module is resolved from the loaded model, not hardcoded: the 2.1 line lives
    in ``app.vjepa_2_1.models.utils.modules`` while V-JEPA 2 uses
    ``src.models.utils.modules``, and patching the wrong copy silently does nothing.
    """
    import importlib
    for blk in model.blocks:
        m = importlib.import_module(type(blk.attn).__module__)
        if getattr(m, _ROPE_FLAG, False):
            return
        orig = m.rotate_queries_or_keys

        # *args/**kwargs, not (x, pos): the 2.1 signature carries extras the 2.0 one
        # does not (n_registers), and pinning the signature silently breaks it.
        def rotate_cast(x, *a, _orig=orig, **kw):
            return _orig(x, *a, **kw).to(x.dtype)

        m.rotate_queries_or_keys = rotate_cast
        setattr(m, _ROPE_FLAG, True)
        return


class Encoder:
    """Frozen encoder + its shape contract. fp16, eval, no grad."""

    def __init__(self, name=DEFAULT, device="cuda", res=None):
        if name not in SPECS:
            raise SystemExit(f"unknown encoder {name!r}; "
                             f"choose from {sorted(SPECS)}")
        self.name = name
        (self.kind, self.ident, native_res, patch, self.scope,
         self.layer) = SPECS[name]
        # 2.1 uses RoPE, so a non-native resolution is at least loadable -- but it
        # is off-distribution and must be measured, never assumed.
        self.res = res or native_res
        self.native_res = native_res
        self.grid = self.res // patch
        self.device = device

        if self.kind == "hf":
            if self.layer is not None:
                raise SystemExit(f"{name}: layer taps are hub-family only")
            from transformers import AutoModel
            self.model = AutoModel.from_pretrained(
                self.ident, torch_dtype=torch.float16).eval().to(device)
        else:
            out = hub_encoder(self.ident)
            _patch_rope_dtype(out)
            self.model = out.to(device).half().eval()
            if self.layer is not None:
                taps = list(getattr(self.model, "hierarchical_layers", []))
                if self.layer not in taps:
                    raise SystemExit(
                        f"{name}: block index {self.layer} is not one of "
                        f"the encoder's trained taps {taps}")
                # forward now returns [norms_block[tap](block_out)] --
                # the encoder's own tap interface (plain normalization
                # at intermediate depth; see SPECS note)
                self.model.out_layers = [self.layer]

        self._mean = torch.tensor(_MEAN, device=device).view(1, 3, 1, 1)
        self._std = torch.tensor(_STD, device=device).view(1, 3, 1, 1)
        self.dim = self._probe_dim()

    def _probe_dim(self):
        x = torch.zeros(2, self.res, self.res, 3, dtype=torch.uint8,
                        device=self.device)
        with torch.no_grad():
            return int(self.tokens(x).shape[-1])

    def tokens(self, x):
        """(n,H,W,3) uint8 on device -> (n/2 * grid * grid, dim) fp16.

        n must be even: tubelets are 2 frames. Rows come out temporal-major
        (t, gy, gx), so ``reshape(-1, grid, grid, dim)`` is valid for either
        family.
        """
        v = x.permute(0, 3, 1, 2).float() / 255.0
        v = ((v - self._mean) / self._std).half()
        if self.kind == "hf":
            y = self.model(pixel_values_videos=v[None]).last_hidden_state
        else:
            y = self.model(v.permute(1, 0, 2, 3)[None])   # (B,C,T,H,W)
            if isinstance(y, (tuple, list)):
                y = y[-1]
        return y.reshape(-1, y.shape[-1])

    def __repr__(self):
        return (f"Encoder({self.name}, {self.kind}, {self.res}px, "
                f"grid {self.grid}x{self.grid}, dim {self.dim})")

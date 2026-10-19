# rust/

FunScriptCast-Nexus 的 Rust 重写工程。方案与阶段见 [docs/RUST-REWRITE-PLAN.md](../docs/RUST-REWRITE-PLAN.md)。

```powershell
cargo check
cargo run -p nexus-host
```

阶段 0：workspace + host 壳（`/api/state`）；其余 crate 为占位。

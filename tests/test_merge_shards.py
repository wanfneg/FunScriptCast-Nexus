# -*- coding: utf-8 -*-
"""_merge_safetensor_shards 的单测（R68 流式两遍重写的守护）。

构造假分片（真实 safetensors 结构：8 字节头长 + JSON 头 + 数据区），走真函数，
逐张量比对输出字节与偏移。weight_map 故意**交错**指向两个分片且乱序——
流式实现按分片分组拷贝，这个用例专门抓"照 weight_map 遍历序在同一分片上
来回 seek"或"输出头与数据顺序不一致"类回归。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import host_server  # noqa: E402


def _write_shard(path: Path, tensors: dict) -> None:
    """tensors: name -> (dtype, shape, bytes)，按给定顺序写成一个合法 safetensors。"""
    hdr = {}
    off = 0
    payload = b""
    for name, (dt, shape, data) in tensors.items():
        hdr[name] = {"dtype": dt, "shape": shape,
                     "data_offsets": [off, off + len(data)]}
        payload += data
        off += len(data)
    hb = json.dumps(hdr).encode("utf-8")
    hb += b" " * ((8 - len(hb) % 8) % 8)
    path.write_bytes(len(hb).to_bytes(8, "little") + hb + payload)


def _read_merged(path: Path) -> dict:
    raw = path.read_bytes()
    n = int.from_bytes(raw[:8], "little")
    hdr = json.loads(raw[8:8 + n])
    data = raw[8 + n:]
    return hdr, data


def test_merge_interleaved_shards(tmp=None):
    d = Path(tmp or (ROOT / "tmp" / "test_merge_shards"))
    d.mkdir(parents=True, exist_ok=True)
    s1 = d / "model-00001-of-00002.safetensors"
    s2 = d / "model-00002-of-00002.safetensors"
    t_a = ("F32", [2, 2], bytes(range(16)))          # 16B
    t_b = ("F16", [4], b"\x01\x02\x03\x04\x05\x06\x07\x08")  # 8B
    t_c = ("I64", [1], b"\xaa" * 8)                  # 8B
    # 分片内容：shard1 = {a, c}，shard2 = {b}
    _write_shard(s1, {"a": t_a, "c": t_c})
    _write_shard(s2, {"b": t_b})
    # weight_map 交错且乱序：b(s2) → a(s1) → c(s2 里没有！c 在 s1)……
    # 故意让相邻两项来自不同分片，抓"顺序拷贝假设"
    idx = {"weight_map": {"b": s2.name, "a": s1.name, "c": s1.name}}
    (d / "model.safetensors.index.json").write_text(
        json.dumps(idx), encoding="utf-8")

    host_server._merge_safetensor_shards(d)

    out = d / "model.safetensors"
    assert out.is_file(), "合并产物缺失"
    hdr, data = _read_merged(out)
    # 三张量字节逐一比对
    assert data[hdr["a"]["data_offsets"][0]:hdr["a"]["data_offsets"][1]] == t_a[2]
    assert data[hdr["b"]["data_offsets"][0]:hdr["b"]["data_offsets"][1]] == t_b[2]
    assert data[hdr["c"]["data_offsets"][0]:hdr["c"]["data_offsets"][1]] == t_c[2]
    # 数据区紧凑无空洞（偏移首尾相接、覆盖整个数据区）
    spans = sorted(hdr[k]["data_offsets"] for k in ("a", "b", "c"))
    assert spans[0][0] == 0
    for (_, e0), (s1_, _) in zip(spans, spans[1:]):
        assert e0 == s1_, f"数据区有空洞：{spans}"
    assert spans[-1][1] == len(data)
    # 头部 8 字节对齐
    n = int.from_bytes(out.read_bytes()[:8], "little")
    assert n % 8 == 0
    # dtype/shape 保真
    assert hdr["a"]["dtype"] == "F32" and hdr["a"]["shape"] == [2, 2]
    assert hdr["c"]["dtype"] == "I64" and hdr["c"]["shape"] == [1]
    # 分片与索引已清理
    assert not s1.exists() and not s2.exists()
    assert not (d / "model.safetensors.index.json").exists()
    assert not (d / "model.safetensors.tmp").exists()
    print(f"  ✓ 交错分片合并：3 张量字节/偏移/dtype 全对，分片已清理（{out.stat().st_size}B）")


def test_merge_noop_without_index(tmp=None):
    d = Path(tmp or (ROOT / "tmp" / "test_merge_noindex"))
    d.mkdir(parents=True, exist_ok=True)
    # 无索引文件：必须是干净的无操作（不能报错、不能写东西）
    host_server._merge_safetensor_shards(d)
    assert not (d / "model.safetensors").exists()
    print("  ✓ 无索引目录：无操作通过")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        test_merge_interleaved_shards(t1)
        test_merge_noop_without_index(t2)
    print("merge 单测全部通过")

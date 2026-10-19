import { useCallback, useEffect, useState } from "react";
import { getJson, type LibraryState } from "../api/client";

export default function Library() {
  const [lib, setLib] = useState<LibraryState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    const ac = new AbortController();
    setLoading(true);
    getJson<LibraryState>("/api/v2/library/state", ac.signal)
      .then((d) => { setLib(d); setErr(null); })
      .catch((e: Error) => { if (e.name !== "AbortError") setErr(e.message); })
      .finally(() => setLoading(false));
    return () => ac.abort();
  }, []);

  useEffect(() => load(), [load]);

  return (
    <div className="page">
      <header className="page-head">
        <h1>媒体库</h1>
        <p>只读视图（P1 第一批迁移）。扫描/添加目录等写操作仍在旧界面。</p>
      </header>

      {err ? <div className="alert">接口未通：{err}</div> : null}

      <section className="grid">
        <div className="card">
          <div className="card-label">媒体数量</div>
          <div className="card-value">{lib ? lib.count : "—"}</div>
        </div>
        <div className="card">
          <div className="card-label">状态</div>
          <div className="card-value">
            {lib ? (lib.scanning ? `扫描中 ${lib.progress ?? 0}%` : "空闲") : "—"}
          </div>
          <div className="card-hint">{lib?.last_scan ? `上次扫描：${lib.last_scan}` : ""}</div>
        </div>
        <div className="card">
          <div className="card-label">媒体根</div>
          <div className="card-value">{lib?.roots.length ?? "—"}</div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>媒体目录</h2>
          <button className="btn" onClick={load} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
        {lib && lib.roots.length > 0 ? (
          <ul className="bullets">
            {lib.roots.map((r) => <li key={r}><code>{r}</code></li>)}
          </ul>
        ) : (
          <p className="muted">还没有添加媒体目录。</p>
        )}
      </section>
    </div>
  );
}

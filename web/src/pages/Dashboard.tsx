import { useEffect, useState } from "react";
import { getJson, type SystemInfo } from "../api/client";

function Card({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className="card-value">{value}</div>
      {hint ? <div className="card-hint">{hint}</div> : null}
    </div>
  );
}

export default function Dashboard() {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    getJson<SystemInfo>("/api/v2/system", ac.signal)
      .then(setSys)
      .catch((e: Error) => {
        if (e.name !== "AbortError") setErr(e.message);
      });
    return () => ac.abort();
  }, []);

  return (
    <div className="page">
      <header className="page-head">
        <h1>总览</h1>
        <p>新底座已接通：前端 Vite + React，后端 app/ 路由注册表，旧界面不受影响。</p>
      </header>

      {err ? <div className="alert">接口未通：{err}</div> : null}

      <section className="grid">
        <Card label="版本" value={sys?.version || "—"} hint="version.json" />
        <Card label="Python" value={sys?.python || "—"} hint={sys?.platform || ""} />
        <Card
          label="前端构建"
          value={sys ? (sys.web_dist.built ? "已构建" : "未构建") : "—"}
          hint={sys?.web_dist.path}
        />
        <Card
          label="旧界面"
          value={sys ? (sys.legacy_ui ? "存在" : "缺失") : "—"}
          hint="ui/index.html（迁移完成前保留）"
        />
      </section>

      <section className="panel">
        <h2>底座约定</h2>
        <ul className="bullets">
          <li><code>app/api/routes/*</code> 注册新接口，前缀 <code>/api/v2</code>。</li>
          <li><code>app/bridge.py</code> 先新后旧：未命中的请求原样回落历史路由。</li>
          <li><code>web/</code> 构建产物由宿主在 <code>/app/</code> 下托管。</li>
          <li>开发期：<code>npm run dev</code>，<code>/api</code> 自动代理到 8790。</li>
        </ul>
      </section>
    </div>
  );
}

import { useEffect, useState } from "react";
import { getJson, type LogEntry, type RuntimeState, type SystemInfo } from "../api/client";

function Card({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className="card-value">{value}</div>
      {hint ? <div className="card-hint">{hint}</div> : null}
    </div>
  );
}

function fmtUptime(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

export default function Dashboard() {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [state, setState] = useState<RuntimeState | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    Promise.all([
      getJson<SystemInfo>("/api/v2/system", ac.signal),
      getJson<RuntimeState>("/api/v2/state", ac.signal),
      getJson<{ ok: boolean; logs: LogEntry[] }>("/api/v2/logs", ac.signal),
    ])
      .then(([s, st, lg]) => {
        setSys(s);
        setState(st);
        setLogs(lg.logs ?? []);
      })
      .catch((e: Error) => {
        if (e.name !== "AbortError") setErr(e.message);
      });
    return () => ac.abort();
  }, []);

  const dlna = state?.dlna;
  const recent = logs.slice(-12).reverse();

  return (
    <div className="page">
      <header className="page-head">
        <h1>总览</h1>
        <p>新底座已接通只读接口：系统、运行状态、日志、媒体库。</p>
      </header>

      {err ? <div className="alert">接口未通：{err}</div> : null}

      <section className="grid">
        <Card label="版本" value={state?.version || sys?.version || "—"}
              hint={`Python ${sys?.python ?? "—"} · ${sys?.channel ?? ""}`} />
        <Card label="运行时长" value={state ? fmtUptime(state.uptime) : "—"}
              hint={state ? `端口 ${state.host.port} · 头显 ${state.host.lan_api_port}` : ""} />
        <Card label="DLNA" value={dlna ? (dlna.running ? "运行中" : "未启动") : "—"}
              hint={dlna?.url ?? dlna?.error ?? ""} />
        <Card label="前端构建" value={sys?.web_dist.built ? "已构建" : "未构建"}
              hint={sys?.web_dist.path} />
      </section>

      <section className="panel">
        <h2>最近事件</h2>
        {recent.length === 0 ? (
          <p className="muted">暂无日志。</p>
        ) : (
          <ul className="log-list">
            {recent.map((l, i) => (
              <li key={`${l.ts}-${i}`} className={`log-${l.level}`}>
                <span className="log-time">{fmtTime(l.ts)}</span>
                <span className="log-msg">{l.msg}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

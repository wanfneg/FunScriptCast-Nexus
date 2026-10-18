import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import {
  LayoutDashboard, Library, Subtitles, Sparkles, Settings, type LucideIcon,
} from "lucide-react";
import Dashboard from "./pages/Dashboard";
import Placeholder from "./pages/Placeholder";

type NavItem = { to: string; label: string; icon: LucideIcon; blurb: string };

export const NAV: NavItem[] = [
  { to: "/", label: "总览", icon: LayoutDashboard, blurb: "系统状态与入口" },
  { to: "/library", label: "媒体库", icon: Library, blurb: "目录、扫描、脚本索引（迁移中）" },
  { to: "/subtitles", label: "AI 字幕", icon: Subtitles, blurb: "识别 / 翻译 / 模型中心（迁移中）" },
  { to: "/vision", label: "AI 识别", icon: Sparkles, blurb: "实时动作识别与置信度（迁移中）" },
  { to: "/settings", label: "设置", icon: Settings, blurb: "启动、设备、路径（迁移中）" },
];

export default function App() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">FSC</span>
          <div>
            <div className="brand-title">Nexus</div>
            <div className="brand-sub">新底座 · v0.1</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === "/"} className="nav-item">
              <Icon size={18} strokeWidth={1.8} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">旧界面仍在 <code>/</code>，本界面在 <code>/app</code></div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          {NAV.filter((n) => n.to !== "/").map((n) => (
            <Route
              key={n.to}
              path={n.to}
              element={<Placeholder title={n.label} blurb={n.blurb} />}
            />
          ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

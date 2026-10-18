export default function Placeholder({ title, blurb }: { title: string; blurb: string }) {
  return (
    <div className="page">
      <header className="page-head">
        <h1>{title}</h1>
        <p>{blurb}</p>
      </header>
      <section className="panel">
        <h2>迁移计划</h2>
        <p className="muted">
          本页是新底座的占位。迁移顺序：先把只读接口搬过来（列表/状态），
          再搬写接口（保存/启动），每搬一块就用旧接口回归一次。
        </p>
      </section>
    </div>
  );
}

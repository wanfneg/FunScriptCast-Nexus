# 对齐官方补丁: 删运动门 + rev_bias 官方语义(前 8000 行拟合一次)
p = "stream_engine.py"
src = open(p, encoding="utf-8").read()

def rep(old, new):
    global src
    assert src.count(old) == 1, f"{src.count(old)}x: {old[:60]!r}"
    src = src.replace(old, new)

# 1. 删运动门常量
rep(
    'MOTION_GATE_ON = 2.0          # 运动门开(P95 域): 活跃场景实测 >=4, 静止精确为 0\n'
    'MOTION_GATE_OFF = 1.0         # 运动门关(迟滞): 低于此值视作画面静止\n',
    '')

# 2. 删 motion 数组分配
rep(
    '        self.times_ms = np.zeros(self.total_rows, np.float64)\n'
    '        self.motion = np.zeros(self.total_rows, np.float32)  # 每行像素运动能量\n'
    '        self.cuts_ms: list[float] = []',
    '        self.times_ms = np.zeros(self.total_rows, np.float64)\n'
    '        self.cuts_ms: list[float] = []')

# 3. 删 flush 里的能量计算(管对像素差)
rep(
    '''            e_all.append(np.asarray(
                [float(np.percentile(np.abs(buf[a + K].astype(np.float32) - buf[a].astype(np.float32)), 95))
                 for a in aa]))''',
    '')
rep("        wins, metas, aa_all, e_all = [], [], [], []",
    "        wins, metas, aa_all = [], [], []")
rep(
    '''        order = np.argsort(np.concatenate(aa_all), kind="stable")
        feats = np.concatenate(y_list)[order]
        times = np.concatenate(metas)[order]
        energy = np.concatenate(e_all)[order]''',
    '''        order = np.argsort(np.concatenate(aa_all), kind="stable")
        feats = np.concatenate(y_list)[order]
        times = np.concatenate(metas)[order]''')
rep(
    '''            self.times_ms[n0:n0 + n] = times[:n]
            self.motion[n0:n0 + n] = energy[:n]
            self.n_rows += n''',
    '''            self.times_ms[n0:n0 + n] = times[:n]
            self.n_rows += n''')

# 4. 删门应用块(保留 _track_dbg 探针中 motion 部分的移除)
rep(
    '''        mot = self.motion[:gen_end]
        mot_s = np.convolve(mot, np.ones(15) / 15.0, mode="same")''',
    '')
rep(
    '''        # 二元门+迟滞: P95 域里静止行精确为 0(fps 重复帧/真静止), 活跃行 >=4,
        # 1.0-2.0 线性过渡。没有"部分压制区"——那会吃掉快场景的冲程幅度。
        gate_f = np.clip((mot_s - MOTION_GATE_OFF)
                         / (MOTION_GATE_ON - MOTION_GATE_OFF), 0.0, 1.0)
''',
    '')
rep(
    '''        vel_pred[:gen_end] = vel_pred[:gen_end] * gate_f''',
    '')

# 5. 探针里引用 motion 的部分删掉
rep(
    '''                  f"motion[{fs}:{fe}] mean={self.motion[fs:fe].mean():.2f} | "''',
    '')

# 6. rev_bias 官方语义: 首 BIAS_FIT_S(8000 行)拟合一次, 无重拟合/无 behind 跳过
rep(
    '''        behind = self.buffer.gen_until_ms - self._playhead_ms
        if (gen_end >= common.rows_at(REV_BIAS_WARMUP_S, ROW_HZ)
                and gen_end - self._rev_bias_at >= common.rows_at(REV_BIAS_REFIT_S, ROW_HZ)
                and behind >= 60_000   # 缓冲富余 <60s 时跳过, 拟合不再停摆水位
                and np.isfinite(tracks["rev_top"][:gen_end]).any()):''',
    '''        # 官方语义(jepa_infer.run): rev 头存在时拟合一次, 拟合带 =
        # 首 min(BIAS_FIT_S, 前缀) 行; 之后不再重拟合
        if (not getattr(self, "_rev_bias_done", False)
                and gen_end >= common.rows_at(jepa_infer.BIAS_FIT_S, ROW_HZ)
                and np.isfinite(tracks["rev_top"][:gen_end]).any()):''')
rep(
    '''                # 拟合窗口裁到最近 200s(6000 行): 官方 BIAS_FIT_S 本就是上限
                # 语义; 全前缀成本线性增长(T=22000 行实测 7.4s)会周期性停摆水位
                w0 = max(0, gen_end - common.rows_at(200.0, ROW_HZ))
                self._rev_bias = jepa_infer.fit_rev_bias(
                    {"rev_top": tracks["rev_top"][w0:gen_end],
                     "rev_bot": tracks["rev_bot"][w0:gen_end]},
                    vel_pred[w0:gen_end], "stream")''',
    '''                w0 = max(0, gen_end - int(jepa_infer.BIAS_FIT_S * 30.0))
                self._rev_bias = jepa_infer.fit_rev_bias(
                    {"rev_top": tracks["rev_top"][w0:gen_end],
                     "rev_bot": tracks["rev_bot"][w0:gen_end]},
                    vel_pred[w0:gen_end], "stream")
                self._rev_bias_done = True''')

open(p, "w", encoding="utf-8", newline="\n").write(src)
print("对齐官方补丁应用成功")

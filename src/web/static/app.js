/* PolyTail dashboard — vanilla JS, no build step. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // Segmented control: exactly one active button per group.
  function activate(selector, clicked) {
    $$(selector).forEach((btn) => btn.classList.toggle("active", btn === clicked));
  }

  // ── formatting ────────────────────────────────────────────────────────
  const fmt = {
    money(v, showSign) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
      const n = Number(v);
      const sign = showSign && n > 0 ? "+" : "";
      return sign + n.toFixed(2);
    },
    pct(v, digits) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
      const d = digits === undefined ? 2 : digits;
      return (Number(v) * 100).toFixed(d) + "%";
    },
    pp(v) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
      return (Number(v) * 100).toFixed(2) + "pp";
    },
    price(v, d) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
      return Number(v).toFixed(d === undefined ? 4 : d);
    },
    int(v) {
      if (v === null || v === undefined) return "--";
      return Number(v).toLocaleString("en-US");
    },
    cls(v) {
      const n = Number(v);
      if (!Number.isFinite(n) || n === 0) return "";
      return n > 0 ? "pos" : "neg";
    },
    duration(sec) {
      if (sec === null || sec === undefined) return "--";
      let s = Math.max(0, Math.floor(sec));
      if (s < 60) return s + "s";
      const m = Math.floor(s / 60);
      const r = s % 60;
      if (m < 60) return m + "m " + String(r).padStart(2, "0") + "s";
      const h = Math.floor(m / 60);
      return h + "h " + String(m % 60).padStart(2, "0") + "m";
    },
    clock(ts) {
      if (!ts) return "--";
      return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
    },
    date(ts) {
      if (!ts) return "--";
      return new Date(ts * 1000).toLocaleString("zh-CN", {
        hour12: false, month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    },
    esc(s) {
      return String(s === null || s === undefined ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    },
  };

  // ── API ───────────────────────────────────────────────────────────────
  async function api(path, opts) {
    const options = Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {});
    const res = await fetch(path, options);
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      let detail = (body && (body.detail || body.errors)) || res.statusText;
      if (Array.isArray(detail)) detail = detail.join("；");
      if (typeof detail === "object") detail = JSON.stringify(detail);
      const err = new Error(detail || ("HTTP " + res.status));
      err.status = res.status;
      throw err;
    }
    return body;
  }

  let toastTimer = null;
  function toast(message, kind) {
    let el = $("#toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      document.body.appendChild(el);
    }
    el.className = "toast show " + (kind || "");
    el.textContent = message;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast " + (kind || ""); }, 3200);
  }

  // ── SVG chart ─────────────────────────────────────────────────────────
  function sparkline(el, points, options) {
    if (!el) return;
    const opts = options || {};
    const w = el.clientWidth || 560;
    const h = opts.height || 160;
    const padL = 46, padR = 10, padT = 10, padB = 16;
    const values = points.map((p) => Number(p.value));
    const n = values.length;
    el.setAttribute("viewBox", "0 0 " + w + " " + h);
    el.setAttribute("width", w);
    el.setAttribute("height", h);
    if (!n) {
      el.innerHTML = '<text x="' + (w / 2) + '" y="' + (h / 2) + '" text-anchor="middle">暂无数据</text>';
      return;
    }
    let min = Math.min.apply(null, values.concat([0]));
    let max = Math.max.apply(null, values.concat([0]));
    if (max - min < 1e-9) { max += 1; min -= 1; }
    const span = max - min;
    const x = (i) => padL + (n === 1 ? (w - padL - padR) / 2 : (i / (n - 1)) * (w - padL - padR));
    const y = (v) => padT + (1 - (v - min) / span) * (h - padT - padB);
    let svg = "";
    for (let g = 0; g <= 2; g++) {
      const value = min + (span * g) / 2;
      const yy = y(value);
      svg += '<line class="grid-line" x1="' + padL + '" x2="' + (w - padR) + '" y1="' + yy + '" y2="' + yy + '"/>';
      svg += '<text x="' + (padL - 8) + '" y="' + (yy + 3) + '" text-anchor="end">' + value.toFixed(1) + "</text>";
    }
    if (min < 0 && max > 0) {
      svg += '<line class="zero" x1="' + padL + '" x2="' + (w - padR) + '" y1="' + y(0) + '" y2="' + y(0) + '"/>';
    }
    let line = "";
    values.forEach((v, i) => { line += (i === 0 ? "M" : "L") + x(i).toFixed(2) + " " + y(v).toFixed(2) + " "; });
    const last = values[n - 1];
    const color = last >= 0 ? "var(--accent)" : "var(--danger)";
    const area = line + "L" + x(n - 1).toFixed(2) + " " + y(0).toFixed(2) + " L" + x(0).toFixed(2) + " " + y(0).toFixed(2) + " Z";
    svg += '<path d="' + area + '" fill="' + color + '" opacity="0.10"/>';
    svg += '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.8" stroke-linejoin="round"/>';
    svg += '<circle cx="' + x(n - 1).toFixed(2) + '" cy="' + y(last).toFixed(2) + '" r="3" fill="' + color + '"/>';
    el.innerHTML = svg;
  }

  // ── minimal markdown ──────────────────────────────────────────────────
  function mdToHtml(text) {
    const lines = String(text || "").split("\n");
    let html = "", inTable = false, inList = false, inCode = false;
    const inline = (s) => fmt.esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    const closeBlocks = () => {
      if (inTable) { html += "</tbody></table>"; inTable = false; }
      if (inList) { html += "</ul>"; inList = false; }
    };
    lines.forEach((raw) => {
      const line = raw.replace(/\s+$/, "");
      if (line.startsWith("```")) {
        closeBlocks();
        html += inCode ? "</pre>" : "<pre>";
        inCode = !inCode;
        return;
      }
      if (inCode) { html += fmt.esc(line) + "\n"; return; }
      if (!line.trim()) { closeBlocks(); return; }
      if (line.startsWith("|")) {
        const cells = line.split("|").slice(1, -1).map((c) => c.trim());
        if (cells.every((c) => /^:?-{2,}:?$/.test(c))) return;
        if (!inTable) {
          if (inList) { html += "</ul>"; inList = false; }
          html += "<table><thead><tr>" + cells.map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>";
          inTable = true;
        } else {
          html += "<tr>" + cells.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>";
        }
        return;
      }
      if (/^#{1,3}\s/.test(line)) {
        closeBlocks();
        const level = line.match(/^#+/)[0].length;
        html += "<h" + level + ">" + inline(line.replace(/^#+\s*/, "")) + "</h" + level + ">";
        return;
      }
      if (line.startsWith("> ")) { closeBlocks(); html += "<blockquote>" + inline(line.slice(2)) + "</blockquote>"; return; }
      if (/^[-*]\s/.test(line)) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += "<li>" + inline(line.replace(/^[-*]\s*/, "")) + "</li>";
        return;
      }
      closeBlocks();
      html += "<p>" + inline(line) + "</p>";
    });
    closeBlocks();
    if (inCode) html += "</pre>";
    return html;
  }

  // ── shared renderers ──────────────────────────────────────────────────
  function statusTag(status) {
    const map = {
      open: "open", won: "won", lost: "lost", void: "skip", error: "error",
      running: "open", done: "won", cancelled: "skip",
    };
    return '<span class="tag ' + (map[status] || "skip") + '">' + fmt.esc(status) + "</span>";
  }

  function phaseIndex(phase) {
    const map = { WAITING: 0, SCANNING: 1, CONFIRMING: 2, ENTERED: 3, SKIPPED: 3, EXPIRED: 3 };
    return map[phase] === undefined ? 0 : map[phase];
  }

  const PHASE_LABEL = {
    WAITING: "等待入场窗口", SCANNING: "扫描中", CONFIRMING: "等待确认",
    ENTERED: "已入场", SKIPPED: "已放弃", EXPIRED: "未触发",
  };

  function renderPhaseSteps(phase, decided) {
    const labels = ["等待", "扫描", "确认", decided ? "决策" : "执行"];
    const idx = phaseIndex(phase);
    let html = "";
    labels.forEach((label, i) => {
      let cls = "step";
      if (i < idx) cls += " done";
      if (i === idx) cls += " current";
      html += '<div class="' + cls + '"><span class="bullet">' + (i < idx ? "✓" : i + 1) + "</span>" + label + "</div>";
      if (i < labels.length - 1) html += '<div class="step-line"></div>';
    });
    return html;
  }

  function statTile(key, label, value, sub, cls) {
    return '<div class="stat"><div class="k">' + label +
      '</div><div class="v ' + (cls || "") + '"' + (key ? ' id="' + key + '"' : "") + ">" +
      value + '</div><div class="s">' + (sub || "") + "</div></div>";
  }

  function compactStat(label, value, cls) {
    return '<div class="compact-stat"><span>' + label +
      '</span><b class="' + (cls || "") + '">' + value + "</b></div>";
  }

  function renderTradesTable(trades, opts) {
    const options = opts || {};
    if (!trades.length) return '<div class="muted small center" style="padding:18px">暂无交易</div>';
    return "<table><thead><tr>" +
      (options.showPeriod ? "<th>策略</th>" : "") +
      "<th>时间</th><th>方向</th><th class='num'>入场</th><th class='num'>份额</th>" +
      "<th class='num'>成本</th><th class='num'>盈亏</th><th>状态</th></tr></thead><tbody>" +
      trades.map((t) =>
        "<tr>" +
        (options.showPeriod ? "<td><span class='badge gray'>" +
          fmt.esc((t.asset || "BTC") + " " + t.period) + "</span></td>" : "") +
        "<td class='mono'>" + fmt.date(t.entry_ts) + "</td>" +
        "<td><span class='tag " + (t.side === "UP" ? "enter" : "lost") + "'>" + t.side + "</span></td>" +
        "<td class='num'>" + fmt.price(t.entry_price) + "</td>" +
        "<td class='num'>" + fmt.price(t.shares, 2) + "</td>" +
        "<td class='num'>" + fmt.money(t.cost) + "</td>" +
        "<td class='num " + fmt.cls(t.pnl) + "'>" + (t.status === "open" ? "--" : fmt.money(t.pnl, true)) + "</td>" +
        "<td>" + statusTag(t.status) + "</td></tr>"
      ).join("") + "</tbody></table>";
  }

  function renderSignalsTable(signals, showStrategy) {
    if (!signals.length) return '<div class="muted small center" style="padding:18px">暂无信号</div>';
    return "<table><thead><tr>" +
      (showStrategy ? "<th>策略</th>" : "") +
      "<th>时间</th><th>方向</th><th class='num'>触发价</th><th class='num'>确认价</th><th>结果</th></tr></thead><tbody>" +
      signals.map((s) =>
        "<tr>" +
        (showStrategy ? "<td><span class='badge gray'>" +
          fmt.esc((s.asset || "BTC") + " " + s.period) + "</span></td>" : "") +
        "<td class='mono'>" + fmt.date(s.created_at) + "</td>" +
        "<td>" + (s.side || "--") + "</td>" +
        "<td class='num'>" + fmt.price(s.trigger_price) + "</td>" +
        "<td class='num'>" + fmt.price(s.confirm_price) + "</td>" +
        "<td>" + (s.decision === "enter"
          ? "<span class='tag enter'>入场</span>"
          : "<span class='tag skip'>" + fmt.esc(s.skip_reason || "跳过") + "</span>") + "</td></tr>"
      ).join("") + "</tbody></table>";
  }

  // ── dashboard ─────────────────────────────────────────────────────────
  const dashboard = {
    async init() {
      await this.refresh();
      setInterval(() => this.refresh(), 2000);
      $("#stop-all").addEventListener("click", () => this.control("killswitch", "all"));
      $("#release-all").addEventListener("click", () => this.control("release", "all"));
    },
    async refresh() {
      try {
        const data = await api("/api/status");
        this.render(data);
      } catch (err) {
        if (err.status === 401) { location.href = "/login?next=/"; return; }
        const el = $("#conn");
        if (el) { el.textContent = "连接失败: " + err.message; el.className = "badge red"; }
      }
    },
    async control(action, strategy) {
      try {
        await api("/api/control/" + action, {
          method: "POST",
          body: JSON.stringify({ strategy, reason: action === "killswitch" ? "面板停止" : "" }),
        });
        toast(action === "killswitch" ? "已停止新开仓" : "已恢复", "ok");
        this.refresh();
      } catch (err) {
        toast(err.message, "error");
      }
    },
    keyOf(s) {
      const app = s.app || {};
      return app.key || (app.asset + ":" + app.period);
    },

    render(data) {
      const all = data.strategies || [];
      const enabled = all.filter((s) => s.app.enabled !== false);
      const totals = { trades: 0, pnl: 0, cost: 0, wins: 0, open: 0 };
      enabled.forEach((s) => {
        const st = s.stats || {};
        totals.trades += st.trades || 0;
        totals.pnl += st.pnl || 0;
        totals.cost += st.cost || 0;
        totals.wins += st.wins || 0;
        totals.open += (s.recent_trades || []).filter((t) => t.status === "open").length;
      });
      setText("#ov-trades", fmt.int(totals.trades));
      setText("#ov-pnl", fmt.money(totals.pnl, true));
      $("#ov-pnl").className = "v mono " + fmt.cls(totals.pnl);
      setText("#ov-roi", totals.cost ? fmt.pct(totals.pnl / totals.cost) : "--");
      setText("#ov-open", fmt.int(totals.open));
      setText("#ov-winrate", totals.trades ? fmt.pct(totals.wins / totals.trades) : "--");

      const grid = $("#strategy-grid");
      grid.innerHTML = all.length
        ? all.map((s) => this.card(s)).join("")
        : '<div class="card"><div class="muted small center" style="padding:24px">没有策略</div></div>';
      $$("[data-act]", grid).forEach((btn) => {
        btn.addEventListener("click", () => this.control(
          btn.getAttribute("data-act"), btn.getAttribute("data-key")));
      });

      const merged = [];
      enabled.forEach((s) => (s.recent_signals || []).forEach((sig) => merged.push(sig)));
      merged.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      $("#signals-all").innerHTML = renderSignalsTable(merged.slice(0, 12), true);
    },

    card(s) {
      const app = s.app || {};
      const win = s.window;
      const st = s.strategy;
      const q = s.quote;
      const risk = s.risk || {};
      const stats = s.stats || {};
      const params = Object.assign({}, s.params || {}, (st && st.params) || {});
      const period = app.period || "";
      const key = this.keyOf(s);
      const enabled = app.enabled !== false;
      const isPaper = app.paper !== false;
      const modeBadge = isPaper
        ? '<span class="badge blue">纸面</span>'
        : '<span class="badge red">实盘</span>';
      let statusBadge = enabled
        ? '<span class="badge green"><span class="dot pulse"></span>运行</span>'
        : '<span class="badge gray">未启用</span>';
      let action = enabled
        ? '<button class="btn small ghost" data-act="killswitch" data-key="' +
          fmt.esc(key) + '">停止</button>'
        : '<a class="btn small ghost" href="/config">开启</a>';
      if (enabled && app.mode_fallback) {
        statusBadge = '<span class="badge amber">凭证缺失</span>';
      }
      if (enabled && risk.killswitch && risk.killswitch.active) {
        statusBadge = '<span class="badge red">已停止</span>';
        action = '<button class="btn small ghost" data-act="release" data-key="' +
          fmt.esc(key) + '">恢复</button>';
      } else if (enabled && risk.paused) {
        statusBadge = '<span class="badge amber">暂停中</span>';
        action = '<button class="btn small ghost" data-act="resume" data-key="' +
          fmt.esc(key) + '">恢复</button>';
      }

      const remaining = win ? win.remaining_s : 0;
      const periodS = period === "5m" ? 300 : 900;
      const pct = win ? Math.max(0, Math.min(100, (remaining / periodS) * 100)) : 0;
      const up = q ? q.up_mid : null;
      const down = q ? q.down_mid : null;
      const activeSide = st && st.trigger_side;
      const open = (s.recent_trades || []).filter((t) => t.status === "open");
      const trade = open[0];
      const triggerText = st && st.trigger_side
        ? st.trigger_side + " @" + fmt.price(st.trigger_price)
        : "--";
      const confirmText = st && st.confirm_in_s !== null && st.confirm_in_s !== undefined
        ? fmt.duration(st.confirm_in_s) + " 后确认" : "";
      const positionText = trade
        ? trade.side + " @" + fmt.price(trade.entry_price) : "空仓";
      const pnlText = trade
        ? fmt.money(trade.shares - trade.cost - trade.fee, true) + " / " +
          fmt.money(-trade.cost - trade.fee)
        : "";
      const note = win && win.discovery_error
        ? "发现失败: " + win.discovery_error : ((st && st.skip_reason) || "");
      const phaseLabel = win
        ? (PHASE_LABEL[win.phase] || win.phase || "--")
        : (enabled ? "等待窗口" : "未启用");
      const marketText = win && win.market
        ? win.market.slug : (enabled ? "等待市场发现…" : "未启用");
      const sec = (value) => value === null || value === undefined
        ? "--" : Math.round(Number(value)) + "s";
      const amount = params.entry_notional_usd === undefined
        ? "--" : params.entry_notional_usd;

      return '' +
        '<div class="card strategy-card compact-card' + (enabled ? "" : " is-disabled") + '">' +
          '<div class="card-head">' +
            '<div class="card-title plain">' + fmt.esc(app.asset) + " " +
              fmt.esc(period) + " " + modeBadge + "</div>" +
            '<div class="btn-row">' + statusBadge + action + "</div>" +
          "</div>" +
          '<div class="compact-top">' +
            '<div class="countdown">' + (win ? fmt.duration(win.remaining_s) : "--") +
              "<small>距结算</small></div>" +
            '<span class="badge gray">' + fmt.esc(phaseLabel) + "</span>" +
          "</div>" +
          '<div class="progress"><i style="width:' + pct + '%"></i></div>' +
          '<div class="compact-prices">' +
            '<div class="price-chip up' + (activeSide === "UP" ? " active" : "") +
              '"><span>UP</span><b>' + fmt.price(up) + "</b></div>" +
            '<div class="price-chip down' + (activeSide === "DOWN" ? " active" : "") +
              '"><span>DOWN</span><b>' + fmt.price(down) + "</b></div>" +
          "</div>" +
          '<div class="compact-market"><span class="slug mono">' +
            fmt.esc(marketText) + "</span><span>" +
            (win ? fmt.int(win.samples) + " 采样" : "") + "</span></div>" +
          '<div class="compact-meta">' +
            "<span>阈值 <b>" + fmt.price(params.threshold, 2) + "</b></span>" +
            "<span>上限 <b>" + fmt.price(params.max_entry_price, 2) + "</b></span>" +
            "<span>窗口 <b>" + sec(params.entry_window_sec) + "</b></span>" +
            "<span>确认 <b>" + sec(params.confirm_sec) + "</b></span>" +
            "<span>金额 <b>" + amount + "U</b></span>" +
          "</div>" +
          '<div class="compact-meta">' +
            "<span>触发 <b>" + fmt.esc(triggerText) + "</b>" +
              (confirmText ? " · " + confirmText : "") + "</span>" +
            "<span>持仓 <b>" + fmt.esc(positionText) + "</b></span>" +
            (trade ? "<span>若胜/负 <b>" + pnlText + "</b></span>" : "") +
          "</div>" +
          (note ? '<div class="compact-note">' + fmt.esc(note) + "</div>" : "") +
          '<div class="compact-stats">' +
            compactStat("交易", fmt.int(stats.trades)) +
            compactStat("胜率", fmt.pct(stats.win_rate)) +
            compactStat("净收益", fmt.money(stats.pnl, true), fmt.cls(stats.pnl)) +
            compactStat("ROI", fmt.pct(stats.roi), fmt.cls(stats.roi)) +
          "</div>" +
        "</div>";
    },
  };

  function setText(sel, value) {
    const el = $(sel);
    if (el) el.textContent = value;
  }

  // ── trades page ───────────────────────────────────────────────────────
  const tradesPage = {
    mode: "",
    period: "",
    asset: "",
    async init() {
      $$("[data-mode]").forEach((btn) => {
        btn.addEventListener("click", () => {
          this.mode = btn.getAttribute("data-mode");
          activate("[data-mode]", btn);
          this.load();
        });
      });
      $$("[data-period]").forEach((btn) => {
        btn.addEventListener("click", () => {
          this.period = btn.getAttribute("data-period");
          activate("[data-period]", btn);
          this.load();
        });
      });
      const assetSelect = $("#f-asset");
      if (assetSelect) assetSelect.addEventListener("change", () => {
        this.asset = assetSelect.value;
        this.load();
      });
      await this.load();
    },
    async load() {
      const query = [];
      if (this.mode) query.push("mode=" + this.mode);
      if (this.period) query.push("period=" + this.period);
      if (this.asset) query.push("asset=" + this.asset);
      const d = await api("/api/trades" + (query.length ? "?" + query.join("&") : ""));
      const s = d.stats;
      setText("#t-trades", fmt.int(s.trades));
      setText("#t-winrate", fmt.pct(s.win_rate));
      setText("#t-net", fmt.money(s.pnl, true));
      $("#t-net").className = "v mono " + fmt.cls(s.pnl);
      setText("#t-roi", fmt.pct(s.roi));
      $("#t-roi").className = "v mono " + fmt.cls(s.roi);
      setText("#t-avg", fmt.price(s.avg_entry_price));
      setText("#t-edge", s.price_edge === null || s.price_edge === undefined ? "--" : fmt.pp(s.price_edge));
      setText("#t-dd", fmt.money(s.max_drawdown));
      setText("#t-tstat", s.t_stat === null || s.t_stat === undefined ? "--" : Number(s.t_stat).toFixed(2));
      setText("#t-fees", fmt.money(s.fees) + " U");
      sparkline($("#t-equity"), (d.equity || []).map((p) => ({ value: p.cumulative })), { height: 200 });

      const unredeemed = d.unredeemed || [];
      const banner = $("#redeem-banner");
      if (unredeemed.length) {
        banner.className = "alert warn";
        banner.innerHTML = "有 " + unredeemed.length + " 笔已中奖的仓位尚未赎回（" +
          fmt.money(unredeemed.reduce((a, t) => a + t.shares, 0)) + " 份额）。请到 " +
          "<a href='https://polymarket.com/portfolio' target='_blank' rel='noopener'>Polymarket 持仓页</a> 赎回后再点「已赎回」。";
        $("#redeem-list").innerHTML = "<table><thead><tr><th>策略</th><th>时间</th><th>方向</th><th class='num'>份额</th><th></th></tr></thead><tbody>" +
          unredeemed.map((t) => "<tr><td>" + fmt.esc((t.asset || "BTC") + " " + t.period) + "</td><td class='mono'>" + fmt.date(t.entry_ts) +
            "</td><td>" + t.side + "</td><td class='num'>" + fmt.price(t.shares, 4) +
            "</td><td><button class='btn small' data-redeemed='" + t.id + "'>已赎回</button></td></tr>").join("") +
          "</tbody></table>";
        $$("[data-redeemed]").forEach((btn) => btn.addEventListener("click", async () => {
          await api("/api/trades/" + btn.getAttribute("data-redeemed") + "/redeemed", { method: "POST" });
          toast("已标记", "ok");
          this.load();
        }));
      } else {
        banner.className = "hidden";
        $("#redeem-list").innerHTML = "";
      }

      const rows = d.trades || [];
      if (!rows.length) { $("#all-trades").innerHTML = '<div class="muted small center" style="padding:24px">暂无交易</div>'; return; }
      $("#all-trades").innerHTML = "<table><thead><tr><th>策略</th><th>模式</th><th>时间</th><th>方向</th>" +
        "<th class='num'>信号价</th><th class='num'>成交价</th><th class='num'>滑点</th>" +
        "<th class='num'>份额</th><th class='num'>成本</th><th class='num'>费用</th>" +
        "<th class='num'>盈亏</th><th>结算</th><th>结果</th></tr></thead><tbody>" +
        rows.map((t) => {
          const slip = t.entry_price - t.signal_price;
          return "<tr><td><span class='badge gray'>" + fmt.esc((t.asset || "BTC") + " " + t.period) + "</span></td>" +
            "<td>" + (t.mode === "live" ? "<span class='badge red'>实盘</span>" : "<span class='badge blue'>纸面</span>") + "</td>" +
            "<td class='mono'>" + fmt.date(t.entry_ts) + "</td>" +
            "<td>" + t.side + "</td>" +
            "<td class='num'>" + fmt.price(t.signal_price) + "</td>" +
            "<td class='num'>" + fmt.price(t.entry_price) + "</td>" +
            "<td class='num " + (slip > 0.012 ? "neg" : "") + "'>" + (slip * 100).toFixed(2) + "pp</td>" +
            "<td class='num'>" + fmt.price(t.shares, 2) + "</td>" +
            "<td class='num'>" + fmt.money(t.cost) + "</td>" +
            "<td class='num'>" + fmt.price(t.fee, 3) + "</td>" +
            "<td class='num " + fmt.cls(t.pnl) + "'>" + (t.status === "open" ? "--" : fmt.money(t.pnl, true)) + "</td>" +
            "<td>" + (t.resolved_side || "--") + "</td>" +
            "<td>" + statusTag(t.status) + "</td></tr>";
        }).join("") + "</tbody></table>";
    },
  };

  // ── config page ───────────────────────────────────────────────────────
  // ── config page: one editable row per strategy ─────────────────────────
  const MAIN_FIELDS = [
    { field: "ENTRY_WINDOW_SEC", param: "entry_window_sec", label: "窗口(s)" },
    { field: "ENTRY_THRESHOLD", param: "threshold", label: "阈值" },
    { field: "CONFIRM_SEC", param: "confirm_sec", label: "确认(s)" },
    { field: "MAX_ENTRY_PRICE", param: "max_entry_price", label: "上限" },
  ];

  const configPage = {
    status: [],
    meta: null,

    async init() {
      await this.loadAll();
      $("#cfg-refresh").addEventListener("click", () => this.loadAll());
      $("#kill-btn").addEventListener("click", () => this.control("killswitch"));
      $("#release-btn").addEventListener("click", () => this.control("release"));
      $("#pause-btn").addEventListener("click", () => this.pause());
      $("#resume-btn").addEventListener("click", () => this.control("resume"));
      $("#shared-save").addEventListener("click", () => this.saveShared());
      this.statusLoop();
      setInterval(() => this.statusLoop(), 5000);
    },

    keyOf(s) {
      const app = s.app || {};
      return app.key || (app.asset + ":" + app.period);
    },

    spec(field) {
      return (this.meta && this.meta.specs && this.meta.specs[field]) || {};
    },

    showError(message) {
      const el = $("#cfg-errors");
      if (!el) return;
      el.className = "alert error";
      el.textContent = message;
    },

    hideError() {
      const el = $("#cfg-errors");
      if (el) el.className = "hidden";
    },

    async loadAll() {
      try {
        const [status, meta] = await Promise.all([
          api("/api/status"), api("/api/strategies"),
        ]);
        this.status = status.strategies || [];
        this.meta = meta;
        this.renderTable();
        this.renderSelects();
        await this.loadShared();
        this.statusLoop();
      } catch (err) {
        if (err.status === 401) { location.href = "/login?next=/config"; return; }
        this.showError("列表加载失败: " + err.message);
        const table = $("#strategy-table");
        if (table) {
          table.innerHTML = "<div class='alert error'>列表加载失败: " +
            fmt.esc(err.message) + "</div>";
        }
      }
    },

    inputHtml(field, value) {
      const spec = this.spec(field);
      const step = spec.step || (spec.type === "int" ? 1 : 0.01);
      const v = value === null || value === undefined ? "" : value;
      return "<input type='number' data-field='" + field + "' value='" + v +
        "' min='" + (spec.min === undefined ? "" : spec.min) +
        "' max='" + (spec.max === undefined ? "" : spec.max) +
        "' step='" + step + "'>";
    },

    renderTable() {
      const el = $("#strategy-table");
      if (!el) return;
      if (!this.status.length) {
        el.innerHTML = "<div class='muted small center' style='padding:20px'>没有策略</div>";
        return;
      }
      const head = "<table class='config-table'><thead><tr><th>策略</th><th>启用</th><th>模式</th>" +
        MAIN_FIELDS.map((f) => "<th class='num'>" + f.label + "</th>").join("") +
        "<th></th></tr></thead><tbody>";
      const rows = this.status.map((s) => {
        const app = s.app || {};
        const p = s.params || {};
        const key = this.keyOf(s);
        const mode = app.mode || "paper";
        return "<tr data-key='" + fmt.esc(key) + "' data-mode='" + fmt.esc(mode) + "'>" +
          "<td><span class='badge gray'>" + fmt.esc(app.asset + " " + app.period) + "</span></td>" +
          "<td><input type='checkbox' data-field='ENABLED'" + (app.enabled ? " checked" : "") + "></td>" +
          "<td><select data-field='MODE'>" +
            "<option value='paper'" + (mode === "paper" ? " selected" : "") + ">纸面</option>" +
            "<option value='live'" + (mode === "live" ? " selected" : "") + ">实盘</option>" +
          "</select></td>" +
          MAIN_FIELDS.map((f) => "<td>" + this.inputHtml(f.field, p[f.param]) + "</td>").join("") +
          "<td><button class='btn small ghost' data-rec='" + fmt.esc(key) + "'>推荐</button> " +
          "<button class='btn small' data-save='" + fmt.esc(key) + "'>保存</button></td>" +
          "</tr>";
      }).join("");
      el.innerHTML = head + rows + "</tbody></table>";
      $$("[data-save]", el).forEach((btn) => {
        btn.addEventListener("click", () => this.saveRow(btn.getAttribute("data-save")));
      });
      $$("[data-rec]", el).forEach((btn) => {
        btn.addEventListener("click", () => this.fillRecommended(btn.getAttribute("data-rec")));
      });
    },

    fillRecommended(key) {
      const tr = $$("#strategy-table tr[data-key]").find((row) => row.getAttribute("data-key") === key);
      const meta = (this.meta && this.meta.strategies || []).find((s) => s.key === key);
      if (!tr || !meta || !meta.recommended) return;
      const rec = meta.recommended;
      MAIN_FIELDS.forEach((f) => {
        const input = tr.querySelector("[data-field='" + f.field + "']");
        if (input && rec[f.param] !== undefined && rec[f.param] !== null) {
          input.value = rec[f.param];
        }
      });
      toast(key + " 已填入 docs 推荐值，点「保存」生效", "ok");
    },

    renderSelects() {
      const options = this.status.map((s) => {
        const app = s.app || {};
        const key = this.keyOf(s);
        return "<option value='" + fmt.esc(key) + "'>" +
          fmt.esc(app.asset + " " + app.period) +
          (app.enabled ? "" : "（未启用）") + "</option>";
      }).join("");
      const ctrl = $("#ctrl-strategy");
      if (ctrl) {
        const current = ctrl.value || "all";
        ctrl.innerHTML = "<option value='all'>全部启用的策略</option>" + options;
        ctrl.value = Array.from(ctrl.options).some((o) => o.value === current) ? current : "all";
      }
    },

    async saveRow(key) {
      const tr = $$("#strategy-table tr[data-key]").find((row) => row.getAttribute("data-key") === key);
      if (!tr) return;
      const values = {
        ENABLED: tr.querySelector("[data-field='ENABLED']").checked,
      };
      MAIN_FIELDS.forEach((f) => {
        const input = tr.querySelector("[data-field='" + f.field + "']");
        if (input) values[f.field] = input.value;
      });
      const mode = tr.querySelector("[data-field='MODE']").value;
      const btn = tr.querySelector("[data-save]");
      if (btn) btn.disabled = true;
      try {
        await api("/api/config", {
          method: "PUT",
          body: JSON.stringify({ strategy: key, values }),
        });
        if (mode !== tr.getAttribute("data-mode")) {
          try {
            await api("/api/control/mode", {
              method: "POST",
              body: JSON.stringify({ strategy: key, mode }),
            });
            tr.setAttribute("data-mode", mode);
          } catch (modeErr) {
            tr.querySelector("[data-field='MODE']").value = tr.getAttribute("data-mode");
            throw modeErr;
          }
        }
        this.hideError();
        toast(key + " 已保存", "ok");
        await this.statusLoop();
        this.renderSelects();
      } catch (err) {
        this.showError("保存失败: " + err.message);
        toast("保存失败: " + err.message, "error");
      } finally {
        if (btn) btn.disabled = false;
      }
    },

    async loadShared() {
      const target = $("#shared-fields");
      if (!target) return;
      try {
        const cfg = await api("/api/config/shared");
        const groups = [["sizing", "单笔金额"], ["execution", "执行"], ["risk", "风控"]];
        let html = "";
        groups.forEach(([group, label]) => {
          const items = Object.entries(cfg.specs).filter(([, spec]) => spec.group === group);
          if (!items.length) return;
          html += "<div class='card-title plain' style='grid-column:1/-1;margin-top:6px'>" +
            label + "</div>";
          html += items.map(([key, spec]) => {
            const value = cfg.values[key];
            const step = spec.step || (spec.type === "int" ? 1 : 0.01);
            let input;
            if (spec.type === "bool") {
              input = "<select data-shared='" + key + "'>" +
                "<option value='true'" + (value ? " selected" : "") + ">开启</option>" +
                "<option value='false'" + (value ? "" : " selected") + ">关闭</option></select>";
            } else if (spec.type === "str") {
              input = "<select data-shared='" + key + "'>" + (spec.options || []).map((opt) =>
                "<option value='" + fmt.esc(opt) + "'" + (String(value) === opt ? " selected" : "") +
                ">" + fmt.esc(opt) + "</option>").join("") + "</select>";
            } else {
              input = "<input type='number' data-shared='" + key + "' value='" + value +
                "' min='" + (spec.min === undefined ? "" : spec.min) +
                "' max='" + (spec.max === undefined ? "" : spec.max) +
                "' step='" + step + "'>";
            }
            const overridden = cfg.overrides[key] !== undefined;
            return "<label class='field'><span class='label'>" + fmt.esc(spec.label) +
              (overridden ? " <span class='badge green' style='padding:0 6px;font-size:10px'>已改</span>" : "") +
              "</span>" + input + "<span class='help'>" + fmt.esc(spec.help) + "</span></label>";
          }).join("");
        });
        target.innerHTML = html || "<div class='muted small'>没有共有参数</div>";
        if (cfg.errors && cfg.errors.length) {
          this.showError(cfg.errors.join("；"));
        } else {
          this.hideError();
        }
      } catch (err) {
        target.innerHTML = "<div class='alert error'>共有参数加载失败: " +
          fmt.esc(err.message) + "</div>";
      }
    },

    async saveShared() {
      const values = {};
      $$("[data-shared]", $("#shared-fields")).forEach((el) => {
        values[el.getAttribute("data-shared")] = el.value;
      });
      try {
        await api("/api/config/shared", {
          method: "PUT",
          body: JSON.stringify({ values }),
        });
        this.hideError();
        toast("共有参数已保存，14 个策略下一个窗口生效", "ok");
        await this.loadShared();
      } catch (err) {
        this.showError("保存失败: " + err.message);
        toast("保存失败: " + err.message, "error");
      }
    },

    async control(action) {
      const strategy = $("#ctrl-strategy").value || "all";
      let reason = "";
      if (action === "killswitch") {
        reason = prompt("停止 " + strategy + " 新开仓的原因（可留空）", "手动停止");
        if (reason === null) return;
      }
      try {
        await api("/api/control/" + action, {
          method: "POST",
          body: JSON.stringify({ strategy, reason: reason || "" }),
        });
        toast(action === "killswitch" ? "已停止" : "已恢复", "ok");
        this.statusLoop();
      } catch (err) {
        toast(err.message, "error");
      }
    },

    async pause() {
      const strategy = $("#ctrl-strategy").value || "all";
      const minutes = Number(prompt("暂停 " + strategy + " 多少分钟？", "30"));
      if (!minutes || minutes <= 0) return;
      try {
        await api("/api/control/pause", {
          method: "POST",
          body: JSON.stringify({ strategy, minutes, reason: "手动暂停" }),
        });
        toast("已暂停", "ok");
        this.statusLoop();
      } catch (err) {
        toast(err.message, "error");
      }
    },

    async statusLoop() {
      try {
        const wallet = await api("/api/wallet");
        const modes = Object.values(wallet.modes || {});
        const paper = modes.filter((m) => m === "paper").length;
        const live = modes.filter((m) => m === "live").length;
        setText("#wallet-mode", "纸面 " + paper + " · 实盘 " + live);
        const values = Array.from(new Set(Object.values(wallet.balances || {})
          .filter((v) => v !== null && v !== undefined)
          .map((v) => fmt.money(v) + " U")));
        setText("#wallet-balance", values.length ? values.join(" / ") : "--");

        const status = await api("/api/status");
        this.status = status.strategies || [];
        const selected = ($("#ctrl-strategy") || {}).value || "all";
        const el = $("#ctrl-state");
        if (!el) return;
        if (selected === "all") {
          const enabled = this.status.filter((s) => s.app.enabled !== false);
          const stopped = enabled.filter((s) =>
            s.risk && s.risk.killswitch && s.risk.killswitch.active).length;
          const paused = enabled.filter((s) => s.risk && s.risk.paused).length;
          el.className = stopped ? "alert error" : (paused ? "alert warn" : "alert ok");
          el.textContent = "已启用 " + enabled.length + " 个策略；停止 " +
            stopped + " · 暂停 " + paused;
          return;
        }
        const card = this.status.find((s) => this.keyOf(s) === selected);
        if (!card) { el.className = "alert warn"; el.textContent = "策略未运行"; return; }
        if (card.app.enabled === false) {
          el.className = "alert warn";
          el.textContent = selected + " 未启用：不会发现新市场或开仓。";
          return;
        }
        const risk = card.risk || {};
        if (card.app.mode_fallback) {
          el.className = "alert warn";
          el.textContent = selected + " 实盘凭证缺失，已降级为纸面。";
        } else if (risk.killswitch && risk.killswitch.active) {
          el.className = "alert error";
          el.textContent = selected + " 已停止新开仓。" +
            (risk.killswitch.reason ? " 原因：" + risk.killswitch.reason : "");
        } else if (risk.paused) {
          el.className = "alert warn";
          el.textContent = selected + " 暂停中，至 " + fmt.clock(risk.pause_until);
        } else {
          el.className = "alert ok";
          el.textContent = selected + " 运行中：允许在信号出现时开仓。";
        }
      } catch (err) {
        setText("#ctrl-state", "状态查询失败: " + err.message);
      }
    },
  };

  // ── research page ─────────────────────────────────────────────────────
  const researchPage = {
    strategy: "5m",
    poll: null,
    async init() {
      $$("[data-rs-strategy]").forEach((btn) => {
        btn.addEventListener("click", () => {
          this.strategy = btn.getAttribute("data-rs-strategy");
          activate("[data-rs-strategy]", btn);
          this.summary();
          this.prefill();
        });
      });
      await this.summary();
      await this.prefill();
      $("#bt-run").addEventListener("click", () => this.start("backtest"));
      $("#opt-run").addEventListener("click", () => this.start("optimize"));
      $("#agent-run").addEventListener("click", () => this.start("agent"));
      $("#cancel-run").addEventListener("click", () => this.cancel());
      $("#apply-best").addEventListener("click", () => this.apply());
    },
    async summary() {
      const d = await api("/api/research/summary?period=" + this.strategy);
      const h = d.history || {};
      const l = d.live || {};
      setText("#hist-info", h.exists ? (h.date_min + " → " + h.date_max + " · " + h.days + " 天 · 约 " + fmt.int(h.markets_estimate) + " 个市场") : "未找到该周期的历史数据集");
      setText("#live-info", l.exists ? (l.date_min + " → " + l.date_max + " · " + l.days + " 天") : "暂无该周期的实盘采集数据");
      setText("#agent-model", d.model || "--");
      $("#agent-ready").className = d.agent_ready ? "badge green" : "badge amber";
      $("#agent-ready").textContent = d.agent_ready ? "API Key 已配置" : "未配置 ANTHROPIC_API_KEY";
      const runs = d.runs || [];
      $("#run-history").innerHTML = runs.length ? runs.map((r) =>
        "<tr><td>" + r.id + "</td><td><span class='badge gray'>" + fmt.esc(r.period || "") + "</span></td><td>" +
        fmt.esc(r.kind) + "</td><td>" + statusTag(r.status) +
        "</td><td class='mono'>" + fmt.date(r.started_at) +
        "</td><td><button class='btn small' data-run='" + r.id + "'>查看</button></td></tr>"
      ).join("") : "<tr><td colspan='6' class='muted center'>暂无记录</td></tr>";
      $$("[data-run]").forEach((btn) => btn.addEventListener("click", () => this.openRun(btn.getAttribute("data-run"))));
    },
    async prefill() {
      try {
        const cfg = await api("/api/config?strategy=" + this.strategy);
        const v = cfg.values || {};
        const set = (field, value) => {
          const el = document.querySelector("[data-field='" + field + "']");
          if (el && value !== undefined && value !== null) el.value = value;
        };
        set("bt-entry_window_sec", v.ENTRY_WINDOW_SEC);
        set("bt-threshold", v.ENTRY_THRESHOLD);
        set("bt-confirm_sec", v.CONFIRM_SEC);
        set("bt-max_entry_price", v.MAX_ENTRY_PRICE);
        set("bt-min_entry_price", v.MIN_ENTRY_PRICE);
        set("bt-stake", cfg.stake_usd);
        set("bt-slippage", v.EXEC_SLIPPAGE);
        set("bt-fee_rate", v.TAKER_FEE_RATE);
      } catch (e) {
        /* config requires login; the page already does */
      }
    },
    collect() {
      const out = {};
      $$("[data-field^='bt-']").forEach((el) => {
        out[el.getAttribute("data-field").slice(3)] = el.type === "number" ? Number(el.value) : el.value;
      });
      return out;
    },
    async start(kind) {
      const payload = kind === "agent"
        ? {
            period: this.strategy,
            goal: $("#ag-goal").value,
            start_date: $("#ag-start").value || null,
            end_date: $("#ag-end").value || null,
            iterations: Number($("#ag-iterations").value),
          }
        : Object.assign(this.collect(), {
            period: this.strategy,
            dataset: $("#bt-dataset").value,
            start_date: $("#bt-start").value || null,
            end_date: $("#bt-end").value || null,
          });
      if (kind === "optimize") {
        Object.assign(payload, {
          objective: $("#opt-objective").value,
          iterations: Number($("#opt-iterations").value),
          train_end: $("#opt-train-end").value || null,
          validate_start: $("#opt-validate-start").value || null,
          validate_end: $("#opt-validate-end").value || null,
          min_trades: Number($("#opt-min-trades").value),
        });
      }
      try {
        const res = await api("/api/research/" + kind, { method: "POST", body: JSON.stringify(payload) });
        toast("任务已启动 #" + res.run_id, "ok");
        this.watch(res.run_id);
      } catch (err) {
        toast("启动失败: " + err.message, "error");
      }
    },
    async cancel() {
      await api("/api/research/cancel", { method: "POST" });
      toast("已请求取消", "ok");
    },
    watch(runId) {
      const panel = $("#job-panel");
      panel.classList.remove("hidden");
      $("#job-title").textContent = "任务 #" + runId + " · " + this.strategy;
      $("#job-bar").style.width = "0%";
      $("#job-report").innerHTML = "";
      clearInterval(this.poll);
      this.poll = setInterval(() => this.pollJob(runId), 1500);
      this.pollJob(runId);
    },
    async pollJob(runId) {
      const d = await api("/api/research/runs/" + runId);
      const live = d.live || {};
      const job = live.job;
      const run = d.run || {};
      const status = job ? job.status : run.status;
      const progress = job ? job.progress : (run.status === "done" ? 1 : 0);
      const message = job ? job.message : run.status;
      $("#job-bar").style.width = Math.max(0, Math.min(100, progress * 100)) + "%";
      $("#job-status").textContent = (status === "running" ? "运行中 · " : "") + (message || status);
      if (status === "running") return;
      clearInterval(this.poll);
      const report = (job && job.report) || run.report || "";
      const result = (job && job.result) || null;
      $("#job-report").innerHTML = '<div class="md">' + mdToHtml(report) + "</div>";
      let candidate = null;
      if (result) {
        if (result.proposal && result.proposal.parameters) candidate = result.proposal.parameters;
        else if (result.best && result.best.params) candidate = result.best.params;
        else if (result.params) candidate = result.params;
      }
      this.bestParams = candidate && Object.keys(candidate).length ? candidate : null;
      $("#apply-best").classList.toggle("hidden", !this.bestParams);
      if (status === "error") {
        $("#job-report").innerHTML = '<div class="alert error">' + fmt.esc(job ? job.error : run.error) + "</div>" + $("#job-report").innerHTML;
      }
      await this.summary();
    },
    async openRun(runId) {
      const d = await api("/api/research/runs/" + runId);
      const panel = $("#job-panel");
      panel.classList.remove("hidden");
      $("#job-title").textContent = "任务 #" + runId + " · " + (d.run.period || "") + " " + (d.run.kind || "");
      $("#job-status").textContent = d.run.status;
      $("#job-bar").style.width = (d.run.status === "done" ? 100 : 0) + "%";
      $("#job-report").innerHTML = '<div class="md">' + mdToHtml(d.run.report || "无报告") + "</div>";
    },
    async apply() {
      if (!this.bestParams) return;
      const ALIASES = {
        entry_window_sec: "ENTRY_WINDOW_SEC",
        threshold: "ENTRY_THRESHOLD",
        confirm_sec: "CONFIRM_SEC",
        max_entry_price: "MAX_ENTRY_PRICE",
        min_entry_price: "MIN_ENTRY_PRICE",
        stake: "STAKE_USD",
        slippage: "EXEC_SLIPPAGE",
        fee_rate: "TAKER_FEE_RATE",
      };
      let allowed = [];
      try {
        const cfg = await api("/api/config?strategy=" + this.strategy);
        allowed = Object.keys(cfg.specs || {});
      } catch (e) {
        toast("读取参数定义失败: " + e.message, "error");
        return;
      }
      const values = {};
      Object.entries(this.bestParams).forEach(([k, v]) => {
        const upper = k.toUpperCase();
        const key = allowed.indexOf(upper) >= 0 ? upper : (ALIASES[k] || ALIASES[k.toLowerCase()]);
        if (key && allowed.indexOf(key) >= 0 && typeof v === "number" && isFinite(v)) {
          values[key] = v;
        }
      });
      if (!Object.keys(values).length) { toast("没有可应用的参数", "error"); return; }
      if (!confirm("把这组参数写入 " + this.strategy + "？下一个窗口生效。\n" + JSON.stringify(values))) return;
      try {
        await api("/api/research/apply", {
          method: "POST",
          body: JSON.stringify({ strategy: this.strategy, values }),
        });
        toast("已应用到 " + this.strategy, "ok");
        await this.prefill();
      } catch (err) {
        toast("应用失败: " + err.message, "error");
      }
    },
  };

  // ── login ─────────────────────────────────────────────────────────────
  function initLogin() {
    const form = $("#login-form");
    if (!form) return;
    api("/api/auth/me").then((me) => {
      if (!me.password_configured) {
        const warning = $("#login-config-warning");
        if (warning) {
          warning.className = "alert warn";
          warning.textContent = "未配置 WEB_PASSWORD：所有页面都需要登录，因此当前无法进入面板。请在环境变量中设置 WEB_PASSWORD 后重启。";
        }
      }
    }).catch(() => {});
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#login-error");
      err.className = "hidden";
      try {
        await api("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({
            username: $("#username").value,
            password: $("#password").value,
          }),
        });
        // Mirrors the server's _safe_next: same-site relative paths only.
        // An unvalidated value here would turn ?next=javascript:... into XSS
        // that runs with the operator's session.
        const raw = new URLSearchParams(location.search).get("next") || "/";
        const safe = raw.charAt(0) === "/" && raw.charAt(1) !== "/" && raw.charAt(1) !== "\\";
        location.href = safe ? raw : "/";
      } catch (ex) {
        err.className = "alert error";
        err.textContent = ex.message;
      }
    });
  }

  // ── boot ──────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.getAttribute("data-page");
    const setClock = () => { const el = $("#clock"); if (el) el.textContent = fmt.clock(Date.now() / 1000) + " 本地"; };
    setInterval(setClock, 1000);
    setClock();
    if (page === "dashboard") dashboard.init();
    else if (page === "trades") tradesPage.init();
    else if (page === "config") configPage.init();
    else if (page === "research") researchPage.init();
    else if (page === "login") initLogin();
  });
})();

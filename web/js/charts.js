/* ============================================================
   自研 Canvas 图表库（零依赖）
   提供：K线(蜡烛+均线+量能+MACD)、分时、柱状、横向条、仪表盘、迷你走势
   所有图表支持 devicePixelRatio 高清渲染，并注册到重绘表以便窗口尺寸变化时自动重绘。
   所有图表支持鼠标悬停：十字光标 / 高亮 / 数值浮层（tooltip）。
   ============================================================ */
(function (global) {
  'use strict';

  const C = {
    up: '#ff5c5c', down: '#21c46a', grid: '#202a3a', text: '#8b95ab',
    textDim: '#5d6880', accent: '#4f8cff', accent2: '#2fd6c3', violet: '#a97bff',
    bg: 'transparent',
    ma5: '#ffd166', ma10: '#4f8cff', ma20: '#ff8fb1', ma60: '#2fd6c3'
  };

  const registry = [];

  function prepare(canvas, heightPx) {
    if (!canvas) return null;
    const dpr = global.devicePixelRatio || 1;
    const w = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
    const h = heightPx || canvas.clientHeight || 200;
    canvas.width = Math.max(1, Math.floor(w * dpr));
    canvas.height = Math.max(1, Math.floor(h * dpr));
    canvas.style.height = h + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function register(canvas, render) {
    if (!canvas) return;
    const idx = registry.findIndex(r => r.canvas === canvas);
    if (idx >= 0) registry[idx].render = render;
    else registry.push({ canvas: canvas, render: render });
    render();
  }

  function redrawAll() {
    registry.forEach(r => { try { r.render(); } catch (e) { /* ignore */ } });
  }

  let resizeTimer = null;
  global.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawAll, 160);
  });

  /* 页面滚动时浮层会与图表脱节，直接收起 */
  global.addEventListener('scroll', function () { hideTip(); }, true);

  function fmt(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '--';
    return Number(v).toFixed(digits === undefined ? 2 : digits);
  }

  function niceNum(v) {
    const a = Math.abs(v);
    if (a >= 1e8) return fmt(v / 1e8, 2) + '亿';
    if (a >= 1e4) return fmt(v / 1e4, 2) + '万';
    return fmt(v, 2);
  }

  function text(ctx, str, x, y, color, align, size, weight) {
    ctx.font = (weight || '400') + ' ' + (size || 11) + 'px -apple-system, "PingFang SC", sans-serif';
    ctx.fillStyle = color || C.text;
    ctx.textAlign = align || 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(str, x, y);
  }

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  /* ============================================================
     悬浮提示层（DOM 实现，文字清晰、可换行、可配色）
     ============================================================ */
  let tipEl = null;

  function getTipEl() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'chart-tip';
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }

  function showTip(clientX, clientY, html) {
    const el = getTipEl();
    el.innerHTML = html;
    el.classList.add('show');
    const r = el.getBoundingClientRect();
    let left = clientX + 16;
    let top = clientY + 16;
    if (left + r.width > global.innerWidth - 10) left = clientX - r.width - 16;
    if (top + r.height > global.innerHeight - 10) top = clientY - r.height - 16;
    el.style.left = Math.max(8, left) + 'px';
    el.style.top = Math.max(8, top) + 'px';
  }

  function hideTip() {
    if (tipEl) tipEl.classList.remove('show');
  }

  function tipRow(k, v, color) {
    return '<div class="tt-row"><span class="k">' + k + '</span>' +
      '<span' + (color ? ' style="color:' + color + '"' : '') + '>' + v + '</span></div>';
  }

  function tipTitle(str) {
    return '<div class="tt-title">' + str + '</div>';
  }

  /* ---------------- hover 状态与绑定 ---------------- */
  function hstate(canvas) {
    if (!canvas.__hv) canvas.__hv = { active: false, index: -1, mx: 0, my: 0 };
    return canvas.__hv;
  }

  function scheduleRedraw(canvas) {
    if (canvas.__rafPend) return;
    canvas.__rafPend = true;
    global.requestAnimationFrame(function () {
      canvas.__rafPend = false;
      const item = registry.find(r => r.canvas === canvas);
      if (item) { try { item.render(); } catch (e) { /* ignore */ } }
    });
  }

  /** 为画布绑定悬停交互；hitTest(mx,my) 返回 {index, html} 或 null */
  function bindHover(canvas, hitTest) {
    canvas.__hvHit = hitTest;
    if (canvas.__hvBound) return;
    canvas.__hvBound = true;
    canvas.style.cursor = 'crosshair';

    canvas.addEventListener('mousemove', function (ev) {
      const st = hstate(canvas);
      const rect = canvas.getBoundingClientRect();
      st.mx = ev.clientX - rect.left;
      st.my = ev.clientY - rect.top;
      st.active = true;
      let hit = null;
      try { hit = canvas.__hvHit ? canvas.__hvHit(st.mx, st.my) : null; } catch (e) { hit = null; }
      if (hit && hit.html) {
        st.index = hit.index;
        showTip(ev.clientX, ev.clientY, hit.html);
      } else {
        st.index = -1;
        hideTip();
      }
      scheduleRedraw(canvas);
    });

    canvas.addEventListener('mouseleave', function () {
      const st = hstate(canvas);
      st.active = false;
      st.index = -1;
      hideTip();
      scheduleRedraw(canvas);
    });
  }

  /* ---------------- 迷你走势（指数卡） ---------------- */
  function sparkline(canvas, values, color, heightPx, opts) {
    opts = opts || {};
    register(canvas, function () {
      const p = prepare(canvas, heightPx || 46);
      if (!p || !values || values.length < 2) return;
      const { ctx, w, h } = p;
      const min = Math.min.apply(null, values), max = Math.max.apply(null, values);
      const range = (max - min) || 1;
      const stepX = w / (values.length - 1);
      const yOf = v => h - 3 - (v - min) / range * (h - 6);

      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, color + '55');
      grad.addColorStop(1, color + '00');
      ctx.beginPath();
      ctx.moveTo(0, yOf(values[0]));
      values.forEach((v, i) => ctx.lineTo(i * stepX, yOf(v)));
      ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
      ctx.fillStyle = grad; ctx.fill();

      ctx.beginPath();
      ctx.moveTo(0, yOf(values[0]));
      values.forEach((v, i) => ctx.lineTo(i * stepX, yOf(v)));
      ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.stroke();

      /* 区间最高 / 最低参考虚线 */
      ctx.setLineDash([2, 4]);
      ctx.strokeStyle = 'rgba(255,255,255,.07)';
      [max, min].forEach(v => {
        ctx.beginPath(); ctx.moveTo(0, yOf(v)); ctx.lineTo(w, yOf(v)); ctx.stroke();
      });
      ctx.setLineDash([]);

      canvas.__geo = { values: values, stepX: stepX, w: w, h: h, yOf: yOf, dates: opts.dates || [], name: opts.name || '' };

      bindHover(canvas, function (mx) {
        const g = canvas.__geo;
        if (!g) return null;
        let i = Math.round(mx / g.stepX);
        i = clamp(i, 0, g.values.length - 1);
        const v = g.values[i];
        const first = g.values[0];
        const pct = first ? (v - first) / first * 100 : 0;
        const date = g.dates[i] || ('近 ' + (g.values.length - i) + ' 个交易日');
        let html = tipTitle((g.name ? g.name + ' · ' : '') + date);
        html += tipRow('点位', fmt(v, 2), color);
        html += tipRow('较区间起点', (pct >= 0 ? '+' : '') + fmt(pct, 2) + '%', pct >= 0 ? C.up : C.down);
        html += tipRow('区间高/低', fmt(g.values.reduce((a, b) => Math.max(a, b), -Infinity), 2) + ' / ' +
          fmt(g.values.reduce((a, b) => Math.min(a, b), Infinity), 2), C.textDim);
        return { index: i, html: html };
      });

      const hs = hstate(canvas);
      if (hs.active && hs.index >= 0 && hs.index < values.length) {
        const x = hs.index * stepX;
        const y = yOf(values[hs.index]);
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = 'rgba(200,215,245,.45)';
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = color; ctx.fill();
        ctx.strokeStyle = '#0a0d14'; ctx.lineWidth = 1.5; ctx.stroke();
      } else {
        /* 常态：末端光点 */
        const lx = w, ly = yOf(values[values.length - 1]);
        ctx.beginPath(); ctx.arc(lx, ly, 2.2, 0, Math.PI * 2);
        ctx.fillStyle = color; ctx.fill();
      }
    });
  }

  /* ---------------- K 线主图 ---------------- */
  function kline(canvas, data, opts) {
    opts = opts || {};
    const rows = (data && data.rows) || [];
    const series = (data && data.series) || {};
    register(canvas, function () {
      const p = prepare(canvas, opts.height || 320);
      if (!p) return;
      const { ctx, w, h } = p;
      if (!rows.length) { text(ctx, '暂无K线数据', w / 2, h / 2, C.textDim, 'center', 13); return; }

      const padL = 8, padR = 58, padT = 10, padB = 20;
      const innerW = w - padL - padR;
      const priceH = Math.floor((h - padT - padB) * 0.60);
      const volH = Math.floor((h - padT - padB) * 0.18);
      const macdH = (h - padT - padB) - priceH - volH - 12;
      const priceTop = padT, volTop = padT + priceH + 6, macdTop = volTop + volH + 6;

      const highs = rows.map(r => r.high), lows = rows.map(r => r.low);
      const maKeys = ['ma5', 'ma10', 'ma20', 'ma60'].filter(k => series[k]);
      let hi = Math.max.apply(null, highs), lo = Math.min.apply(null, lows);
      maKeys.forEach(k => series[k].forEach(v => { if (v != null) { hi = Math.max(hi, v); lo = Math.min(lo, v); } }));
      const pad = (hi - lo) * 0.06 || 1;
      hi += pad; lo -= pad;
      const yPrice = v => priceTop + (hi - v) / (hi - lo) * priceH;
      const n = rows.length;
      const cw = innerW / n;
      const xOf = i => padL + i * cw + cw / 2;

      /* 格子与价格轴 */
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
      for (let g = 0; g <= 4; g++) {
        const y = priceTop + priceH * g / 4;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + innerW, y); ctx.stroke();
        text(ctx, fmt(hi - (hi - lo) * g / 4, 2), w - padR + 6, y, C.textDim, 'left', 10);
      }

      /* 蜡烛 */
      rows.forEach((r, i) => {
        const rise = r.close >= r.open;
        const col = rise ? C.up : C.down;
        ctx.strokeStyle = col; ctx.fillStyle = col;
        const x = xOf(i);
        ctx.beginPath();
        ctx.moveTo(x, yPrice(r.high)); ctx.lineTo(x, yPrice(r.low));
        ctx.lineWidth = 1; ctx.stroke();
        const bw = Math.max(1, cw * 0.66);
        const yo = yPrice(r.open), yc = yPrice(r.close);
        const top = Math.min(yo, yc), bh = Math.max(1, Math.abs(yc - yo));
        ctx.fillRect(x - bw / 2, top, bw, bh);
      });

      /* 均线 */
      const maColors = { ma5: C.ma5, ma10: C.ma10, ma20: C.ma20, ma60: C.ma60 };
      maKeys.forEach(k => {
        const arr = series[k];
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < Math.min(arr.length, n); i++) {
          const v = arr[i];
          if (v == null) { started = false; continue; }
          const x = xOf(i), y = yPrice(v);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = maColors[k] || C.accent; ctx.lineWidth = 1.15; ctx.stroke();
      });

      /* 成交量 */
      const vols = rows.map(r => r.volume);
      const vmax = Math.max.apply(null, vols) || 1;
      const vy = v => volTop + volH - v / vmax * volH;
      text(ctx, 'VOL', padL + 2, volTop + 6, C.textDim, 'left', 10);
      rows.forEach((r, i) => {
        const bw = Math.max(1, cw * 0.66);
        ctx.fillStyle = (r.close >= r.open ? C.up : C.down) + 'cc';
        const y = vy(r.volume);
        ctx.fillRect(xOf(i) - bw / 2, y, bw, volTop + volH - y);
      });

      /* MACD 副图 */
      const hist = series.hist || [], dif = series.dif || [], dea = series.dea || [];
      let macdSpan = 1;
      const my = v => macdTop + macdH / 2 - (v / macdSpan) * (macdH / 2 - 2);
      if (hist.length) {
        const seg = hist.slice(-n);
        macdSpan = Math.max.apply(null, seg.map(v => Math.abs(v || 0)).concat([1e-6]));
        ctx.strokeStyle = C.grid;
        ctx.beginPath(); ctx.moveTo(padL, macdTop + macdH / 2); ctx.lineTo(padL + innerW, macdTop + macdH / 2); ctx.stroke();
        text(ctx, 'MACD', padL + 2, macdTop + 6, C.textDim, 'left', 10);
        for (let i = 0; i < n; i++) {
          const v = seg[seg.length - n + i];
          if (v == null) continue;
          const bw = Math.max(1, cw * 0.5);
          const y0 = my(0), y1 = my(v);
          ctx.fillStyle = v >= 0 ? C.up : C.down;
          ctx.fillRect(xOf(i) - bw / 2, Math.min(y0, y1), bw, Math.max(1, Math.abs(y1 - y0)));
        }
        [[dif, C.ma5], [dea, C.accent]].forEach(pair => {
          const arr = pair[0]; if (!arr || !arr.length) return;
          ctx.beginPath(); let st = false;
          for (let i = 0; i < n; i++) {
            const v = arr[arr.length - n + i];
            if (v == null) { st = false; continue; }
            const x = xOf(i), y = my(v);
            if (!st) { ctx.moveTo(x, y); st = true; } else ctx.lineTo(x, y);
          }
          ctx.strokeStyle = pair[1]; ctx.lineWidth = 1.1; ctx.stroke();
        });
      }

      /* 日期轴 */
      const ticks = Math.min(6, n);
      for (let t = 0; t < ticks; t++) {
        const i = Math.floor((n - 1) * t / Math.max(1, ticks - 1));
        const d = rows[i].date || '';
        text(ctx, d.slice(2), xOf(i), h - padB / 2 - 2, C.textDim, 'center', 10);
      }

      /* ---------------- 悬停：十字光标 + 数值 ---------------- */
      const hs = hstate(canvas);

      canvas.__geo = {
        padL: padL, padR: padR, innerW: innerW, cw: cw, n: n,
        priceTop: priceTop, priceH: priceH, hi: hi, lo: lo,
        yPrice: yPrice, xOf: xOf, rows: rows, series: series, maColors: maColors
      };

      bindHover(canvas, function (mx) {
        const g = canvas.__geo;
        if (!g) return null;
        if (mx < g.padL - 2 || mx > g.padL + g.innerW + 2) return null;
        const i = clamp(Math.floor((mx - g.padL) / g.cw), 0, g.n - 1);
        const r = g.rows[i];
        const prevClose = i > 0 ? g.rows[i - 1].close : r.open;
        const pct = prevClose ? (r.close - prevClose) / prevClose * 100 : 0;
        const col = pct >= 0 ? C.up : C.down;
        const at = function (k) {
          const arr = g.series[k];
          if (!arr) return null;
          const v = arr[arr.length - g.n + i];
          return v == null ? null : v;
        };
        let html = tipTitle(r.date || '');
        html += tipRow('开盘', fmt(r.open, 2));
        html += tipRow('最高', fmt(r.high, 2), C.up);
        html += tipRow('最低', fmt(r.low, 2), C.down);
        html += tipRow('收盘', fmt(r.close, 2), col);
        html += tipRow('涨跌幅', (pct >= 0 ? '+' : '') + fmt(pct, 2) + '%', col);
        html += tipRow('成交量', niceNum(r.volume) + ' 手');
        if (r.amount) html += tipRow('成交额', niceNum(r.amount));
        [['ma5', C.ma5], ['ma10', C.ma10], ['ma20', C.ma20], ['ma60', C.ma60]].forEach(pair => {
          const v = at(pair[0]);
          if (v != null) html += tipRow(pair[0].toUpperCase(), fmt(v, 2), pair[1]);
        });
        const hv = at('hist');
        if (hv != null) html += tipRow('MACD 柱', fmt(hv, 3), hv >= 0 ? C.up : C.down);
        return { index: i, html: html };
      });

      if (hs.active && hs.index >= 0 && hs.index < n) {
        const i = hs.index, x = xOf(i);
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = 'rgba(190,205,235,.5)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, priceTop); ctx.lineTo(x, macdTop + macdH); ctx.stroke();

        if (hs.my >= priceTop && hs.my <= priceTop + priceH) {
          ctx.beginPath(); ctx.moveTo(padL, hs.my); ctx.lineTo(padL + innerW, hs.my); ctx.stroke();
          ctx.setLineDash([]);
          const pv = hi - (hs.my - priceTop) / priceH * (hi - lo);
          const lbl = fmt(pv, 2);
          ctx.font = '600 10px -apple-system, "PingFang SC", sans-serif';
          const tw = Math.min(padR - 6, ctx.measureText(lbl).width + 10);
          ctx.fillStyle = '#2c3548';
          ctx.fillRect(w - padR + 2, hs.my - 8, tw, 16);
          text(ctx, lbl, w - padR + 5, hs.my, '#e7ebf3', 'left', 10, '600');
        }
        ctx.setLineDash([]);

        /* 收盘价光点 */
        const cy = yPrice(rows[i].close);
        const rise = rows[i].close >= rows[i].open;
        ctx.beginPath(); ctx.arc(x, cy, 3, 0, Math.PI * 2);
        ctx.fillStyle = rise ? C.up : C.down; ctx.fill();
        ctx.strokeStyle = '#0a0d14'; ctx.lineWidth = 1.5; ctx.stroke();

        /* 底部日期气泡 */
        const d = rows[i].date || '';
        ctx.font = '600 10px -apple-system, "PingFang SC", sans-serif';
        const dw = ctx.measureText(d).width + 12;
        const dx = clamp(x - dw / 2, padL, padL + innerW - dw);
        ctx.fillStyle = '#2c3548';
        ctx.fillRect(dx, h - padB + 2, dw, 15);
        text(ctx, d, dx + dw / 2, h - padB + 9.5, '#dfe6f5', 'center', 10, '600');
      }
    });
  }

  /* ---------------- 分时图 ---------------- */
  function minute(canvas, data, prevClose, opts) {
    opts = opts || {};
    const pts = (data && data.points) || [];
    register(canvas, function () {
      const p = prepare(canvas, opts.height || 320);
      if (!p) return;
      const { ctx, w, h } = p;
      if (!pts.length) { text(ctx, '暂无分时数据（或非交易日）', w / 2, h / 2, C.textDim, 'center', 13); return; }

      const padL = 8, padR = 58, padT = 12, padB = 20;
      const innerW = w - padL - padR;
      const innerH = h - padT - padB - 34;
      const base = prevClose || pts[0].price;
      const dev = Math.max.apply(null, pts.map(q => Math.abs(q.price - base))) || base * 0.01;
      const hi = base + dev, lo = base - dev;
      const yOf = v => padT + (hi - v) / (hi - lo) * innerH;
      const xOf = i => padL + i / Math.max(1, pts.length - 1) * innerW;

      ctx.strokeStyle = C.grid;
      for (let g = 0; g <= 4; g++) {
        const y = padT + innerH * g / 4;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + innerW, y); ctx.stroke();
        const val = hi - (hi - lo) * g / 4;
        const pctVal = (val - base) / base * 100;
        text(ctx, fmt(val, 2) + '  ' + (pctVal >= 0 ? '+' : '') + fmt(pctVal, 2) + '%',
          w - padR + 6, y, Math.abs(pctVal) < 0.001 ? C.textDim : (pctVal > 0 ? C.up : C.down), 'left', 10);
      }

      /* 均价线 */
      const avgArr = [];
      let accV = 0, accA = 0;
      pts.forEach(q => { accV += q.volume || 0; accA += q.amount || 0; avgArr.push(accV > 0 ? accA / accV : q.price); });

      const close = pts[pts.length - 1].price;
      const col = close >= base ? C.up : C.down;
      const grad = ctx.createLinearGradient(0, padT, 0, padT + innerH);
      grad.addColorStop(0, col + '3a'); grad.addColorStop(1, col + '00');

      ctx.beginPath();
      ctx.moveTo(xOf(0), yOf(pts[0].price));
      pts.forEach((q, i) => ctx.lineTo(xOf(i), yOf(q.price)));
      ctx.lineTo(xOf(pts.length - 1), padT + innerH); ctx.lineTo(xOf(0), padT + innerH); ctx.closePath();
      ctx.fillStyle = grad; ctx.fill();

      ctx.beginPath();
      ctx.moveTo(xOf(0), yOf(pts[0].price));
      pts.forEach((q, i) => ctx.lineTo(xOf(i), yOf(q.price)));
      ctx.strokeStyle = col; ctx.lineWidth = 1.3; ctx.stroke();

      ctx.beginPath();
      avgArr.forEach((v, i) => i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v)));
      ctx.strokeStyle = C.ma5; ctx.lineWidth = 1; ctx.stroke();

      /* 均价基准线 */
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = C.textDim;
      ctx.beginPath(); ctx.moveTo(padL, yOf(base)); ctx.lineTo(padL + innerW, yOf(base)); ctx.stroke();
      ctx.setLineDash([]);

      /* 分时量能 */
      const volTop = padT + innerH + 8, volH = 26;
      const vmax = Math.max.apply(null, pts.map(q => q.volume || 0)) || 1;
      pts.forEach((q, i) => {
        const bh = (q.volume || 0) / vmax * volH;
        const prev = i > 0 ? pts[i - 1].price : base;
        ctx.fillStyle = (q.price >= prev ? C.up : C.down) + 'b0';
        ctx.fillRect(xOf(i) - 1, volTop + volH - bh, Math.max(1, innerW / pts.length * 0.7), bh);
      });

      ['09:30', '10:30', '11:30/13:00', '14:00', '15:00'].forEach((lb, i, arr) => {
        text(ctx, lb, padL + innerW * i / (arr.length - 1), h - padB / 2, C.textDim, 'center', 10);
      });

      /* ---------------- 悬停：十字光标 + 数值 ---------------- */
      const hs = hstate(canvas);
      canvas.__geo = {
        padL: padL, innerW: innerW, padT: padT, innerH: innerH,
        n: pts.length, pts: pts, base: base, hi: hi, lo: lo,
        yOf: yOf, xOf: xOf, avgArr: avgArr, volTop: volTop, volH: volH, vmax: vmax
      };

      bindHover(canvas, function (mx) {
        const g = canvas.__geo;
        if (!g) return null;
        if (mx < g.padL - 2 || mx > g.padL + g.innerW + 2) return null;
        const i = clamp(Math.round((mx - g.padL) / Math.max(1, g.innerW) * (g.n - 1)), 0, g.n - 1);
        const q = g.pts[i];
        const pct = g.base ? (q.price - g.base) / g.base * 100 : 0;
        const ccol = pct >= 0 ? C.up : C.down;
        let html = tipTitle(q.t || '');
        html += tipRow('价格', fmt(q.price, 2), ccol);
        html += tipRow('涨跌幅', (pct >= 0 ? '+' : '') + fmt(pct, 2) + '%', ccol);
        html += tipRow('均价', fmt(g.avgArr[i], 2), C.ma5);
        html += tipRow('成交量', niceNum(q.volume) + ' 手');
        if (q.amount) html += tipRow('成交额', niceNum(q.amount));
        html += tipRow('昨收', fmt(g.base, 2), C.textDim);
        return { index: i, html: html };
      });

      if (hs.active && hs.index >= 0 && hs.index < pts.length) {
        const i = hs.index;
        const x = xOf(i), y = yOf(pts[i].price);
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = 'rgba(190,205,235,.5)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, volTop + volH); ctx.stroke();
        if (hs.my >= padT && hs.my <= padT + innerH) {
          ctx.beginPath(); ctx.moveTo(padL, hs.my); ctx.lineTo(padL + innerW, hs.my); ctx.stroke();
        }
        ctx.setLineDash([]);

        const pv = hi - (hs.my - padT) / innerH * (hi - lo);
        const pctv = (pv - base) / base * 100;
        const lbl = fmt(pv, 2);
        ctx.font = '600 10px -apple-system, "PingFang SC", sans-serif';
        const tw = Math.min(padR - 6, ctx.measureText(lbl).width + 10);
        ctx.fillStyle = '#2c3548';
        ctx.fillRect(w - padR + 2, clamp(hs.my, padT + 8, padT + innerH - 8) - 8, tw, 16);
        text(ctx, lbl, w - padR + 5, clamp(hs.my, padT + 8, padT + innerH - 8), '#e7ebf3', 'left', 10, '600');

        ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = pctv >= 0 ? C.up : C.down; ctx.fill();
        ctx.strokeStyle = '#0a0d14'; ctx.lineWidth = 1.5; ctx.stroke();

        const tt = pts[i].t || '';
        ctx.font = '600 10px -apple-system, "PingFang SC", sans-serif';
        const dw = ctx.measureText(tt).width + 12;
        const dx = clamp(x - dw / 2, padL, padL + innerW - dw);
        ctx.fillStyle = '#2c3548';
        ctx.fillRect(dx, h - padB + 2, dw, 15);
        text(ctx, tt, dx + dw / 2, h - padB + 9.5, '#dfe6f5', 'center', 10, '600');
      }
    });
  }

  /* ---------------- 柱状图（支持正负双向） ---------------- */
  function bars(canvas, items, opts) {
    opts = opts || {};
    register(canvas, function () {
      const p = prepare(canvas, opts.height || 132);
      if (!p) return;
      const { ctx, w, h } = p;
      if (!items || !items.length) { text(ctx, '暂无数据', w / 2, h / 2, C.textDim, 'center', 12); return; }
      const padL = 26, padR = 8, padT = 14, padB = 20;
      const innerW = w - padL - padR, innerH = h - padT - padB;
      const signed = !!opts.signed;
      const values = items.map(d => signed ? (d.raw !== undefined ? d.raw : d.value) : d.value);
      const maxPos = Math.max.apply(null, values.concat([0]));
      const maxNeg = Math.min.apply(null, values.concat([0]));
      const span = (maxPos - maxNeg) || Math.max(Math.abs(maxPos), 1);
      const zeroY = signed ? padT + innerH * (maxPos / span) : padT + innerH;
      const scale = innerH / span;
      const bw = innerW / items.length;
      const labelStep = items.length > 16 ? Math.ceil(items.length / 10) : 1;

      ctx.strokeStyle = C.grid;
      ctx.beginPath(); ctx.moveTo(padL, zeroY); ctx.lineTo(padL + innerW, zeroY); ctx.stroke();
      if (signed) {
        text(ctx, '0', padL - 6, zeroY, C.textDim, 'right', 10);
      }

      const hs = hstate(canvas);
      const hovIdx = hs.active ? hs.index : -1;

      items.forEach((d, i) => {
        const v = values[i];
        const bh = Math.max(1, Math.abs(v) * scale);
        const x = padL + i * bw + bw * 0.16;
        const bwid = bw * 0.68;
        const col = d.color || (v >= 0 ? C.up : C.down);
        const y = v >= 0 ? zeroY - bh : zeroY;
        const g = ctx.createLinearGradient(0, y, 0, y + bh);
        g.addColorStop(0, col); g.addColorStop(1, col + '55');
        ctx.fillStyle = g;
        ctx.fillRect(x, y, bwid, bh);

        /* 悬停高亮 */
        if (i === hovIdx) {
          ctx.fillStyle = 'rgba(255,255,255,.10)';
          ctx.fillRect(padL + i * bw + 1, padT, bw - 2, innerH);
          ctx.strokeStyle = col; ctx.lineWidth = 1;
          ctx.strokeRect(x, y, bwid, Math.max(1, bh));
        }

        if (items.length <= 16 || i % labelStep === 0) {
          text(ctx, d.label, padL + i * bw + bw / 2, h - padB / 2,
            i === hovIdx ? '#e7ebf3' : C.textDim, 'center', 10);
        }
      });

      if (opts.markLatest) {
        const last = items[items.length - 1];
        const v = values[values.length - 1];
        const bh = Math.max(1, Math.abs(v) * scale);
        const y = v >= 0 ? zeroY - bh - 8 : zeroY + bh + 8;
        text(ctx, last.text || '', padL + (items.length - 1) * bw + bw / 2, y, last.color || C.text, 'right', 10, '600');
      }

      canvas.__geo = { padL: padL, bw: bw, items: items, values: values };
      bindHover(canvas, function (mx) {
        const g = canvas.__geo;
        if (!g) return null;
        const i = Math.floor((mx - g.padL) / g.bw);
        if (i < 0 || i >= g.items.length) return null;
        const d = g.items[i];
        let html = tipTitle(d.label);
        html += tipRow('数值', (d.text || fmt(g.values[i], 0)), d.color || C.text);
        if (d.hint) html += tipRow('说明', d.hint, C.textDim);
        return { index: i, html: html };
      });
    });
  }

  /* ---------------- 横向条（涨跌结构 / 板块资金） ---------------- */
  function hbars(canvas, items, opts) {
    opts = opts || {};
    register(canvas, function () {
      const p = prepare(canvas, opts.height || 132);
      if (!p) return;
      const { ctx, w, h } = p;
      if (!items || !items.length) { text(ctx, '暂无数据', w / 2, h / 2, C.textDim, 'center', 12); return; }
      const padT = 8, padB = 8, gap = 8;
      const rowH = Math.min(26, (h - padT - padB - gap * (items.length - 1)) / items.length);
      const max = Math.max.apply(null, items.map(d => Math.abs(d.value))) || 1;
      const total = items.reduce((a, b) => a + Math.abs(b.value || 0), 0) || 1;

      const hs = hstate(canvas);
      const hovIdx = hs.active ? hs.index : -1;

      let y = padT;
      items.forEach((d, i) => {
        if (i === hovIdx) {
          ctx.fillStyle = 'rgba(255,255,255,.055)';
          ctx.fillRect(0, y - 2, w, rowH + 4);
        }
        text(ctx, d.label, 0, y + rowH / 2, i === hovIdx ? '#e7ebf3' : C.text, 'left', 11.5,
          i === hovIdx ? '600' : '400');
        const barX = 86, barW = w - barX - 76;
        ctx.fillStyle = '#1b2331';
        ctx.fillRect(barX, y + rowH / 2 - 4, barW, 8);
        const bw = Math.abs(d.value) / max * barW;
        ctx.fillStyle = d.color || C.accent;
        ctx.fillRect(barX, y + rowH / 2 - 4, Math.max(1, bw), 8);
        text(ctx, d.text || fmt(d.value, 0), w, y + rowH / 2, d.color || C.text, 'right', 11.5, '600');
        y += rowH + gap;
      });

      canvas.__geo = { padT: padT, rowH: rowH, gap: gap, items: items, total: total };
      bindHover(canvas, function (mx, my) {
        const g = canvas.__geo;
        if (!g) return null;
        const idx = Math.floor((my - g.padT) / (g.rowH + g.gap));
        if (idx < 0 || idx >= g.items.length) return null;
        const d = g.items[idx];
        const share = g.total ? Math.abs(d.value) / g.total * 100 : 0;
        let html = tipTitle(d.label);
        html += tipRow('数量', (d.text || fmt(d.value, 0)), d.color || C.text);
        html += tipRow('占比', fmt(share, 1) + '%', C.textDim);
        return { index: idx, html: html };
      });
    });
  }

  /* ---------------- 仪表盘 ---------------- */
  function gauge(canvas, value, opts) {
    opts = opts || {};
    register(canvas, function () {
      const p = prepare(canvas, opts.height || 120);
      if (!p) return;
      const { ctx, w, h } = p;
      const cx = w / 2, cy = h * 0.78, r = Math.min(w * 0.38, h * 0.72);
      const start = Math.PI * 0.92, end = Math.PI * 2.08;
      const v = Math.max(0, Math.min(100, value || 0));
      const col = v >= 70 ? C.up : v >= 55 ? '#ff9f43' : v >= 45 ? C.accent : v >= 30 ? C.accent2 : C.down;

      ctx.lineWidth = 10; ctx.lineCap = 'round';
      ctx.strokeStyle = '#1b2331';
      ctx.beginPath(); ctx.arc(cx, cy, r, start, end); ctx.stroke();
      ctx.strokeStyle = col;
      ctx.beginPath(); ctx.arc(cx, cy, r, start, start + (end - start) * v / 100); ctx.stroke();

      text(ctx, String(Math.round(v)), cx, cy - r * 0.28, col, 'center', 30, '700');
      if (opts.suffix) text(ctx, opts.suffix, cx, cy - r * 0.28 + 26, C.textDim, 'center', 11);
      text(ctx, '0', cx - r * 0.92, cy + 12, C.textDim, 'center', 10);
      text(ctx, '100', cx + r * 0.92, cy + 12, C.textDim, 'center', 10);

      canvas.__geo = { value: v, level: opts.level || '', hint: opts.hint || '', cx: cx, cy: cy, r: r };
      bindHover(canvas, function (mx, my) {
        const g = canvas.__geo;
        if (!g) return null;
        /* 只在圆环附近响应，避免整块画布都弹层 */
        const dist = Math.sqrt((mx - g.cx) * (mx - g.cx) + (my - g.cy) * (my - g.cy));
        if (dist < g.r - 22) return null;
        let html = tipTitle(opts.suffix || '数值');
        html += tipRow('当前', fmt(g.value, 0) + ' / 100', col);
        if (g.level) html += tipRow('所处区间', g.level, col);
        if (g.hint) html += tipRow('含义', g.hint, C.textDim);
        return { index: 0, html: html };
      });
    });
  }

  /* ---------------- 分组柱状（相对大盘） ---------------- */
  function groupBars(canvas, groups, opts) {
    opts = opts || {};
    register(canvas, function () {
      const p = prepare(canvas, opts.height || 132);
      if (!p) return;
      const { ctx, w, h } = p;
      if (!groups || !groups.length) return;
      const padL = 34, padR = 10, padT = 14, padB = 22;
      const innerW = w - padL - padR, innerH = h - padT - padB;
      const all = [];
      groups.forEach(g => g.values.forEach(v => all.push(v)));
      const maxAbs = Math.max.apply(null, all.map(Math.abs).concat([1]));
      const mid = padT + innerH / 2;
      ctx.strokeStyle = C.grid;
      ctx.beginPath(); ctx.moveTo(padL, mid); ctx.lineTo(padL + innerW, mid); ctx.stroke();
      text(ctx, '0', padL - 6, mid, C.textDim, 'right', 10);

      const hs = hstate(canvas);
      const hovIdx = hs.active ? hs.index : -1;
      const gw = innerW / groups.length;

      groups.forEach((g, gi) => {
        if (gi === hovIdx) {
          ctx.fillStyle = 'rgba(255,255,255,.055)';
          ctx.fillRect(padL + gi * gw + 2, padT, gw - 4, innerH);
        }
        const bw = gw * 0.72 / g.values.length;
        g.values.forEach((v, vi) => {
          const bh = Math.abs(v) / maxAbs * (innerH / 2 - 6);
          const x = padL + gi * gw + gw * 0.14 + vi * bw;
          const y = v >= 0 ? mid - bh : mid;
          ctx.fillStyle = g.colors[vi];
          ctx.fillRect(x, y, bw * 0.82, Math.max(1, bh));
        });
        text(ctx, g.label, padL + gi * gw + gw / 2, h - padB / 2,
          gi === hovIdx ? '#e7ebf3' : C.textDim, 'center', 10.5);
      });

      canvas.__geo = { padL: padL, gw: gw, groups: groups };
      bindHover(canvas, function (mx) {
        const g = canvas.__geo;
        if (!g) return null;
        const gi = Math.floor((mx - g.padL) / g.gw);
        if (gi < 0 || gi >= g.groups.length) return null;
        const grp = g.groups[gi];
        let html = tipTitle(grp.label + (grp.subtitle ? ' · ' + grp.subtitle : ''));
        const names = grp.names || ['个股', '上证指数'];
        grp.values.forEach((v, k) => {
          html += tipRow(names[k] || ('系列' + (k + 1)),
            (v >= 0 ? '+' : '') + fmt(v, 2) + '%', v >= 0 ? C.up : C.down);
        });
        if (grp.values.length >= 2) {
          const diff = grp.values[0] - grp.values[1];
          html += tipRow('超额收益', (diff >= 0 ? '+' : '') + fmt(diff, 2) + 'pct', diff >= 0 ? C.up : C.down);
        }
        return { index: gi, html: html };
      });
    });
  }

  global.Charts = {
    sparkline: sparkline,
    kline: kline,
    minute: minute,
    bars: bars,
    hbars: hbars,
    gauge: gauge,
    groupBars: groupBars,
    redrawAll: redrawAll,
    hideTip: hideTip,
    niceNum: niceNum,
    fmt: fmt,
    colors: C
  };
})(window);

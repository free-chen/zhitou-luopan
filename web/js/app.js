/* ============================================================
   智投罗盘 · 前端主逻辑
   四大视图：市场总览 / 个股分析 / 智能选股 / 自选股
   ============================================================ */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const WATCH_KEY = 'zhitou.watchlist.v1';

  const state = {
    view: 'market',
    market: null,
    marketSectorKind: 'industry',
    stock: null,
    stockCode: '',
    period: 'day',
    strategies: null,
    strategy: 'trend_bull',
    custom: {},
    jobId: null,
    pollTimer: null,
    result: null,
    sortKey: 'amount',
    sortAsc: false,
    sortTouched: false,
    watch: loadWatch()
  };

  /* ---------------- 工具 ---------------- */
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function num(v, d) {
    if (v === null || v === undefined || v === '' || isNaN(v)) return '--';
    return Number(v).toFixed(d === undefined ? 2 : d);
  }
  function fmtAmt(v) {
    const n = Number(v) || 0;
    if (Math.abs(n) >= 1e8) return num(n / 1e8, 2) + '亿';
    if (Math.abs(n) >= 1e4) return num(n / 1e4, 2) + '万';
    return num(n, 2);
  }
  function fmtPct(v, d) {
    if (v === null || v === undefined || isNaN(v)) return '--';
    const n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(d === undefined ? 2 : d) + '%';
  }
  function signCls(v) {
    const n = Number(v) || 0;
    return n > 0.0001 ? 'up' : n < -0.0001 ? 'down' : 'flat';
  }
  function clampPct(v) {
    const n = Number(v);
    if (isNaN(n)) return 50;
    return Math.max(0, Math.min(100, n));
  }
  function toast(msg, ms) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove('show'), ms || 2600);
  }
  function loadWatch() {
    try { return JSON.parse(localStorage.getItem(WATCH_KEY) || '[]'); } catch (e) { return []; }
  }
  function saveWatch() {
    try { localStorage.setItem(WATCH_KEY, JSON.stringify(state.watch)); } catch (e) { /* ignore */ }
  }
  function inWatch(full) { return state.watch.some(w => w.full === full); }
  function toggleWatch(item) {
    if (inWatch(item.full)) {
      state.watch = state.watch.filter(w => w.full !== item.full);
      toast('已移出自选');
    } else {
      state.watch.push({ full: item.full, name: item.name, code: item.code });
      toast('已加入自选');
    }
    saveWatch();
  }
  function normalizeInput(v) {
    const s = String(v || '').trim().toLowerCase();
    if (/^(sh|sz|bj)?\d{6}$/.test(s)) return s;
    return null;
  }

  /* ---------------- 路由 ---------------- */
  function switchView(view) {
    state.view = view;
    Charts.hideTip();
    document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === view));
    document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
    const node = $('view-' + view);
    if (node) node.classList.remove('hidden');
    if (view === 'market' && !state.market) loadMarket();
    if (view === 'screener' && !state.strategies) loadStrategies();
    if (view === 'watchlist') renderWatchlist();
    requestAnimationFrame(() => Charts.redrawAll());
  }

  /* ============================================================
     市场总览
     ============================================================ */
  async function loadMarket(force) {
    if (state.market && !force) { renderMarket(); return; }
    $('marketUpdated').textContent = '加载中…';
    try {
      const data = await API.market();
      state.market = data;
      renderMarket();
    } catch (e) {
      $('marketUpdated').textContent = '';
      toast('市场数据加载失败：' + e.message);
    }
  }

  function renderMarket() {
    const m = state.market;
    if (!m) return;
    $('marketUpdated').textContent = '更新于 ' + (m.updated_at || '');
    renderIndexCards(m.indices || []);
    renderSentiment(m.sentiment || {});
    renderBreadth(m.breadth || {});
    renderEffect(m);
    renderLadder(m.ladder || {});
    renderSectorFlow();
    renderSectorRank(m.sector_rank || []);
    renderLimitUp(m.limit_up_pool || []);
  }

  function renderIndexCards(list) {
    $('indexCards').innerHTML = list.map((it, i) => {
      const cls = signCls(it.change_pct);
      const hasRange = it.high > 0 && it.low > 0 && it.high > it.low;
      const pos = hasRange ? clampPct((it.price - it.low) / (it.high - it.low) * 100) : 50;
      return '' +
        '<div class="index-card">' +
        '<div class="ic-top"><span class="ic-name">' + esc(it.name) + '</span>' +
        '<span class="ic-chg ' + cls + '">' + fmtPct(it.change_pct) + '</span></div>' +
        '<div class="ic-price ' + cls + '">' + num(it.price, 2) + '</div>' +
        (hasRange
          ? '<div class="ic-range" title="当前价位于今日高低区间 ' + num(pos, 0) + '% 位置">' +
            '<i style="left:' + num(pos, 1) + '%"></i></div>' +
          '<div class="ic-sub"><span>高 ' + num(it.high, 2) + '</span><span>低 ' + num(it.low, 2) + '</span></div>'
          : '') +
        '<canvas class="spark" id="idxSpark' + i + '"></canvas>' +
        '<div class="ic-amt">成交 ' + fmtAmt(it.amount) + '</div>' +
        '</div>';
    }).join('');
    list.forEach((it, i) => {
      const cls = signCls(it.change_pct);
      const color = cls === 'up' ? Charts.colors.up : cls === 'down' ? Charts.colors.down : '#8b95ab';
      Charts.sparkline($('idxSpark' + i), it.trend || [], color, 42, {
        dates: it.trend_dates || [],
        name: it.name
      });
    });
  }

  function renderSentiment(s) {
    Charts.gauge($('sentimentGauge'), s.temperature || 0, {
      suffix: '情绪温度', height: 118,
      level: s.level || '', hint: '越高代表短线做多意愿越强'
    });
    const toneCls = s.tone === 'bull' ? 'up' : s.tone === 'bear' ? 'down' : 'flat';
    $('sentimentMeta').innerHTML =
      '<div class="lv ' + toneCls + '">' + esc(s.level || '--') + '</div>' +
      '<div class="muted small">温度越高代表短线做多意愿越强</div>';
  }

  function renderBreadth(b) {
    const up = b.up || 0, down = b.down || 0, flat = b.flat || 0;
    const total = up + down + flat;
    Charts.hbars($('breadthBar'), [
      { label: '上涨', value: up, color: Charts.colors.up, text: up + ' 家' },
      { label: '下跌', value: down, color: Charts.colors.down, text: down + ' 家' },
      { label: '平盘', value: flat, color: '#6b7690', text: flat + ' 家' },
      { label: '涨停', value: b.limit_up || 0, color: '#ff8b3d', text: (b.limit_up || 0) + ' 家' },
      { label: '跌停', value: b.limit_down || 0, color: '#7a5cff', text: (b.limit_down || 0) + ' 家' }
    ], { height: 140 });
    $('breadthStats').innerHTML =
      stat('上涨占比', num(b.up_ratio, 1) + '%', up > down ? 'up' : 'down') +
      stat('全市场成交', fmtAmt(b.amount), '') +
      stat('涨超5%', (b.up5 || 0) + ' 家', 'up') +
      stat('跌超5%', (b.down5 || 0) + ' 家', 'down') +
      stat('样本总数', total + ' 只', '');
  }

  function renderEffect(m) {
    const l = m.ladder || {};
    $('effectStats').innerHTML =
      stat('最高连板', (l.max_board || 0) + ' 板', (l.max_board || 0) >= 4 ? 'up' : '', '梯队中最高的连续涨停天数') +
      stat('涨停家数', (m.breadth && m.breadth.limit_up) || 0, 'up', '当日收盘封板的家数') +
      stat('跌停家数', (m.breadth && m.breadth.limit_down) || 0, 'down', '当日收盘跌停的家数') +
      stat('炸板家数', m.broken_count || 0, '', '盘中触及涨停但收盘未封住') +
      stat('封板率', num(m.seal_rate, 1) + '%', (m.seal_rate || 0) >= 70 ? 'up' : '', '封板数 /（封板数 + 炸板数）') +
      stat('炸板率', num(100 - (m.seal_rate || 0), 1) + '%', (m.seal_rate || 0) < 70 ? 'down' : '', '炸板数 /（封板数 + 炸板数）');
  }

  function renderLadder(l) {
    const items = (l.ladder || []).map(x => ({
      label: x.board + '板',
      value: x.count,
      color: x.board >= 3 ? Charts.colors.up : x.board === 2 ? '#ff9f43' : Charts.colors.accent,
      hint: x.board >= 3 ? '高位连板，短线情绪核心' : (x.board === 2 ? '二板梯队，接力中坚' : '首板，情绪起点')
    }));
    Charts.bars($('ladderChart'), items, { height: 132 });
    $('ladderLegend').innerHTML =
      '<span>最高连板：<b class="up">' + (l.max_board || 0) + ' 板</b></span>' +
      '<span>梯队进级股数：<b>' + (l.total || 0) + '</b></span>' +
      '<span class="muted">连板高度越高，短线情绪越活跃</span>';
  }

  function renderSectorFlow() {
    const m = state.market; if (!m) return;
    const kind = state.marketSectorKind;
    const group = (m.sector_fund || {})[kind] || {};
    const inflow = group.inflow || [], outflow = group.outflow || [];
    const col = (title, items, dir) => {
      if (!items.length) return '<div class="flow-col"><h4>' + title + '</h4><div class="muted small">暂无数据</div></div>';
      const max = Math.max.apply(null, items.map(x => Math.abs(x.net_amount || 0))) || 1;
      return '<div class="flow-col"><h4>' + title + '</h4>' + items.slice(0, 10).map(x => {
        const v = x.net_amount || 0;
        const w = Math.max(4, Math.abs(v) / max * 100);
        const c = v >= 0 ? Charts.colors.up : Charts.colors.down;
        return '<div class="flow-item" title="' + esc(x.name) + '：净' + (v >= 0 ? '流入 ' : '流出 ') +
          fmtAmt(Math.abs(v)) + '，涨跌幅 ' + fmtPct(x.change_pct) + '">' +
          '<span class="nm">' + esc(x.name) + ' <span class="muted small">' + fmtPct(x.change_pct) + '</span></span>' +
          '<span class="bar"><i style="width:' + w + '%;background:' + c + '"></i></span>' +
          '<span class="amt ' + (v >= 0 ? 'up' : 'down') + '">' + fmtAmt(v) + '</span>' +
          '</div>';
      }).join('') + '</div>';
    };
    $('sectorFlow').innerHTML = col('资金净流入 TOP10', inflow, 1) + col('资金净流出 TOP10', outflow, -1);
  }

  function renderSectorRank(list) {
    const head = '<thead><tr><th class="left">板块</th><th>涨跌幅</th><th>5日</th><th>20日</th><th class="left">领涨股</th></tr></thead>';
    const body = list.slice(0, 12).map(s =>
      '<tr><td>' + esc(s.name) + '</td>' +
      '<td class="' + signCls(s.change_pct) + '">' + fmtPct(s.change_pct) + '</td>' +
      '<td class="' + signCls(s.change_5d) + '">' + fmtPct(s.change_5d) + '</td>' +
      '<td class="' + signCls(s.change_20d) + '">' + fmtPct(s.change_20d) + '</td>' +
      '<td class="left">' + esc(s.leader_name || '--') + ' <span class="' + signCls(s.leader_change_pct) + '">' + fmtPct(s.leader_change_pct) + '</span></td></tr>'
    ).join('');
    $('sectorRankTable').innerHTML = head + '<tbody>' + (body || emptyRow(5)) + '</tbody>';
  }

  function renderLimitUp(pool) {
    const head = '<thead><tr><th class="left">股票</th><th>连板</th><th>涨幅</th><th>封单</th><th class="left">行业</th><th>首封</th></tr></thead>';
    const body = pool.slice(0, 15).map(p => {
      const full = (p.market || '') + p.code;
      const t = String(p.first_seal_time || '').padStart(6, '0');
      return '<tr class="clickable" data-code="' + full + '">' +
        '<td><div class="name-cell"><span>' + esc(p.name) + '</span><span class="cd">' + p.code + '</span></div></td>' +
        '<td>' + p.board_count + ' 板</td>' +
        '<td class="up">' + fmtPct(p.change_pct) + '</td>' +
        '<td>' + fmtAmt(p.seal_amount) + '</td>' +
        '<td class="left">' + esc(p.industry || '--') + '</td>' +
        '<td>' + (t.length === 6 ? t.slice(0, 2) + ':' + t.slice(2, 4) : '--') + '</td></tr>';
    }).join('');
    $('limitUpTable').innerHTML = head + '<tbody>' + (body || emptyRow(6)) + '</tbody>';
    bindRowClick($('limitUpTable'));
  }

  function stat(k, v, cls, tip) {
    return '<div class="stat"' + (tip ? ' title="' + esc(tip) + '"' : '') +
      '><span class="k">' + k + '</span><span class="v ' + (cls || '') + '">' + v + '</span></div>';
  }
  function emptyRow(cols) {
    return '<tr><td class="empty-row" colspan="' + cols + '">暂无数据</td></tr>';
  }
  function bindRowClick(root) {
    root.querySelectorAll('tr[data-code]').forEach(tr => {
      tr.addEventListener('click', () => openStock(tr.dataset.code));
    });
  }

  /* ============================================================
     个股分析
     ============================================================ */
  async function openStock(code) {
    switchView('stock');
    if (!code) return;
    state.stockCode = code;
    $('stockBody').classList.add('hidden');
    $('stockHead').innerHTML = '<h1>加载中…</h1><p class="muted">正在拉取行情、K线、资金与主力行为数据</p>';
    $('stockMeta').innerHTML = '';
    try {
      const data = await API.stock(code);
      state.stock = data;
      renderStock();
    } catch (e) {
      $('stockHead').innerHTML = '<h1>加载失败</h1><p class="muted">' + esc(e.message) + '</p>';
    }
  }

  function renderStock() {
    const d = state.stock;
    if (!d) return;
    const q = d.quote || {};
    const code = d.code || {};
    $('stockHead').innerHTML =
      '<h1>' + esc(q.name) + ' <span class="mono muted small">' + esc(code.code) + '</span></h1>' +
      '<p class="muted">更新于 ' + esc(d.updated_at || '') + ' · 数据来源：公开行情接口</p>';
    $('stockMeta').innerHTML =
      '<button class="btn btn-ghost" id="btnWatch">' + (inWatch(code.full) ? '★ 已自选' : '☆ 加自选') + '</button>' +
      '<button class="btn btn-primary" id="btnStockAI">生成 AI 点评</button>';
    $('btnWatch').addEventListener('click', () => {
      toggleWatch({ full: code.full, name: q.name, code: code.code });
      $('btnWatch').textContent = inWatch(code.full) ? '★ 已自选' : '☆ 加自选';
    });
    $('btnStockAI').addEventListener('click', runStockAI);

    $('stockBody').classList.remove('hidden');
    renderWarnings(d.warnings || []);
    renderHero(q);
    renderKline();
    renderMinute();
    renderTrend(d);
    renderMainForce(d.main_force || {});
    renderIndicators(d.indicators || {});
    renderFund(d.fund_flow || {});
    renderVsMarket(d.vs_market || {});
    renderLimitUpHistory(d.limit_up || {});
    renderBillboard(d.billboard || []);
    $('stockAI').innerHTML = '<span class="muted">点击右上角「生成 AI 点评」，模型将结合行情、指标、资金与主力行为给出综合研判。</span>';
  }

  function renderWarnings(list) {
    const old = document.getElementById('stockWarnings');
    if (old) old.remove();
    if (!list.length) return;
    const box = document.createElement('div');
    box.id = 'stockWarnings';
    box.className = 'warn-box';
    box.innerHTML = list.map(w => '<div>⚠ ' + esc(w) + '</div>').join('');
    $('stockBody').insertBefore(box, $('stockBody').firstChild);
  }

  function renderHero(q) {
    const cls = signCls(q.change_pct);
    const chips = [
      ['今开', num(q.open, 2)], ['最高', num(q.high, 2)], ['最低', num(q.low, 2)], ['昨收', num(q.prev_close, 2)],
      ['成交额', fmtAmt(q.amount)], ['换手率', num(q.turnover, 2) + '%'], ['量比', num(q.volume_ratio, 2)],
      ['振幅', num(q.amplitude, 2) + '%'], ['总市值', fmtAmt(q.total_cap)], ['市盈率TTM', num(q.pe_ttm, 1)],
      ['市净率', num(q.pb, 2)], ['涨停价', num(q.limit_up, 2)], ['跌停价', num(q.limit_down, 2)]
    ];
    $('stockHero').innerHTML =
      '<div><div class="sh-name">' + esc(q.name) + '</div><div class="sh-code">' + esc(q.code) + '</div></div>' +
      '<div><div class="sh-price ' + cls + '">' + num(q.price, 2) + '</div>' +
      '<div class="sh-chg ' + cls + '">' + (q.change >= 0 ? '+' : '') + num(q.change, 2) + '  ' + fmtPct(q.change_pct) + '</div></div>' +
      '<div class="chips">' + chips.map(c => '<span class="chip">' + c[0] + ' <b>' + c[1] + '</b></span>').join('') + '</div>';
  }

  function renderKline() {
    const d = state.stock;
    const node = d.kline[state.period];
    Charts.kline($('klineChart'), node, { height: 320 });
    const has = (node && node.series) || {};
    const legend = [];
    if (has.ma5) legend.push(['MA5', Charts.colors.ma5]);
    if (has.ma10) legend.push(['MA10', Charts.colors.ma10]);
    if (has.ma20) legend.push(['MA20', Charts.colors.ma20]);
    if (has.ma60) legend.push(['MA60', Charts.colors.ma60]);
    legend.push(['MACD(12,26,9)', Charts.colors.accent]);
    $('klineLegend').innerHTML = legend.map(l => '<span><i style="background:' + l[1] + '"></i>' + l[0] + '</span>').join('') +
      '<span class="muted">前复权 · 红涨绿跌 · 悬停查看当日数值</span>';
  }

  function renderMinute() {
    const d = state.stock;
    Charts.minute($('minuteChart'), d.minute || {}, (d.quote || {}).prev_close, { height: 320 });
    const q = d.quote || {};
    $('minuteLegend').innerHTML =
      '<span><i style="background:' + Charts.colors.up + '"></i>价格</span>' +
      '<span><i style="background:' + Charts.colors.ma5 + '"></i>均价</span>' +
      '<span class="muted">昨收 ' + num(q.prev_close, 2) + ' · 悬停查看分时数值</span>';
  }

  function renderTrend(d) {
    const t = d.trend || {};
    Charts.gauge($('trendGauge'), t.score || 0, {
      suffix: '趋势评分', height: 118,
      level: t.level || '', hint: '综合均线、MACD、量能与其相对强弱计算'
    });
    const toneMap = { bull: 'up', bear: 'down', neutral: 'flat' };
    $('trendChips').innerHTML =
      '<span class="chip ' + (t.tone || 'neutral') + '">' + esc(t.level || '--') + '</span>' +
      (t.labels || []).map(l => '<span class="chip">' + esc(l) + '</span>').join('');
    $('trendStats').innerHTML =
      stat('近5日', fmtPct(t.change_5d), signCls(t.change_5d)) +
      stat('近20日', fmtPct(t.change_20d), signCls(t.change_20d)) +
      stat('近60日', fmtPct(t.change_60d), signCls(t.change_60d)) +
      stat('支撑位', num(t.support, 2), '', '近60日区间下沿，跌破意味着趋势转弱') +
      stat('压力位', num(t.resistance, 2), '', '近60日区间上沿，突破意味着打开空间');
  }

  function renderMainForce(mf) {
    const toneMap = { '拉升': 'bull', '吸筹': 'bull', '多头占优': 'bull', '洗盘': 'neutral', '震荡待确认': 'neutral', '出货': 'bear', '走弱': 'bear', '空头占优': 'bear' };
    const cls = toneMap[mf.phase] || 'neutral';
    $('mfPhase').innerHTML =
      '<div class="phase-badge ' + cls + '">' + esc(mf.phase || '--') + '</div>' +
      '<div class="phase-desc">置信度 <b>' + (mf.confidence || 0) + '%</b> · 命中 ' + (mf.hit_count || 0) + '/' + (mf.total_signals || 0) +
      ' 条 · 60日分位 ' + num(mf.position_60d, 1) + '%<br>' + esc(mf.summary || '') + '</div>';
    const sigs = mf.signals || [];
    $('mfSignals').innerHTML = sigs.map(s =>
      '<div class="signal ' + (s.hit ? 'hit' : 'miss') + '">' +
      '<span class="dot"></span>' +
      '<span><span class="st ' + (s.hit ? (s.side === 'bear' ? 'down' : 'up') : '') + '">' + esc(s.name) + '</span>' +
      '<span class="muted small"> ' + (s.hit ? '命中' : '未命中') + '</span><br>' +
      '<span class="sd">' + esc(s.detail) + '</span></span></div>'
    ).join('');
  }

  function renderIndicators(ind) {
    const items = [
      ['MA5', num(ind.ma5, 2)], ['MA10', num(ind.ma10, 2)], ['MA20', num(ind.ma20, 2)], ['MA60', num(ind.ma60, 2)],
      ['EMA12', num(ind.ema12, 2)], ['EMA50', num(ind.ema50, 2)],
      ['DIF', num(ind.dif, 3)], ['DEA', num(ind.dea, 3)], ['MACD柱', num(ind.hist, 3)],
      ['KDJ-K', num(ind.k, 1)], ['KDJ-D', num(ind.d, 1)], ['KDJ-J', num(ind.j, 1)],
      ['RSI6', num(ind.rsi6, 1)], ['RSI14', num(ind.rsi14, 1)],
      ['BOLL上轨', num(ind.boll_up, 2)], ['BOLL中轨', num(ind.boll_mid, 2)], ['BOLL下轨', num(ind.boll_low, 2)],
      ['ATR14', num(ind.atr14, 2)], ['5日均量', fmtAmt((ind.vol_ma5 || 0) * 100)]
    ];
    $('indicatorGrid').innerHTML = items.map(x =>
      '<div class="stat"><span class="k">' + x[0] + '</span><span class="v">' + x[1] + '</span></div>').join('');
  }

  function renderFund(f) {
    const trend = f.trend || [];
    Charts.bars($('fundChart'), trend.map(x => ({
      label: (x.date || '').slice(5),
      raw: x.net_amount || 0,
      value: Math.abs(x.net_amount || 0),
      text: fmtAmt(x.net_amount || 0),
      color: (x.net_amount || 0) >= 0 ? Charts.colors.up : Charts.colors.down,
      hint: '当日主力资金净' + ((x.net_amount || 0) >= 0 ? '流入' : '流出')
    })), { height: 190, signed: true, markLatest: true });
    const t = f.today || {};
    $('fundStats').innerHTML =
      stat('今日净流入', fmtAmt(t.net_amount), signCls(t.net_amount)) +
      stat('今日净占比', fmtPct(t.net_ratio), signCls(t.net_ratio)) +
      stat('连续净流入', (f.consec_inflow || 0) + ' 日', (f.consec_inflow || 0) > 0 ? 'up' : '') +
      stat('近5日累计', fmtAmt(f.net_5d), signCls(f.net_5d)) +
      stat('近10日累计', fmtAmt(f.net_10d), signCls(f.net_10d)) +
      stat('近20日累计', fmtAmt(f.net_20d), signCls(f.net_20d));
  }

  function renderVsMarket(v) {
    const groups = [];
    [5, 20, 60].forEach(n => {
      const d = v['d' + n];
      if (!d) return;
      groups.push({
        label: '近' + n + '日',
        values: [d.stock, d.index],
        colors: [Charts.colors.up, Charts.colors.accent],
        names: ['个股', '上证指数'],
        subtitle: '个股 vs 上证'
      });
    });
    Charts.groupBars($('vsMarketChart'), groups, { height: 150 });
    $('vsMarketLegend').innerHTML =
      '<span><i style="background:' + Charts.colors.up + '"></i>个股涨跌幅</span>' +
      '<span><i style="background:' + Charts.colors.accent + '"></i>上证指数</span>' +
      '<span class="muted">悬停查看超额收益</span>';
    const idx = v.index || {};
    const rows = [5, 20, 60].map(n => {
      const d = v['d' + n]; if (!d) return '';
      return '<tr><td>近' + n + '日</td>' +
        '<td class="' + signCls(d.stock) + '">' + fmtPct(d.stock) + '</td>' +
        '<td class="' + signCls(d.index) + '">' + fmtPct(d.index) + '</td>' +
        '<td class="' + (d.outperform ? 'up' : 'down') + '">' + (d.outperform ? '跑赢' : '跑输') + ' ' + fmtPct(d.diff) + '</td></tr>';
    }).join('');
    $('vsMarketStats').innerHTML =
      '<div class="stat"><span class="k">上证指数</span><span class="v ' + signCls(idx.change_pct) + '">' + num(idx.price, 2) + ' ' + fmtPct(idx.change_pct) + '</span></div>' +
      '<div class="stat"><span class="k">图例</span><span class="v">红=个股 蓝=指数</span></div>';
    $('vsMarketTable').innerHTML =
      '<thead><tr><th class="left">区间</th><th>个股</th><th>上证</th><th>相对强弱</th></tr></thead><tbody>' +
      (rows || emptyRow(4)) + '</tbody>';
  }

  function renderLimitUpHistory(lu) {
    const last = lu.last;
    $('limitUpHead').innerHTML = last
      ? '最近涨停 <b>' + esc(last.date) + '</b> ' + fmtPct(last.change_pct) + ' · 距今 ' + (lu.last_days_ago === null ? '--' : lu.last_days_ago) + ' 天 · 近一年 ' + (lu.count_250d || 0) + ' 次'
      : '该股在统计区间内无涨停记录';
    const head = '<thead><tr><th class="left">涨停日期</th><th>收盘涨幅</th><th>收盘价</th><th>连板</th></tr></thead>';
    const body = (lu.records || []).map(r =>
      '<tr><td>' + esc(r.date) + '</td><td class="up">' + fmtPct(r.change_pct) + '</td>' +
      '<td>' + num(r.close, 2) + '</td><td>' + (r.streak > 1 ? r.streak + ' 连板' : '首板') + '</td></tr>'
    ).join('');
    $('limitUpHistory').innerHTML = head + '<tbody>' + (body || emptyRow(4)) + '</tbody>';
  }

  function renderBillboard(list) {
    const head = '<thead><tr><th class="left">日期</th><th>涨跌幅</th><th>净买入</th><th class="left">上榜原因</th></tr></thead>';
    const body = list.map(b =>
      '<tr><td>' + esc(b.date) + '</td><td class="' + signCls(b.change_pct) + '">' + fmtPct(b.change_pct) + '</td>' +
      '<td class="' + signCls(b.net_amount) + '">' + fmtAmt(b.net_amount) + '</td>' +
      '<td class="left" style="white-space:normal;font-family:inherit">' + esc(b.reason) + '</td></tr>'
    ).join('');
    $('billboardTable').innerHTML = head + '<tbody>' + (body || emptyRow(4)) + '</tbody>';
  }

  /* ---------------- AI 流式统一渲染 ---------------- */
  function formatAnswer(text) {
    return esc(text).split('\n').map(line => {
      const t = line.trim();
      if (/^[一二三四五六七八九十]+、/.test(t)) return '<b class="ai-h">' + t + '</b>';
      if (/^（[一二三四五六七八九十]+）/.test(t)) return '<b class="ai-h2">' + t + '</b>';
      return line;
    }).join('\n');
  }

  function runAI(opts) {
    const box = $(opts.boxId);
    const btn = opts.btnId ? $(opts.btnId) : null;
    const modelNode = opts.modelId ? $(opts.modelId) : null;
    if (btn) btn.disabled = true;
    box.classList.remove('hidden');

    let thinking = '';
    let answer = '';
    let answerStarted = false;
    let finished = false;

    let rafPending = false;
    function paint() {
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame(() => {
        rafPending = false;
        render();
      });
    }

    function render() {
      let html = '';
      if (thinking || !answerStarted) {
        html += '<div class="ai-think' + (answerStarted ? ' collapsed' : '') + '" id="' + opts.boxId + '-think">' +
          '<div class="ai-think-head"><span>模型深度思考中 <b id="' + opts.boxId + '-tk">' + thinking.length + '</b> 字' +
          (answerStarted ? '（已折叠，点击展开）' : '…') + '</span><span class="small">' +
          (answerStarted ? '展开 / 收起' : '正在推理') + '</span></div>' +
          '<div class="ai-think-body">' + esc(thinking) + '</div></div>';
      }
      html += '<div class="ai-answer">' + (answer ? formatAnswer(answer) : '<span class="muted">等待模型输出…</span>') +
        (answerStarted && !finished ? '<span class="cursor"></span>' : '') + '</div>';
      box.innerHTML = html;
      const head = $(opts.boxId + '-think');
      if (head) {
        head.querySelector('.ai-think-head').addEventListener('click', () => head.classList.toggle('collapsed'));
      }
      box.scrollTop = box.scrollHeight;
    }

    paint();
    API.aiStream(opts.payload, {
      onMeta: (m) => { if (modelNode) modelNode.textContent = m.model || ''; },
      onThinking: (t) => { thinking += t; paint(); },
      onDelta: (t) => { answerStarted = true; answer += t; paint(); },
      onError: (msg) => { box.innerHTML = '<span class="down">生成失败：' + esc(msg) + '</span>'; },
      onDone: () => {
        finished = true;
        if (!answer) {
          box.innerHTML = '<span class="muted">模型未返回正文内容，请重试。</span>';
          return;
        }
        render();
      }
    }).catch(e => { box.innerHTML = '<span class="down">生成失败：' + esc(e.message) + '</span>'; })
      .finally(() => { if (btn) btn.disabled = false; });
  }

  function runStockAI() {
    if (!state.stockCode) return;
    runAI({ boxId: 'stockAI', btnId: 'btnStockAI', modelId: 'stockModel',
      payload: { scene: 'stock', code: state.stockCode } });
  }

  function runMarketAI() {
    runAI({ boxId: 'marketAI', btnId: 'btnMarketAI', modelId: 'marketModel', payload: { scene: 'market' } });
  }

  function runScreenerAI() {
    const r = state.result || {};
    const items = r.items || [];
    if (!items.length) { toast('暂无可解读的结果'); return; }
    runAI({ boxId: 'screenerAI', btnId: 'btnScreenerAI',
      payload: { scene: 'screener', strategy_name: r.strategy_name || '', items: items } });
  }

  /* ============================================================
     智能选股
     ============================================================ */
  async function loadStrategies() {
    try {
      const data = await API.strategies();
      state.strategies = data;
      renderStrategies();
      renderCustomPanel();
    } catch (e) { toast('策略加载失败：' + e.message); }
  }

  function renderStrategies() {
    const data = state.strategies || {};
    const list = data.strategies || [];
    const cats = data.categories || [];
    const grid = $('strategyGrid');

    const catName = {};
    cats.forEach(c => { catName[c.id] = c.name; });

    function card(s) {
      const active = state.strategy === s.id ? ' active' : '';
      const tag = s.category === 'quant'
        ? '<span class="st-tag">量化</span>' : '';
      return '<button class="strategy-item' + active + '" data-id="' + s.id + '">' +
        '<div class="sn"><span class="sn-name">' + esc(s.name) + '</span>' + tag + '</div>' +
        '<div class="sn-desc">' + esc(s.desc) + '</div></button>';
    }

    const groups = cats.map(c => ({
      id: c.id, name: c.name, items: list.filter(s => s.category === c.id)
    })).filter(g => g.items.length);

    const known = new Set(cats.map(c => c.id));
    const others = list.filter(s => !known.has(s.category));
    if (others.length) groups.push({ id: 'other', name: '其他', items: others });

    let html = groups.map(g =>
      '<div class="strategy-group">' +
      '<div class="strategy-group-title">' + esc(g.name) +
      '<span class="sgt-count">' + g.items.length + '</span></div>' +
      '<div class="strategy-grid">' + g.items.map(card).join('') + '</div>' +
      '</div>').join('');

    html += '<div class="strategy-group">' +
      '<div class="strategy-group-title">自定义<span class="sgt-count">1</span></div>' +
      '<div class="strategy-grid">' + card({ id: 'custom', name: '自定义条件', desc: '自己组合多因子条件' }) + '</div>' +
      '</div>';

    grid.innerHTML = html;
    grid.querySelectorAll('.strategy-item').forEach(b => {
      b.addEventListener('click', () => {
        state.strategy = b.dataset.id;
        renderStrategies();
      });
    });

    const spec = list.find(s => s.id === state.strategy);
    $('strategyDetail').innerHTML = !spec
      ? '<div class="muted">当前为「自定义条件」模式，请在下方填写筛选条件。</div>'
      : '<div class="sd-head"><b>' + esc(spec.name) + '</b>' +
        (spec.category ? '<span class="sd-cat">' + esc(catName[spec.category] || '') + '</span>' : '') +
        '<span class="muted">' + esc(spec.desc) + '</span></div>' +
        (spec.basis ? '<div class="sd-basis"><b>方法依据</b>' + esc(spec.basis) + '</div>' : '') +
        '<ul>' + spec.rules.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>';
  }

  const CUSTOM_FIELDS = [
    { key: 'change_pct', label: '涨跌幅 %', pair: true },
    { key: 'turnover', label: '换手率 %', pair: true },
    { key: 'amount', label: '成交额（亿元）≥', single: true },
    { key: 'volume_ratio', label: '量比 ≥', single: true },
    { key: 'cap', label: '总市值（亿元）', pair: true },
    { key: 'pe', label: '市盈率', pair: true },
    { key: 'change_60d_min', label: '60日涨幅 % ≥', single: true },
    { key: 'from_high_max', label: '距60日高点 % ≤', single: true }
  ];

  function renderCustomPanel() {
    $('customPanel').innerHTML = CUSTOM_FIELDS.map(f => {
      if (f.pair) {
        return '<div class="field"><label>' + f.label + '</label><div class="field-pair">' +
          '<input type="number" step="any" data-ck="' + f.key + '_min" placeholder="最小">' +
          '<span class="muted small">~</span>' +
          '<input type="number" step="any" data-ck="' + f.key + '_max" placeholder="最大">' +
          '</div></div>';
      }
      return '<div class="field"><label>' + f.label + '</label><input type="number" step="any" data-ck="' +
        f.key + '" placeholder="不限"></div>';
    }).join('') +
      '<div class="field" style="grid-column:1/-1"><label>均线条件</label><div class="check-row">' +
      '<label><input type="checkbox" data-ck="ma5" data-group="price_above_ma"> 站上MA5</label>' +
      '<label><input type="checkbox" data-ck="ma20" data-group="price_above_ma"> 站上MA20</label>' +
      '<label><input type="checkbox" data-ck="ma60" data-group="price_above_ma"> 站上MA60</label>' +
      '<label><input type="checkbox" data-ck="macd_bull"> MACD柱>0</label>' +
      '<label><input type="checkbox" data-ck="ma_align"> 均线多头排列</label>' +
      '</div></div>';
  }

  function collectCustom() {
    const out = {};
    const mas = [];
    $('customPanel').querySelectorAll('[data-ck]').forEach(node => {
      const key = node.dataset.ck;
      if (node.type === 'checkbox') {
        if (node.dataset.group === 'price_above_ma') { if (node.checked) mas.push(key); }
        else if (node.checked) out[key] = true;
        return;
      }
      const v = node.value.trim();
      if (v === '') return;
      out[key] = Number(v);
    });
    if (mas.length) out.price_above_ma = mas;
    return out;
  }

  async function startScan() {
    const body = {
      strategy: state.strategy,
      pool_size: Number($('poolSize').value) || 400
    };
    if (state.strategy === 'custom') {
      body.custom = collectCustom();
      const meaningful = Object.keys(body.custom).length;
      if (!meaningful) { toast('请至少填写一个自定义条件'); return; }
    }
    $('btnScan').disabled = true;
    $('btnStop').classList.remove('hidden');
    $('btnScreenerAI').classList.add('hidden');
    $('screenerAI').classList.add('hidden');
    $('resultTable').innerHTML = '';
    $('resultSummary').textContent = '';
    state.sortTouched = false;
    state.sortKey = 'amount';
    state.sortAsc = false;
    setProgress(0);
    $('scanStatus').textContent = '正在获取候选池…';
    try {
      const r = await API.screenerStart(body);
      state.jobId = r.job_id;
      pollJob();
    } catch (e) {
      $('scanStatus').textContent = '启动失败：' + e.message;
      resetScan();
    }
  }

  function setProgress(ratio) {
    $('scanProgress').style.width = Math.max(0, Math.min(100, ratio * 100)) + '%';
  }

  function resetScan() {
    $('btnScan').disabled = false;
    $('btnStop').classList.add('hidden');
    clearTimeout(state.pollTimer);
  }

  function pollJob() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      if (!state.jobId) return;
      try {
        const p = await API.screenerProgress(state.jobId);
        if (p.total) setProgress(p.done / p.total);
        $('scanStatus').textContent = '正在扫描，已完成 ' + p.done + ' / ' + p.total +
          '（逐只拉取日K并计算指标，约需数十秒）';
        if (p.status === 'done' || p.status === 'stopped') {
          state.result = p.result || {};
          renderResult();
          $('scanStatus').textContent = (p.status === 'stopped' ? '已取消，' : '') +
            '共命中 ' + (state.result.count || 0) + ' 只（有效扫描 ' + (state.result.valid_count || 0) +
            ' / ' + (state.result.pool_size || 0) + ' 只）';
          resetScan();
        } else if (p.status === 'error') {
          $('scanStatus').textContent = '扫描失败：' + (p.error || '未知错误');
          resetScan();
        } else {
          pollJob();
        }
      } catch (e) {
        $('scanStatus').textContent = '查询进度失败：' + e.message;
        resetScan();
      }
    }, 800);
  }

  function renderResult() {
    const r = state.result || {};
    const items = (r.items || []).slice();
    const hasScore = items.some(x => x.score !== undefined);
    /* 多因子结果默认按综合分排序，其余按成交额 */
    if (hasScore && !state.sortTouched) {
      state.sortKey = 'score';
      state.sortAsc = false;
    }
    const key = state.sortKey, asc = state.sortAsc;
    items.sort((a, b) => {
      const va = a[key], vb = b[key];
      if (typeof va === 'string') return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      return asc ? (va || 0) - (vb || 0) : (vb || 0) - (va || 0);
    });
    $('resultSummary').textContent = r.strategy_name
      ? ('策略：' + r.strategy_name + ' · 有效扫描 ' + (r.valid_count || 0) + '/' + (r.pool_size || 0) + ' 只')
      : '';
    const cols = [
      { k: 'name', t: '股票', left: true },
      { k: 'price', t: '现价' },
      { k: 'change_pct', t: '涨跌幅' },
      { k: 'turnover', t: '换手率' },
      { k: 'volume_ratio', t: '量比' },
      { k: 'amount', t: '成交额' },
      { k: 'total_cap', t: '总市值' },
      { k: 'pe', t: '市盈率' }
    ];
    if (hasScore) cols.splice(1, 0, { k: 'score', t: '综合分' });
    const head = '<thead><tr>' + cols.map(c =>
      '<th class="' + (c.left ? 'left ' : '') + '" data-sort="' + c.k + '">' + c.t +
      (state.sortKey === c.k ? (asc ? ' ▲' : ' ▼') : '') + '</th>').join('') +
      '<th class="left">命中说明</th></tr></thead>';
    const body = items.map(it =>
      '<tr class="clickable" data-code="' + it.full + '">' +
      '<td><div class="name-cell"><span>' + esc(it.name) + '</span><span class="cd">' + esc(it.code) + '</span></div></td>' +
      (hasScore ? '<td class="' + (it.score >= 80 ? 'up' : '') + '"><b>' + num(it.score, 1) + '</b></td>' : '') +
      '<td>' + num(it.price, 2) + '</td>' +
      '<td class="' + signCls(it.change_pct) + '">' + fmtPct(it.change_pct) + '</td>' +
      '<td>' + num(it.turnover, 2) + '%</td>' +
      '<td>' + num(it.volume_ratio, 2) + '</td>' +
      '<td>' + fmtAmt(it.amount) + '</td>' +
      '<td>' + fmtAmt(it.total_cap) + '</td>' +
      '<td>' + num(it.pe, 1) + '</td>' +
      '<td class="left" style="white-space:normal;font-family:inherit;min-width:280px">' + esc(it.reason) + '</td></tr>'
    ).join('');
    const empty = '<tr><td class="empty-row" colspan="' + (cols.length + 1) + '">' +
      '当前市场环境下该策略没有符合条件的标的。可换用其它策略，或把候选池扩大到 600 / 800 只。</td></tr>';
    $('resultTable').innerHTML = head + '<tbody>' + (body || empty) + '</tbody>';
    bindRowClick($('resultTable'));
    $('resultTable').querySelectorAll('th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const k = th.dataset.sort;
        if (state.sortKey === k) state.sortAsc = !state.sortAsc;
        else { state.sortKey = k; state.sortAsc = false; }
        state.sortTouched = true;
        renderResult();
      });
    });
    if (items.length) $('btnScreenerAI').classList.remove('hidden');
    else $('btnScreenerAI').classList.add('hidden');
    if (items.length) $('btnExport').classList.remove('hidden');
    else $('btnExport').classList.add('hidden');
  }

  function exportCsv() {
    const r = state.result || {};
    const items = r.items || [];
    if (!items.length) { toast('暂无可导出的结果'); return; }
    const head = ['代码', '名称', '现价', '涨跌幅%', '换手率%', '量比', '成交额', '总市值', '市盈率', '命中说明'];
    const esc2 = (v) => '"' + String(v === null || v === undefined ? '' : v).replace(/"/g, '""') + '"';
    const lines = [head.map(esc2).join(',')];
    items.forEach(it => {
      lines.push([
        it.code, it.name, it.price, it.change_pct, it.turnover, it.volume_ratio,
        Math.round(it.amount || 0), Math.round(it.total_cap || 0), it.pe, it.reason
      ].map(esc2).join(','));
    });
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '选股结果_' + (r.strategy_name || '') + '_' +
      new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    toast('已导出 ' + items.length + ' 条结果');
  }

  function runScreenerAI() {
    const r = state.result || {};
    const items = r.items || [];
    if (!items.length) { toast('暂无可解读的结果'); return; }
    runAI({ boxId: 'screenerAI', btnId: 'btnScreenerAI',
      payload: { scene: 'screener', strategy_name: r.strategy_name || '', items: items } });
  }

  /* ============================================================
     自选股
     ============================================================ */
  async function renderWatchlist() {
    const grid = $('watchGrid');
    if (!state.watch.length) {
      grid.innerHTML = '<div class="card muted" style="grid-column:1/-1">自选列表为空。进入「个股分析」后可点击「☆ 加自选」添加。</div>';
      return;
    }
    grid.innerHTML = state.watch.map(w =>
      '<div class="card watch-card" data-code="' + w.full + '">' +
      '<button class="wc-del" data-del="' + w.full + '">×</button>' +
      '<div class="wc-top"><span class="wc-name">' + esc(w.name || w.code) + '</span>' +
      '<span class="wc-code">' + esc(w.code) + '</span></div>' +
      '<div class="wc-price" data-price="' + w.full + '">--</div>' +
      '<div class="wc-chg" data-chg="' + w.full + '">--</div>' +
      '</div>'
    ).join('');
    grid.querySelectorAll('.wc-del').forEach(b => {
      b.addEventListener('click', (ev) => {
        ev.stopPropagation();
        state.watch = state.watch.filter(w => w.full !== b.dataset.del);
        saveWatch();
        renderWatchlist();
      });
    });
    grid.querySelectorAll('.watch-card').forEach(c => {
      c.addEventListener('click', () => openStock(c.dataset.code));
    });
    await refreshWatchQuotes();
  }

  async function refreshWatchQuotes() {
    const codes = state.watch.map(w => w.full);
    if (!codes.length) return;
    try {
      const json = await API.quotes(codes);
      (json.items || []).forEach(q => {
        const p = document.querySelector('[data-price="' + q.full + '"]');
        const c = document.querySelector('[data-chg="' + q.full + '"]');
        const cls = signCls(q.change_pct);
        if (p) { p.textContent = num(q.price, 2); p.className = 'wc-price ' + cls; }
        if (c) { c.textContent = (q.change >= 0 ? '+' : '') + num(q.change, 2) + '  ' + fmtPct(q.change_pct); c.className = 'wc-chg ' + cls; }
      });
    } catch (e) { toast('自选行情刷新失败：' + e.message); }
  }

  /* ============================================================
     搜索
     ============================================================ */
  let searchTimer = null;
  function bindSearch() {
    const input = $('searchInput');
    const drop = $('searchDrop');
    input.addEventListener('input', () => {
      const v = input.value.trim();
      clearTimeout(searchTimer);
      if (!v) { drop.classList.remove('show'); return; }
      searchTimer = setTimeout(async () => {
        try {
          const data = await API.search(v);
          const items = (data.items || []).slice(0, 12);
          if (!items.length) { drop.classList.remove('show'); return; }
          drop.innerHTML = items.map(i =>
            '<div class="search-item" data-code="' + i.full + '"><span>' + esc(i.name) + '</span>' +
            '<span class="code">' + esc(i.code) + '</span></div>').join('');
          drop.classList.add('show');
          drop.querySelectorAll('.search-item').forEach(node => {
            node.addEventListener('click', () => {
              drop.classList.remove('show');
              input.value = node.textContent.trim();
              openStock(node.dataset.code);
            });
          });
        } catch (e) { /* ignore */ }
      }, 240);
    });
    input.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter') return;
      const v = input.value.trim();
      if (!v) return;
      drop.classList.remove('show');
      const norm = normalizeInput(v);
      openStock(norm || v);
    });
    document.addEventListener('click', (ev) => {
      if (!ev.target.closest('.search-wrap')) drop.classList.remove('show');
    });
  }

  /* ============================================================
     AI 投研助手（侧边对话）
     ============================================================ */
  const chat = {
    open: false,
    seeded: false,
    streaming: false,
    controller: null,
    history: []
  };

  function chatAutoGrow() {
    const t = $('chatInput');
    if (!t) return;
    t.style.height = 'auto';
    t.style.height = Math.min(120, Math.max(40, t.scrollHeight)) + 'px';
  }

  function chatScroll() {
    const b = $('chatBody');
    if (b) b.scrollTop = b.scrollHeight;
  }

  /* 轻量 markdown：加粗 / 行内代码 / 小标题 / 列表符号 */
  function formatChat(t) {
    let s = esc(t);
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
    s = s.replace(/^#{1,4}\s*(.+)$/gm, '<span class="chat-h">$1</span>');
    s = s.replace(/^\s*[-*]\s+/gm, '· ');
    return s;
  }

  function chatUserMsg(text) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg user';
    wrap.innerHTML = '<div class="chat-main"><div class="chat-bubble">' + esc(text) + '</div></div>';
    $('chatBody').appendChild(wrap);
    chatScroll();
  }

  function chatBotStatic(text) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg bot';
    wrap.innerHTML = '<div class="chat-avatar">AI</div>' +
      '<div class="chat-main"><div class="chat-bubble">' + formatChat(text) + '</div></div>';
    $('chatBody').appendChild(wrap);
    chatScroll();
    return wrap;
  }

  function chatBotStreamBox() {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg bot';
    wrap.innerHTML = '<div class="chat-avatar">AI</div>' +
      '<div class="chat-main">' +
      '<div class="ai-think hidden">' +
      '<div class="ai-think-head"><span>思考中 <b>0</b> 字</span><span class="small">展开</span></div>' +
      '<div class="ai-think-body"></div></div>' +
      '<div class="chat-bubble"></div></div>';
    $('chatBody').appendChild(wrap);
    chatScroll();
    return wrap;
  }

  function seedChat() {
    if (chat.seeded) return;
    chat.seeded = true;
    chatBotStatic('你好，我是投研助手。\n\n可以问我：技术指标含义、当前页面数据怎么读、选股策略逻辑、交易规则与风险控制。\n我会结合你当前打开的页面实时数据回答，不编造行情。');
  }

  function renderQuickAsk() {
    const qs = [];
    if (state.view === 'stock' && state.stockCode) {
      const nm = (state.stock && state.stock.quote && state.stock.quote.name) || state.stockCode;
      qs.push(nm + ' 的技术面怎么解读？');
      qs.push('主力行为判定可信吗？');
    } else if (state.view === 'market') {
      qs.push('今天市场情绪如何？');
      qs.push('涨停梯队说明什么？');
    } else if (state.view === 'screener') {
      qs.push('这些选股策略分别适合什么行情？');
      qs.push('多因子综合评分怎么理解？');
    } else {
      qs.push('自选股应该怎么跟踪？');
    }
    qs.push('MACD 金叉是什么意思？');
    qs.push('量比和换手率怎么看？');
    const box = $('chatQuick');
    box.innerHTML = qs.slice(0, 4).map(q =>
      '<button type="button" class="chat-chip" data-q="' + esc(q) + '">' + esc(q) + '</button>').join('');
    box.querySelectorAll('.chat-chip').forEach(b => {
      b.addEventListener('click', () => sendChat(b.dataset.q));
    });
  }

  function toggleChat(force) {
    const open = force === undefined ? !chat.open : !!force;
    chat.open = open;
    $('chatPanel').classList.toggle('open', open);
    $('chatMask').classList.toggle('show', open);
    $('chatFab').classList.toggle('hidden-fab', open);
    $('chatPanel').setAttribute('aria-hidden', open ? 'false' : 'true');
    document.body.style.overflow = open ? 'hidden' : '';
    if (open) {
      seedChat();
      renderQuickAsk();
      chatScroll();
      setTimeout(() => { const t = $('chatInput'); if (t) t.focus(); }, 240);
    }
  }

  async function sendChat(raw) {
    const text = String(raw || '').trim();
    if (!text || chat.streaming) return;

    seedChat();
    $('chatInput').value = '';
    chatAutoGrow();

    chatUserMsg(text);
    chat.history.push({ role: 'user', content: text });

    const box = chatBotStreamBox();
    const thinkEl = box.querySelector('.ai-think');
    const thinkBody = box.querySelector('.ai-think-body');
    const answerEl = box.querySelector('.chat-bubble');

    chat.streaming = true;
    chat.controller = new AbortController();
    const btn = $('chatSend');
    btn.classList.add('stop');
    btn.querySelector('.send-ico').textContent = '■';

    let thinking = '';
    let answer = '';
    let errMsg = null;
    let rafPending = false;

    function paint() {
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame(() => {
        rafPending = false;
        if (thinking) {
          thinkEl.classList.remove('hidden');
          thinkEl.querySelector('b').textContent = thinking.length;
          thinkBody.textContent = thinking;
          thinkEl.querySelector('.ai-think-head .small').textContent = answer ? '已折叠' : '展开';
          if (answer) thinkEl.classList.add('collapsed');
        }
        if (answer) {
          answerEl.innerHTML = formatChat(answer) + (finished ? '' : '<span class="cursor"></span>');
        } else if (!errMsg) {
          answerEl.innerHTML = '<span class="muted">正在思考…</span>';
        }
        chatScroll();
      });
    }

    let finished = false;
    paint();

    const ctx = { view: state.view };
    if (state.view === 'stock' && state.stockCode) ctx.code = state.stockCode;

    try {
      await API.aiStream(
        { messages: chat.history.slice(-10), context: ctx },
        {
          onMeta: (m) => { if (m && m.model) $('chatModelLabel').textContent = m.model; },
          onThinking: (t) => { thinking += t; paint(); },
          onDelta: (t) => { answer += t; paint(); },
          onError: (msg) => {
            errMsg = msg || '模型返回异常';
            answerEl.innerHTML = '<span class="chat-err">生成失败：' + esc(errMsg) + '</span>';
            chatScroll();
          }
        },
        { endpoint: '/api/chat/stream', signal: chat.controller.signal }
      );
      finished = true;
      if (!errMsg && answer) {
        if (thinking) {
          thinkEl.classList.add('collapsed');
          thinkEl.querySelector('.ai-think-head .small').textContent = '已折叠';
        }
        answerEl.innerHTML = formatChat(answer);
        chat.history.push({ role: 'assistant', content: answer });
        if (chat.history.length > 20) chat.history = chat.history.slice(-20);
        chatScroll();
      } else if (!errMsg && !answer) {
        answerEl.innerHTML = '<span class="muted">模型未返回正文内容，请重试。</span>';
      }
    } catch (e) {
      finished = true;
      if (e && e.name === 'AbortError') {
        answerEl.innerHTML = (answer ? formatChat(answer) : '<span class="muted">已停止</span>') +
          '<div class="muted small" style="margin-top:6px">已停止生成</div>';
        if (answer) chat.history.push({ role: 'assistant', content: answer });
      } else {
        answerEl.innerHTML = '<span class="chat-err">请求失败：' +
          esc(e && e.message ? e.message : '未知错误') + '</span>';
      }
    } finally {
      chat.streaming = false;
      chat.controller = null;
      btn.classList.remove('stop');
      btn.querySelector('.send-ico').textContent = '↑';
      chatScroll();
    }
  }

  function onChatSend() {
    if (chat.streaming) {
      if (chat.controller) chat.controller.abort();
      return;
    }
    sendChat($('chatInput').value);
  }

  function bindChat() {
    $('chatFab').addEventListener('click', () => toggleChat(true));
    $('chatClose').addEventListener('click', () => toggleChat(false));
    $('chatMask').addEventListener('click', () => toggleChat(false));
    $('chatSend').addEventListener('click', onChatSend);
    $('chatForm').addEventListener('submit', (ev) => { ev.preventDefault(); onChatSend(); });
    $('chatInput').addEventListener('input', chatAutoGrow);
    $('chatInput').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        onChatSend();
      }
    });
    $('chatClear').addEventListener('click', () => {
      if (chat.streaming) { toast('请先停止当前回答'); return; }
      chat.history = [];
      chat.seeded = false;
      $('chatBody').innerHTML = '';
      seedChat();
      renderQuickAsk();
      toast('已清空对话');
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && chat.open) toggleChat(false);
    });
  }

  /* ============================================================
     初始化
     ============================================================ */
  function bindNav() {
    $('nav').addEventListener('click', (ev) => {
      const btn = ev.target.closest('.nav-item');
      if (btn) switchView(btn.dataset.view);
    });
    $('btnRefresh').addEventListener('click', () => {
      if (state.view === 'market') loadMarket(true);
      else if (state.view === 'stock' && state.stockCode) openStock(state.stockCode);
      else if (state.view === 'watchlist') renderWatchlist();
      else toast('当前页面无需刷新');
    });
    $('btnMarketAI').addEventListener('click', runMarketAI);
    $('btnScan').addEventListener('click', startScan);
    $('btnStop').addEventListener('click', async () => {
      if (!state.jobId) return;
      try { await API.screenerStop(state.jobId); toast('已请求取消，等待当前批次结束'); }
      catch (e) { toast('取消失败：' + e.message); }
    });
    $('btnScreenerAI').addEventListener('click', runScreenerAI);
    $('btnExport').addEventListener('click', exportCsv);
    $('btnToggleCustom').addEventListener('click', () => $('customPanel').classList.toggle('hidden'));
    $('btnWatchRefresh').addEventListener('click', renderWatchlist);
    $('periodSeg').addEventListener('click', (ev) => {
      const b = ev.target.closest('.seg-item'); if (!b) return;
      state.period = b.dataset.period;
      $('periodSeg').querySelectorAll('.seg-item').forEach(x => x.classList.toggle('active', x === b));
      renderKline();
    });
    $('sectorFlowSeg').addEventListener('click', (ev) => {
      const b = ev.target.closest('.seg-item'); if (!b) return;
      state.marketSectorKind = b.dataset.kind;
      $('sectorFlowSeg').querySelectorAll('.seg-item').forEach(x => x.classList.toggle('active', x === b));
      renderSectorFlow();
    });
  }

  async function checkHealth() {
    const pill = $('statusPill');
    try {
      const h = await API.health();
      pill.textContent = h.ai_ready ? ('模型就绪 · ' + h.model) : '模型未配置 Key';
      pill.className = 'status-pill ' + (h.ai_ready ? 'ok' : 'err');
    } catch (e) {
      pill.textContent = '服务不可用';
      pill.className = 'status-pill err';
    }
  }

  function init() {
    bindNav();
    bindSearch();
    bindChat();
    checkHealth();
    switchView('market');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

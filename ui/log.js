(function () {
  'use strict';

  const SEAT = {
    D: { label: '主驾', cls: 'd' },
    P: { label: '副驾', cls: 'p' },
    'null': { label: '', cls: 'single' }
  };

  const state = {
    sessions: [],
    selectedId: null,
    data: null,
    openFolds: new Set(),
    loading: false
  };
  const dom = {};

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function safeId(value) { return String(value).replace(/[^A-Za-z0-9_-]/g, '-'); }
  function seatOf(seat) { return SEAT[seat] || SEAT['null']; }
  function score(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(2) : '—';
  }
  function pct(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n * 100))) : 0;
  }
  function ms(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return n < 1000 ? Math.round(n) + ' ms' : (n / 1000).toFixed(1) + ' 秒';
  }
  function clock(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    const d = new Date(n);
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(function (x) { return String(x).padStart(2, '0'); }).join(':');
  }
  function day(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    const d = new Date(n);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0');
  }
  function shortJson(value, max) {
    let s;
    try { s = JSON.stringify(value); } catch (e) { s = String(value); }
    if (s == null) s = '—';
    max = max || 150;
    return s.length > max ? s.slice(0, max) + '…' : s;
  }
  function oneLine(value, max) {
    const s = String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
    max = max || 110;
    return s.length > max ? s.slice(0, max) + '…' : s;
  }
  function foldKey(ev, name) { return String(ev.seq) + '::' + name; }
  function isOpen(ev, name) { return state.openFolds.has(foldKey(ev, name)); }

  function engineLabel(engine) {
    if (!engine) return '未记录引擎';
    const key = String(engine).toLowerCase();
    if (key.indexOf('rule') >= 0 || key.indexOf('offline') >= 0) return '离线规则替身';
    if (key.indexOf('llm') >= 0 || key.indexOf('model') >= 0) return '读心模型';
    return String(engine);
  }
  function sourceLabel(source) {
    return ({
      rules_offline: '离线规则', text: '文字输入', text_send: '文字发送即收住',
      '8009': '真判停 8009', real: '真判停 8009', offline: '离线替身'
    })[source] || (source || '未记录来源');
  }
  function toolName(name) {
    return String(name || '').replace(/^tool:/, '') || '未记录工具';
  }
  function doorLabel(route) {
    return route === 'agent' ? '开闸' : '只记上下文';
  }

  async function api(path) {
    const resp = await fetch(path, { cache: 'no-store' });
    const body = await resp.json().catch(function () { return {}; });
    if (!resp.ok) throw new Error(body.error || ('HTTP ' + resp.status));
    return body;
  }

  async function loadSessions(keepSelection) {
    dom.refreshBtn.disabled = true;
    try {
      const body = await api('/api/neixinxi/log/sessions');
      state.sessions = body.sessions || [];
      const wanted = keepSelection ? state.selectedId : null;
      const found = wanted && state.sessions.some(function (s) { return s.session_id === wanted; });
      state.selectedId = found ? wanted : (state.sessions[0] && state.sessions[0].session_id) || null;
      renderSessionBar();
      if (!state.selectedId) {
        state.data = null;
        renderBanner();
        renderMain();
        renderDrawerSource();
        return;
      }
      await loadSession(state.selectedId);
    } catch (error) {
      dom.truthBanner.className = 'truth-banner is-empty';
      dom.truthBanner.innerHTML = '<span class="tb-label">读取失败</span><p>' +
        esc('日志服务没有返回可读记录：' + error.message) + '</p>';
    } finally {
      dom.refreshBtn.disabled = false;
    }
  }

  async function loadSession(sessionId) {
    state.selectedId = sessionId;
    state.openFolds.clear();
    state.loading = true;
    try {
      const body = await api('/api/neixinxi/log/' + encodeURIComponent(sessionId));
      state.data = body;
      renderSessionBar();
      renderBanner();
      renderMain();
      renderDrawerSource();
    } catch (error) {
      dom.truthBanner.className = 'truth-banner is-empty';
      dom.truthBanner.innerHTML = '<span class="tb-label">读取失败</span><p>' +
        esc('这条会话读不出来：' + error.message) + '</p>';
    } finally {
      state.loading = false;
    }
  }

  function currentSession() {
    return state.sessions.find(function (item) { return item.session_id === state.selectedId; }) || null;
  }

  function renderBanner() {
    const el = dom.truthBanner;
    if (!state.sessions.length) {
      el.className = 'truth-banner is-empty';
      el.innerHTML = '<span class="tb-label">空态</span><p>还没有真实运行记录。先去 <a href="/neixinxi">内心戏</a> 完成一轮（收住 → 办理 → 回执上屏），再回这里看时间线。这是空态，不是检索无结果；跨会话检索还没建设。</p>';
      return;
    }
    const detail = state.data;
    const events = (detail && detail.events) || [];
    const hasMind = events.some(function (e) { return e.kind === 'mind'; });
    const hasFrame = events.some(function (e) { return e.kind === 'frame'; });
    const missing = [];
    if (!hasMind) missing.push('读心未记录');
    if (!hasFrame) missing.push('画面未记录');
    el.className = 'truth-banner is-real';
    el.innerHTML = '<span class="tb-label">真实记录</span><p>下面每个时间点都来自这次会话的落盘记录，按发生先后分别挂在中轴两侧，不是左右成对。' +
      (missing.length ? '缺的项照实标：<strong>' + esc(missing.join(' · ')) + '</strong>，不编造时间，也不拿示例图顶上。' : '') +
      '产品会话与主链会话已用记录键关联；页面那一拍「要不要回答」只作记录，以主链判答为准。</p>';
  }

  function renderSessionBar() {
    const el = dom.sessionBar;
    if (!state.sessions.length) {
      el.hidden = true;
      el.innerHTML = '';
      return;
    }
    el.hidden = false;
    el.innerHTML = state.sessions.map(function (item) {
      const active = item.session_id === state.selectedId;
      const first = item.first_text ? '「' + oneLine(item.first_text, 14) + '」' : '（无原话）';
      const bits = [];
      if (item.mind_count) bits.push('读心 ' + item.mind_count);
      if (item.run_count) bits.push('主链 ' + item.run_count);
      if (!item.event_count) bits.push('空会话');
      return '<button type="button" class="session-pill' + (active ? ' is-active' : '') +
        '" data-session="' + esc(item.session_id) + '">' +
        '<span class="sp-title">' + esc(clock(item.started_at) + ' · ' + first) + '</span>' +
        '<span class="sp-meta">' + esc(bits.join(' · ') || '无事件') + '</span></button>';
    }).join('');
  }

  function missingChips(detail) {
    const events = (detail && detail.events) || [];
    const has = function (fn) { return events.some(fn); };
    const chips = [];
    if (!has(function (e) { return e.kind === 'eot'; })) chips.push('判停 未记录');
    if (!has(function (e) { return e.kind === 'main' && ['gate_decision', 'gate_open', 'gate_context_only'].indexOf((e.payload || {}).name) >= 0; })) chips.push('判答 未记录');
    if (!has(function (e) { return e.kind === 'main' && ['tools_scores', 'llm_decision'].indexOf((e.payload || {}).name) >= 0; })) chips.push('工具披露 未记录');
    if (!has(function (e) { return e.kind === 'main' && ['au_decided', 'au_executed'].indexOf((e.payload || {}).name) >= 0; })) chips.push('Agent 对话 未记录');
    if (!has(function (e) { return e.kind === 'mind'; })) chips.push('读心 未记录');
    if (!has(function (e) { return e.kind === 'frame'; })) chips.push('画面 未记录');
    return chips;
  }

  function renderMain() {
    const detail = state.data;
    const metaEl = dom.sessionMeta;
    const mount = dom.timelineMount;
    if (!detail) {
      metaEl.hidden = true;
      metaEl.innerHTML = '';
      mount.innerHTML = emptyHTML();
      return;
    }
    const session = detail.session || {};
    const events = detail.events || [];
    const summary = currentSession() || {};
    const utterances = events.filter(function (e) { return e.kind === 'utterance'; });
    const first = utterances[0] && (utterances[0].payload || {}).text;
    const seat = utterances[0] && (utterances[0].payload || {}).seat;
    const chips = missingChips(detail);
    const tags = ['真实记录'];
    if (summary.mind_engine) tags.push('读心：' + engineLabel(summary.mind_engine));
    if (summary.run_count) tags.push('主链 ' + summary.run_count + ' 趟');
    if (summary.frame_count) tags.push('画面 ' + summary.frame_count + ' 帧');
    metaEl.hidden = false;
    metaEl.innerHTML =
      '<div class="sm-top"><div><h1>' +
      esc((seat ? seatOf(seat).label : '') + (first ? '说：「' + oneLine(first, 48) + '」' : '一条内心戏会话')) +
      '</h1><p class="sm-sub">' + esc(day(session.started_at) + ' ' + clock(session.started_at) +
        ' · 会话 ' + (session.session_id || '') + ' · 模型 ' + ((session.meta || {}).model || '未记录')) +
      '</p></div><div class="sm-tags">' + tags.map(function (t) {
        return '<span class="badge neutral">' + esc(t) + '</span>';
      }).join('') + '</div></div>' +
      (chips.length ? '<div class="sm-notes">' + chips.map(function (t) {
        return '<span class="badge muted">' + esc(t) + '</span>';
      }).join('') + '</div>' : '');
    mount.innerHTML = timelineHTML(detail);
  }

  function emptyHTML() {
    return '<div class="empty-board"><div class="empty-card"><h2>还没有可展示的会话</h2>' +
      '<p>这是空态，不是检索无结果。跨会话列表和全文检索都还没建设，所以这里不会出现“搜不到”的假结果。</p>' +
      '<p><a href="/neixinxi">去内心戏完成一轮</a></p></div></div>';
  }

  function timelineHTML(detail) {
    const events = detail.events || [];
    const items = buildItems(events);
    const hasRight = items.some(function (ev) { return ev.side === 'right'; });
    const legend = '<p class="track-legend">左右是两条独立轨道，各按自己的发生时间挂到中轴上，<b>不是左右成对</b>。对侧空着是正常的。</p>';
    const emptyRight = hasRight ? '' :
      '<div class="rail-empty-row"><div></div><div></div><div class="col right"><div class="rail-note"><strong>日志里没有读心记录 · 未记录画面</strong>这条记录没有读心，也没有画面。不会编造读心时间，也不会用示例图代替。</div></div></div>';
    const parts = [];
    items.forEach(function (ev, index) {
      if (index > 0) {
        const dt = ev.at - items[index - 1].at;
        const h = Math.max(10, Math.min(136, Math.round((Number(dt) || 0) / 1000 * 52)));
        const label = dt >= 3000 ? ('间隔约 ' + (dt >= 10000 ? Math.round(dt / 1000) : (dt / 1000).toFixed(1)) + ' 秒') : '';
        parts.push('<div class="time-gap" style="height:' + h + 'px">' + (label ? '<span class="gap-label">' + label + '</span>' : '') + '</div>');
      }
      parts.push(bandHTML(detail, ev));
    });
    return legend +
      '<div class="col-heads" aria-hidden="true">' +
      '<div class="col-label left">对话中枢 · Agent</div>' +
      '<div class="axis-cap">时间线</div>' +
      '<div class="col-label right">读心 · 画面</div></div>' +
      emptyRight +
      '<div class="timeline">' + parts.join('') + '</div>' +
      rawHTML(detail, events);
  }

  function buildItems(events) {
    const runEnds = {};
    const hasName = {};
    const candidates = {};
    events.forEach(function (e) {
      if (e.kind === 'run_end') runEnds[(e.payload || {}).run_id] = e.payload;
      if (e.kind === 'main') {
        const p = e.payload || {};
        (hasName[p.run_id] = hasName[p.run_id] || {})[p.name] = true;
        if (p.name === 'tools_scores') candidates[p.run_id] = p;
      }
    });
    const frameByMind = {};
    events.forEach(function (e) {
      if (e.kind !== 'frame') return;
      const prev = events.find(function (x) { return x.kind === 'mind' && x.seq === e.seq - 1; });
      if (prev) frameByMind[prev.seq] = e;
    });

    const items = [];
    events.forEach(function (e) {
      const p = e.payload || {};
      if (e.kind === 'utterance') {
        items.push({ side: 'axis', kind: 'speech', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, text: p.text, payload: p });
        return;
      }
      if (e.kind === 'eot') {
        items.push({ side: 'left', kind: 'eot', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p });
        return;
      }
      if (e.kind === 'mind') {
        items.push({ side: 'right', kind: 'mind', at: e.at, t_ms: e.t_ms, seq: e.seq,
          payload: p, frame: frameByMind[e.seq] || null });
        return;
      }
      if (e.kind === 'frame') {
        if (!frameByMind[e.seq - 1]) {
          items.push({ side: 'right', kind: 'frame', at: e.at, t_ms: e.t_ms, seq: e.seq, payload: p });
        }
        return;
      }
      if (e.kind === 'run_end') {
        if ((p.missing || []).length) {
          items.push({ side: 'left', kind: 'gap', at: e.at, t_ms: e.t_ms, seq: e.seq, payload: p });
        }
        return;
      }
      if (e.kind !== 'main') return;
      const names = hasName[p.run_id] || {};
      if (p.name === 'gate_decision') {
        items.push({ side: 'left', kind: 'gate', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p, runEnd: runEnds[p.run_id] });
      } else if ((p.name === 'gate_open' || p.name === 'gate_context_only') && !names.gate_decision) {
        items.push({ side: 'left', kind: 'gate', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p, runEnd: runEnds[p.run_id] });
      } else if (p.name === 'llm_decision') {
        items.push({ side: 'left', kind: 'tool', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p, candidates: candidates[p.run_id] });
      } else if (p.name === 'tools_scores' && !names.llm_decision) {
        items.push({ side: 'left', kind: 'tool', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p, candidates: p });
      } else if (p.name === 'au_executed') {
        items.push({ side: 'left', kind: 'agent', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p, runEnd: runEnds[p.run_id] });
      } else if (p.name === 'au_decided' && !names.au_executed) {
        items.push({ side: 'left', kind: 'agent-decided', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p, runEnd: runEnds[p.run_id] });
      } else if (p.name === 'receipt' && !names.au_executed) {
        items.push({ side: 'left', kind: 'receipt', at: e.at, t_ms: e.t_ms, seq: e.seq, seat: p.seat, payload: p });
      }
    });
    items.sort(function (a, b) { return (a.at - b.at) || (a.seq - b.seq); });
    return items;
  }

  function bandHTML(detail, ev) {
    const seat = seatOf(ev.seat);
    const side = ev.side;
    const axisKind = ev.kind === 'speech';
    const nodeCls = axisKind ? ('speech ' + seat.cls) : (side === 'right' ? 'mind' : ('hub ' + seat.cls));
    const left = side === 'left' ? ('<div class="stack">' + eventCard(detail, ev) + '</div>') : '';
    const right = side === 'right' ? ('<div class="stack">' + eventCard(detail, ev) + '</div>') : '';
    const extra = axisKind
      ? '<div class="speech-label">' + esc((seat.label || '人') + '说：「' + oneLine(ev.text, 56) + '」') + '</div>'
      : ((seat.label && side !== 'axis') ? '<div class="seat-chip ' + seat.cls + '">' + esc(seat.label) + '</div>' : '') +
        '<div class="axis-kind">' + esc(kindLabel(ev)) + '</div>';
    return '<article class="band' + (axisKind ? ' is-axis' : '') + '" id="ev-' + esc(safeId(ev.seq)) + '">' +
      '<div class="col left">' + left + '</div>' +
      '<div class="axis"><div class="axis-time">' + esc(clock(ev.at)) + '</div>' +
      '<div class="axis-node ' + nodeCls + '" aria-hidden="true"></div>' + extra + '</div>' +
      '<div class="col right">' + right + '</div></article>';
  }

  function kindLabel(ev) {
    return ({
      eot: '判停', gate: '判答', tool: '工具披露', agent: 'Agent 对话',
      'agent-decided': 'Agent 决定', receipt: '办理回执', gap: '关联缺口',
      mind: '读心', frame: '画面', speech: '原话'
    })[ev.kind] || '';
  }

  function eventCard(detail, ev) {
    if (ev.kind === 'eot') return cardEot(ev);
    if (ev.kind === 'gate') return cardGate(ev);
    if (ev.kind === 'tool') return cardTool(ev);
    if (ev.kind === 'agent' || ev.kind === 'agent-decided') return cardAgent(ev);
    if (ev.kind === 'receipt') return cardReceipt(ev);
    if (ev.kind === 'gap') return cardGap(ev);
    if (ev.kind === 'mind') return cardMind(ev);
    if (ev.kind === 'frame') return cardFrame(ev);
    return '';
  }

  function cardShell(title, badge, body, foldLabel, foldBody, open, missing) {
    return '<section class="card' + (missing ? ' is-missing' : '') + '">' +
      '<div class="card-k"><span class="k">' + esc(title) + '</span>' +
      (badge ? '<span class="badge ' + esc(badge.cls) + '">' + esc(badge.label) + '</span>' : '') +
      '</div>' + body +
      (foldLabel ? foldButton(title, foldLabel, foldBody, open) : '') + '</section>';
  }

  function foldButton(title, label, body, open) {
    const key = state.pendingFoldKey;
    return '<button type="button" class="fold-btn' + (open ? ' is-open' : '') +
      '" data-fold="' + esc(key) + '" aria-expanded="' + (open ? 'true' : 'false') + '">' +
      '<span class="caret" aria-hidden="true"></span>' + esc(label) + '</button>' +
      '<div class="fold"' + (open ? '' : ' hidden') + '>' + body + '</div>';
  }

  function cardEot(ev) {
    const p = ev.payload;
    const label = p.label_cn || ({ complete: '说完', cut: '截断', wait: '没说完' })[p.label] || '未记录';
    const badge = p.label === 'complete' ? { label: '说完', cls: 'ok' }
      : p.label === 'cut' ? { label: '截断', cls: 'warn' }
        : p.label === 'wait' ? { label: '继续等', cls: 'neutral' }
          : { label: '未记录', cls: 'muted' };
    const open = isOpen(ev, 'eot');
    state.pendingFoldKey = foldKey(ev, 'eot');
    const bars = [
      ['说完', p.scores && p.scores.complete, p.label === 'complete'],
      ['切断', p.scores && p.scores.cut, p.label === 'cut'],
      ['继续等', p.scores && p.scores.wait, p.label === 'wait']
    ].map(function (row) {
      return '<div class="bar-row' + (row[2] ? ' is-main' : '') + '"><span>' + row[0] + '</span>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + pct(row[1]) + '%"></div></div>' +
        '<span>' + score(row[1]) + '</span></div>';
    }).join('');
    const kv = '<dl class="kv">' +
      (p.confidence != null ? '<dt>把握</dt><dd>' + score(p.confidence) + '</dd>' : '') +
      (p.margin != null ? '<dt>分差</dt><dd>' + score(p.margin) + '</dd>' : '') +
      (p.latency_ms != null ? '<dt>用时</dt><dd>' + ms(p.latency_ms) + '</dd>' : '') +
      (p.error ? '<dt>错误</dt><dd>' + esc(p.error) + '</dd>' : '') +
      '<dt>来源</dt><dd>' + esc(sourceLabel(p.source)) + '</dd></dl>';
    return cardShell('判停', badge,
      '<p class="card-title">判停结论：' + esc(label) + '</p>' +
      '<p class="card-reason">' + esc(oneLine(p.source_mode === 'text' ? '文字输入发送即收住，不走 ASR 判停' : '判停来源：' + sourceLabel(p.source), 90)) + '</p>',
      '分数与细节', '<div class="bars">' + bars + '</div>' + kv, open, false);
  }

  function cardGate(ev) {
    const p = ev.payload;
    const d = p.data || {};
    const opened = d.route === 'agent';
    const badge = opened ? { label: '开闸', cls: 'teal' } : { label: doorLabel(d.route), cls: 'neutral' };
    const open = isOpen(ev, 'gate');
    state.pendingFoldKey = foldKey(ev, 'gate');
    const accept = d.accept && typeof d.accept === 'object' ? d.accept : null;
    const page = ev.runEnd && ev.runEnd.page_accept;
    const pageText = page ? (page.accept === true ? '要回答' : page.accept === false ? '不回答' : '没回来（' + (page.state || '未知') + '）') : '未记录';
    const kv = '<dl class="kv">' +
      (accept && accept.accept != null ? '<dt>接不接</dt><dd>' + score(accept.accept) + '</dd>' : '') +
      (d.two_route_ms != null ? '<dt>用时</dt><dd>' + ms(d.two_route_ms) + '</dd>' : '') +
      '<dt>页面</dt><dd>' + esc(pageText) + '（只记录，不决定）</dd>' +
      (ev.runEnd && ev.runEnd.duo_session_id ? '<dt>主链</dt><dd><span class="tool-raw">' + esc(ev.runEnd.duo_session_id) + '</span></dd>' : '<dt>主链</dt><dd>未关联到主链会话</dd>') +
      '</dl>';
    return cardShell('判答', badge,
      '<p class="card-title">' + esc(opened ? '交给 Agent 办理' : '只记上下文，不办理') + '</p>' +
      '<p class="card-reason">' + esc(oneLine(d.reason || '主链没有写下原因。', 120)) + '</p>' +
      (page ? '<p class="card-extra">页面那一拍：' + esc(pageText) + '（只记录；是否办理以本卡的主链判答为准）</p>' : ''),
      '分数与细节', kv, open, false);
  }

  function cardTool(ev) {
    const p = ev.payload;
    const d = p.data || {};
    const open = isOpen(ev, 'tool');
    state.pendingFoldKey = foldKey(ev, 'tool');
    if (p.name === 'llm_decision') {
      const decision = d.decision || {};
      const tool = decision.tool || '';
      const kind = decision.kind || '';
      const human = kind === 'tool_call' ? '选「' + toolName(tool) + '」并办理' : (kind === 'ask_user' ? '回问一句' : '结束动作');
      const args = decision.arguments || {};
      const argText = Object.keys(args).length ? shortJson(args, 90) : '无参数';
      const body = '<p class="card-title">工具披露：' + esc(human) + '</p>' +
        '<p class="card-reason">' + esc('主链决定：' + (kind || '未记录') + '；参数：' + argText) + '</p>' +
        (tool ? '<p class="card-extra">工具原名：<span class="tool-raw">' + esc(tool) + '</span></p>' : '');
      const cand = ev.candidates && ev.candidates.data ? ev.candidates.data : {};
      const top = (cand.top || []).slice(0, 6).map(function (item) {
        return '<p class="card-extra">' + esc(toolName(item.name)) + ' · ' + score(item.score) + '</p>';
      }).join('');
      const kv = '<dl class="kv">' +
        (d.llm_ms != null ? '<dt>决策用时</dt><dd>' + ms(d.llm_ms) + '</dd>' : '') +
        '<dt>工具原名</dt><dd><span class="tool-raw">' + esc(tool || '未记录') + '</span></dd>' +
        '<dt>参数</dt><dd>' + esc(shortJson(args, 160)) + '</dd></dl>';
      return cardShell('工具披露', { label: kind === 'tool_call' ? '已选定' : '非动作', cls: kind === 'tool_call' ? 'teal' : 'neutral' },
        body, '候选与原名', (top ? '<p class="card-extra">短名单（按分数）：</p>' + top : '') + kv, open, false);
    }
    const selected = d.selected || [];
    const top = (d.top || []).slice(0, 6).map(function (item) {
      return '<p class="card-extra">' + esc(toolName(item.name)) + ' · ' + score(item.score) + '</p>';
    }).join('');
    const sel = selected.map(function (name) { return toolName(name); }).join('、');
    return cardShell('工具披露', { label: selected.length ? '候选已回' : '未选定', cls: selected.length ? 'neutral' : 'muted' },
      '<p class="card-title">工具候选：' + esc(sel || '没有入选工具') + '</p>' +
      '<p class="card-reason">' + esc(oneLine(selected.length ? '短名单已回来，等主链做一次决策。' : '主链没有给出工具候选。', 90)) + '</p>',
      '候选与原名', (top || '<p class="card-extra">没有候选打分记录。</p>') +
        '<dl class="kv"><dt>入选</dt><dd>' + esc(sel || '未记录') + '</dd></dl>', open, !selected.length);
  }

  function cardAgent(ev) {
    const p = ev.payload;
    const d = p.data || {};
    const seat = seatOf(p.seat);
    const human = p.utterance || '';
    const open = isOpen(ev, 'agent');
    state.pendingFoldKey = foldKey(ev, 'agent');
    const question = (d.arguments || {}).question || '';
    const car = d.spoken || (d.kind === 'ask_user' ? question : '') || '';
    const receipt = d.receipt || (ev.runEnd && ev.runEnd.receipt) || '';
    const isDecided = ev.kind === 'agent-decided';
    const humanBubble = '<div class="bubble human ' + seat.cls + '"><span class="who">' + esc(seat.label) + '说</span><p class="txt">' + esc(human || '（这一趟没有原话记录）') + '</p></div>';
    const carBubble = car
      ? '<div class="bubble car"><span class="who">' + (d.kind === 'ask_user' ? '车问了一句' : '车说') + '</span><p class="txt">' + esc(car) + '</p></div>'
      : '<div class="bubble none"><span class="who">车</span><p class="txt">' + esc(isDecided ? '主链已决定，但还没给出对人说的话。' : '主链这一趟没有给出对人说的话；中间保持空，不编台词。') + '</p></div>';
    const badge = car ? { label: d.kind === 'ask_user' ? '回问' : '有回执', cls: 'ok' } : { label: '未说话', cls: 'muted' };
    const kv = '<dl class="kv">' +
      '<dt>动作</dt><dd>' + esc(d.kind || '未记录') + '</dd>' +
      (d.tool ? '<dt>工具</dt><dd><span class="tool-raw">' + esc(d.tool) + '</span></dd>' : '') +
      (receipt ? '<dt>办理回执</dt><dd>' + esc(receipt) + '</dd>' : '<dt>办理回执</dt><dd>未记录</dd>') +
      (ev.runEnd && ev.runEnd.duo_session_id ? '<dt>主链</dt><dd><span class="tool-raw">' + esc(ev.runEnd.duo_session_id) + '</span></dd>' : '<dt>主链</dt><dd>未关联到主链会话</dd>') +
      '</dl>';
    return cardShell('Agent 对话', badge,
      '<div class="talk">' + humanBubble + carBubble + '</div>',
      '办理回执与关联', kv, open, !car);
  }

  function cardReceipt(ev) {
    const p = ev.payload;
    return cardShell('办理回执', { label: '有回执', cls: 'teal' },
      '<p class="card-title">主链只给了办理回执</p>' +
      '<p class="card-reason">' + esc(oneLine((p.data || {}).text || '没有可读的回执文本。', 120)) + '</p>',
      '', '', false, false);
  }

  function cardGap(ev) {
    const p = ev.payload;
    const missing = p.missing || [];
    const open = isOpen(ev, 'gap');
    state.pendingFoldKey = foldKey(ev, 'gap');
    const kv = '<dl class="kv">' +
      '<dt>缺口</dt><dd>' + esc(missing.join('、') || '未记录') + '</dd>' +
      '<dt>已回事件</dt><dd>' + esc((p.event_names || []).join(' → ') || '无') + '</dd>' +
      (p.duo_session_id ? '<dt>主链</dt><dd><span class="tool-raw">' + esc(p.duo_session_id) + '</span></dd>' : '') +
      (p.error ? '<dt>错误</dt><dd>' + esc(p.error) + '</dd>' : '') +
      '</dl>';
    return cardShell('关联缺口', { label: '未返回', cls: 'muted' },
      '<p class="card-title">主链这一趟没有返回：' + esc(missing.join('、') || '记录') + '</p>' +
      '<p class="card-reason">这一趟没有对应事件，照实标缺失，不补造结论。</p>',
      '这一趟收到的事件', kv, open, true);
  }

  function cardMind(ev) {
    const m = ev.payload;
    const open = isOpen(ev, 'mind');
    state.pendingFoldKey = foldKey(ev, 'mind');
    const chips = [
      m.intent ? chip('意图', m.intent) : '',
      m.target ? chip('对象', m.target) : '',
      m.topic ? chip('话题', m.topic) : ''
    ].join('');
    const kv = '<dl class="kv">' +
      (m.engine || m.source ? '<dt>引擎</dt><dd>' + esc(engineLabel(m.engine || m.source)) + '</dd>' : '') +
      (m.source ? '<dt>来源</dt><dd>' + esc(m.source) + '</dd>' : '') +
      (m.trigger ? '<dt>触发</dt><dd>' + esc(m.trigger) + '</dd>' : '') +
      (m.basis ? '<dt>依据</dt><dd>' + esc(m.basis) + '</dd>' : '') +
      (m.cues && m.cues.length ? '<dt>线索</dt><dd>' + esc(m.cues.join('、')) + '</dd>' : '') +
      (m.confidence != null ? '<dt>把握</dt><dd>' + score(m.confidence) + '</dd>' : '') +
      (m.revision != null ? '<dt>第几次</dt><dd>第 ' + esc(m.revision) + ' 次</dd>' : '') +
      (m.seats && m.seats.length ? '<dt>在场</dt><dd>' + esc(m.seats.map(function (s) { return (s.label || s.seat || '') + '：' + (s.text || ''); }).join('；')) + '</dd>' : '') +
      '</dl>';
    const revision = Number(m.revision || 0);
    const badge = revision > 1 ? { label: '读心更新', cls: 'teal' } : { label: '有输出', cls: 'teal' };
    const frame = ev.frame;
    const shot = frame
      ? '<section class="card shot"><div class="shot-frame"><img src="' + esc(frame.payload.frame_ref) + '" alt="日志中保存的画面"/>' +
        '<span class="shot-badge">' + esc(frame.payload.label || '画面') + '</span></div>' +
        '<div class="shot-cap"><strong>' + esc(frame.payload.label || '画面') + '</strong> · 这一帧与这份读心同一次记录；来源照实标注。</div></section>'
      : '<p class="card-extra missing-line">这一拍未记录画面（不拿示例图代替）。</p>';
    return cardShell(revision > 1 ? '读心更新' : '读心', badge,
      '<p class="mind-lead">' + esc(m.summary || '没有读心旁白。') + '</p>' +
      (chips ? '<div class="mind-meta">' + chips + '</div>' : '') +
      '<p class="card-reason">' + esc(engineLabel(m.engine || m.source) + ' · ' + (m.source || '来源未记录')) + '</p>' +
      shot,
      '依据与细节', kv, open, !m.summary);
  }

  function cardFrame(ev) {
    const p = ev.payload;
    return '<section class="card shot"><div class="shot-frame"><img src="' + esc(p.frame_ref) + '" alt="日志中保存的画面"/>' +
      '<span class="shot-badge">' + esc(p.label || '画面') + '</span></div>' +
      '<div class="shot-cap"><strong>' + esc(p.label || '画面') + '</strong> · 这一帧没有配对的读心记录，单独挂在自己的时间上。</div></section>';
  }

  function chip(k, v) {
    return '<span class="mind-chip"><b>' + esc(k) + '</b>' + esc(v) + '</span>';
  }

  function rawHTML(detail, events) {
    if (!events.length) return '';
    const fake = { seq: 'raw' };
    const open = isOpen(fake, 'raw');
    state.pendingFoldKey = foldKey(fake, 'raw');
    const rows = events.map(function (e) {
      const p = e.payload || {};
      const name = e.kind === 'main' ? p.name : (e.kind === 'run_end' ? 'run_end' : e.kind);
      return '<tr><td class="ev-t">' + esc(clock(e.at)) + '</td><td class="ev-n">' + esc(name || '—') +
        '</td><td class="ev-d">' + esc(shortJson(p, 150)) + '</td></tr>';
    }).join('');
    return '<div class="raw-row"><section class="card raw-wrap">' +
      '<div class="card-k"><span class="k">原始事件</span></div>' +
      '<button type="button" class="fold-btn' + (open ? ' is-open' : '') + '" data-fold="' + esc(foldKey(fake, 'raw')) +
      '" aria-expanded="' + (open ? 'true' : 'false') + '"><span class="caret" aria-hidden="true"></span>' +
      (open ? '收起原始事件' : '展开原始事件') + '</button>' +
      '<div class="fold"' + (open ? '' : ' hidden') + '"><p class="card-extra">下面是这次会话落盘的原始事件，按发生顺序排列；不是示例回放。</p>' +
      '<table class="raw-table"><thead><tr><th>时间</th><th>事件</th><th>摘要</th></tr></thead><tbody>' +
      rows + '</tbody></table></div></section></div>';
  }

  function renderDrawerSource() {
    const el = document.getElementById('drawerSource');
    if (!el) return;
    if (!state.data) {
      el.textContent = state.sessions.length ? '正在读取选中的会话…' : '当前没有可读的落盘会话。';
      return;
    }
    const summary = currentSession() || {};
    el.textContent = '当前读取的是真实落盘会话 ' + (summary.session_id || '') +
      '，共 ' + (summary.event_count || 0) + ' 条事件；读心 ' + (summary.mind_count || 0) +
      ' 次，画面 ' + (summary.frame_count || 0) + ' 帧，主链 ' + (summary.run_count || 0) +
      ' 趟。没有读心或画面时只标明缺失，没有编造读心时间或示例图。';
  }

  function bind() {
    dom.refreshBtn.addEventListener('click', function () { loadSessions(true); });
    dom.sessionBar.addEventListener('click', function (e) {
      const btn = e.target.closest('[data-session]');
      if (!btn) return;
      loadSession(btn.getAttribute('data-session'));
    });
    document.body.addEventListener('click', function (e) {
      const btn = e.target.closest('[data-fold]');
      if (!btn) return;
      const key = btn.getAttribute('data-fold');
      if (state.openFolds.has(key)) state.openFolds.delete(key);
      else state.openFolds.add(key);
      if (state.data) {
        const y = window.scrollY;
        dom.timelineMount.innerHTML = timelineHTML(state.data);
        window.scrollTo(0, y);
      }
    });
    dom.drawerBtn.addEventListener('click', openDrawer);
    dom.drawerClose.addEventListener('click', closeDrawer);
    dom.drawerOverlay.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && dom.drawer.classList.contains('is-open')) closeDrawer();
    });
  }

  function openDrawer() {
    dom.lastFocus = document.activeElement;
    dom.drawer.classList.add('is-open');
    dom.drawer.setAttribute('aria-hidden', 'false');
    dom.drawerOverlay.hidden = false;
    dom.drawerClose.focus();
  }
  function closeDrawer() {
    dom.drawer.classList.remove('is-open');
    dom.drawer.setAttribute('aria-hidden', 'true');
    dom.drawerOverlay.hidden = true;
    if (dom.lastFocus && dom.lastFocus.focus) dom.lastFocus.focus();
  }

  function init() {
    dom.truthBanner = document.getElementById('truthBanner');
    dom.sessionBar = document.getElementById('sessionBar');
    dom.sessionMeta = document.getElementById('sessionMeta');
    dom.timelineMount = document.getElementById('timelineMount');
    dom.drawer = document.getElementById('drawer');
    dom.drawerOverlay = document.getElementById('drawerOverlay');
    dom.drawerBtn = document.getElementById('drawerBtn');
    dom.drawerClose = document.getElementById('drawerClose');
    dom.refreshBtn = document.getElementById('refreshBtn');
    bind();
    let wanted = null;
    try { wanted = new URLSearchParams(location.search).get('session'); } catch (e) {}
    if (wanted) {
      state.selectedId = wanted;
      loadSessions(true);
    } else {
      loadSessions(false);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
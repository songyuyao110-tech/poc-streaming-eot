/* 开麦路由：选了哪支就必须开哪支（内心戏页 / 对话流页共用；纯函数，node 里也能测）。
 *
 * 为什么只看设备名不够：2026-11-12 用户实测，Mac 上同时开蓝牙耳夹 + USB 有线两支麦，
 * 浏览器表面上开了两支（回报的设备名也可能是两支），两路实际拿到的是同一份声音。
 * 设备名 / deviceId 都是浏览器自己报的，不能当证据；「是不是两支」由服务端按每席实际收到的
 * PCM 电平走势判（见 ui/server.py 的 mic_route_verdict / MicRouteGuard）。
 * 本文件只管浏览器这一层能当场定死的事：
 *   1) 两席都必须有真实设备号（空设备号 = 会落到系统默认，绝不开）；
 *   2) 两席不能是同一支麦（同一支开不出两路声音）；
 *   3) 浏览器回报的实际设备必须就是选的那支（回报为空 = 这个浏览器不报，按不可判，交给服务端按声音判）；
 *   4) 页面怎么把「真在用哪支 / 两路是不是真的分开」写清楚。
 */
(function (root) {
  'use strict';

  var SEAT_CN = {D: '主驾', P: '副驾'};

  function text(value) {
    return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  }
  function seatName(seat) { return SEAT_CN[seat] || ('席 ' + seat); }
  // 设备名比对：先归一空白（浏览器回报的同一支麦，名字常差一个前导空格 / 前缀），
  // 再按「完全相同或互相包含」认成同一支；名字都没有就不比（交给服务端按声音判）。
  function sameLabel(left, right) {
    var a = text(left), b = text(right);
    if (!a || !b) return false;
    return a === b || a.indexOf(b) >= 0 || b.indexOf(a) >= 0;
  }

  // 开麦前：两席的「要哪支」定死了没有。空设备号会落到系统默认那支 —— 正是这次要修的 bug。
  function plan(selected) {
    var seats = Object.keys(selected || {});
    for (var i = 0; i < seats.length; i += 1) {
      var seat = seats[i];
      var item = selected[seat] || {};
      if (!text(item.deviceId)) {
        return {ok: false, status: 'missing_device_id', seat: seat,
                message: seatName(seat) + '还没有拿到真实设备号（浏览器没给设备权限或设备没就绪）：'
                         + '先授权 / 刷新设备、按真名选一次再开始，不按默认开'};
      }
    }
    if (seats.length === 2 && text(selected.D.deviceId) === text(selected.P.deviceId)) {
      return {ok: false, status: 'same_device', seat: 'D',
              message: '主驾和副驾选了同一支麦（' + (text(selected.D.label) || '同一支')
                       + '）：同一支开不出两路声音，换一支再开始'};
    }
    return {ok: true, status: 'ok', seats: seats};
  }

  // 浏览器到底给的是不是要的那支：`getSettings().deviceId` 为空 = 这个浏览器不报，
  // 按「不可判」处理（不冒充一致，也不冒充不一致；真正是不是两支由服务端按声音判）。
  function grant(requested, settings) {
    var want = text((requested || {}).deviceId);
    var mine = settings || {};
    var actual = text(mine.deviceId);
    var label = text(mine.label) || text((requested || {}).label);
    if (!want) {
      return {ok: false, known: false, status: 'missing_device_id', match: null,
              actualDeviceId: '', label: label, message: '这一席没有真实设备号，不按默认开麦'};
    }
    if (!actual) return {ok: false, known: false, status: 'unknown', match: null,
                         actualDeviceId: '', label: label};
    if (actual !== want) {
      // 设备号对不上：只有浏览器报的**设备名**也明显不是所选那支，才当场算没开成。
      // 名字一样（或浏览器不报名字 / 只回报另一套号）时设备号回声不足以定案，按不可判处理，
      // 交给服务端按两路实际声音判——绝不拿回声冒充一致，也不拿回声冒充不一致。
      var selectedLabel = text((requested || {}).label);
      var actualLabel = text(mine.label);
      if (actualLabel && selectedLabel && !sameLabel(actualLabel, selectedLabel)) {
        return {ok: false, known: true, status: 'device_mismatch', match: false,
                actualDeviceId: actual, label: label,
                message: '浏览器给的不是所选这支（选的是「' + selectedLabel
                         + '」，实际开的是「' + label + '」）：这一轮不算开成，换设备或重新开始'};
      }
      return {ok: false, known: false, status: 'unknown', match: null,
              actualDeviceId: actual, label: label};
    }
    return {ok: true, known: true, status: 'ok', match: true, actualDeviceId: actual, label: label};
  }

  var ROUTE_CN = {
    pending: '两路已按所选打开，正在确认是不是真的两支（还没听到能分辨的声音）',
    ok: '两路已确认分开：各自是所选那支，两路电平走势对不上',
    same_source: '两路其实是同一份声音（浏览器把两路开成了同一支麦），这一轮没开成两支',
    device_mismatch: '浏览器给的不是所选那支麦，这一轮没开成两支'
  };

  function statusText(status) { return ROUTE_CN[status] || ''; }

  // 这一席「真正在用的是哪支」：与服务端判定合起来写成一行（页面上必须和所选对得上）。
  function seatText(seat, info) {
    var body = info || {};
    var actual = text(body.actualLabel) || text(body.selectedLabel) || '未选设备';
    var selected = text(body.selectedLabel);
    var mismatch = selected && text(body.actualLabel) && selected !== text(body.actualLabel);
    var parts = [seatName(seat) + ' 真在用：' + actual];
    if (mismatch) parts.push('（选的是「' + selected + '」）');
    if (body.status === 'ok') parts.push('· 两路已确认分开');
    else if (body.status === 'same_source') parts.push('· 两路是同一份声音');
    else if (body.status === 'device_mismatch') parts.push('· 与所选不一致');
    else if (body.status === 'pending') parts.push('· 确认中');
    return parts.join(' ');
  }

  var api = {plan: plan, grant: grant, sameLabel: sameLabel, statusText: statusText,
             seatText: seatText, seatName: seatName};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MicRoute = api;
})(typeof window !== 'undefined' ? window : globalThis);

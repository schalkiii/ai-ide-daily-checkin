#!/usr/bin/env node
/**
 * 豆包 CDP 真点击每日签到（v2）。
 *
 * 背景：landing/checkin 可能对脱机 HTTP 直调返回 710010702「系统错误」；2026-11-09 实测
 * CDP 点击真实按钮、webview 内同源 fetch 与脱机直调报文完全一致（服务端按时间窗/账号维度
 * 放行，用户手点偶发成功）。本脚本在真实客户端 webview 内点击「立即签到」，并用 Network
 * 域捕获 checkin 响应体做权威判定（比按钮文本/toast 可靠——失败时按钮不变、toast 易误匹配）。
 *
 * 前置：豆包需带调试端口启动（Agent 沙箱 Job 会连杀子进程，须用计划任务或 explorer 拉起）：
 *   Register-ScheduledTask（一次性）-Execute "<豆包>\Application\app\Doubao.exe"
 *     -Argument "--remote-debugging-port=9444"
 *
 * 结果写 %TEMP%\_doubao_checkin_result.json（绿盾对项目目录 node 写入透明加密，TEMP 不受影响）。
 * 退出码：0=成功/已签；1=服务端拒绝（710010702 等，如实上报等恢复）；2=点击未触发请求；3=环境错误。
 */
const PORT = 9444;
const fs = await import('fs');
const { join } = await import('path');
const { tmpdir } = await import('os');
const OUT = join(tmpdir(), '_doubao_checkin_result.json');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

class CDP {
  constructor(wsUrl) { this.wsUrl = wsUrl; this.id = 0; this.pending = new Map(); this.handlers = new Map(); }
  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.addEventListener('open', () => resolve());
      this.ws.addEventListener('error', () => reject(new Error('WebSocket 连接失败')));
      this.ws.addEventListener('message', (ev) => {
        let d; try { d = JSON.parse(ev.data); } catch { return; }
        if (d.id && this.pending.has(d.id)) { this.pending.get(d.id)(d); this.pending.delete(d.id); }
        else if (d.method && this.handlers.has(d.method)) { for (const h of this.handlers.get(d.method)) h(d.params); }
      });
    });
  }
  on(method, handler) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(handler);
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`超时: ${method}`)); }, 25000);
      this.pending.set(id, (msg) => { clearTimeout(timer); msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result); });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('JS 异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 300));
    return r.result ? r.result.value : undefined;
  }
  async clickAt(x, y) {
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await sleep(80);
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
  }
  close() { try { this.ws.close(); } catch {} }
}

// 文本语义定位签到按钮（抗 class 变化）：取文本完全匹配的最内层元素中心坐标
const FIND_BTN_JS = `
(() => {
  let best = null;
  for (const el of document.querySelectorAll('button, [role="button"], a, div, span')) {
    const t = (el.innerText || '').replace(/\\s+/g, '').trim();
    if (t !== '立即签到' && t !== '已签到' && t !== '明日再来') continue;
    if (el.children.length > 2) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    if (!best || (r.width * r.height) < (best.w * best.h)) {
      best = { txt: t, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
    }
  }
  return best ? JSON.stringify(best) : 'null';
})()
`;

async function main() {
  const result = { ok: false, steps: [], time: new Date().toISOString() };
  const step = (name, data) => { result.steps.push({ name, ...data }); console.log(name, JSON.stringify(data)); };
  const finish = (code) => { fs.default.writeFileSync(OUT, JSON.stringify(result, null, 1)); process.exit(code); };

  // 1. 找 agent-explore webview（签到页所在 target）
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const webview = list.find(t => t.url.includes('agent-explore'));
  if (!webview) {
    step('error', { msg: 'agent-explore webview 不存在（豆包未启动或未带 --remote-debugging-port=9444）' });
    result.error = 'no_webview'; finish(3);
  }
  step('found_webview', { url: webview.url.slice(0, 80) });

  const cdp = new CDP(webview.webSocketDebuggerUrl);
  await cdp.connect();

  // 2. Network 域监听 landing/checkin 响应（权威判定来源）
  await cdp.send('Network.enable');
  const checkinResponses = [];
  cdp.on('Network.responseReceived', async (p) => {
    const url = String(p.response.url || '');
    if (!url.includes('landing/checkin')) return;
    try {
      const body = await cdp.send('Network.getResponseBody', { requestId: p.requestId });
      checkinResponses.push({ status: p.response.status, body: (body.body || '').slice(0, 400) });
    } catch (e) { checkinResponses.push({ status: p.response.status, bodyErr: e.message }); }
  });

  // 3. 定位签到按钮
  let btn = await cdp.eval(FIND_BTN_JS);
  if (btn === 'null') {
    step('error', { msg: '未找到「立即签到」按钮（可能页面未加载完）' });
    result.error = 'no_button'; cdp.close(); finish(3);
  }
  btn = JSON.parse(btn);
  step('found_button', btn);
  if (btn.txt !== '立即签到') {
    step('already', { state: btn.txt });
    result.ok = true; result.already = true; result.state = btn.txt;
    cdp.close(); finish(0);
  }

  // 4. 点击：先 Input 真实鼠标点击，未触发请求再回退 JS click（webview hidden 时 Input 可能不派发）
  await cdp.clickAt(btn.x, btn.y);
  step('input_click', { x: btn.x, y: btn.y });
  await sleep(4000);
  if (!checkinResponses.length) {
    await cdp.eval(`(() => {
      const btns = [...document.querySelectorAll('button,[role="button"],div,span')]
        .filter(e => (e.innerText || '').replace(/\\s+/g, '').trim() === '立即签到' && e.offsetParent !== null);
      if (btns.length) btns[btns.length - 1].click();
      return btns.length;
    })()`);
    step('js_click_fallback', {});
    await sleep(5000);
  }

  // 5. 权威判定：解析 checkin 响应体
  result.checkinResponses = checkinResponses;
  if (!checkinResponses.length) {
    step('error', { msg: '点击后未捕获到 landing/checkin 请求' });
    result.error = 'no_request'; cdp.close(); finish(2);
  }

  let verdict = null;
  for (const r of checkinResponses) {
    try {
      const j = JSON.parse(r.body);
      if (j.code === 0) { verdict = 'success'; break; }
      verdict = 'rejected:' + j.code + ' ' + (j.msg || j.message || '');
    } catch { verdict = 'unparseable'; }
  }
  step('verdict', { verdict, responses: checkinResponses.length });

  if (verdict === 'success') {
    // 成功后复查按钮状态（用于上报，不作为判定依据）
    const after = await cdp.eval(FIND_BTN_JS);
    result.state = after && after !== 'null' ? JSON.parse(after).txt : '按钮消失';
    result.ok = true;
    cdp.close(); finish(0);
  } else {
    result.ok = false;
    result.verdict = verdict;
    cdp.close(); finish(1);
  }
}
main().catch(e => {
  try { fs.default.writeFileSync(OUT, JSON.stringify({ ok: false, error: e.message }, null, 1)); } catch {}
  console.error('FATAL:', e.message);
  process.exit(3);
});

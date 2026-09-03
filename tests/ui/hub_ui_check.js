/*
 * tests/ui/hub_ui_check.js — execute the hub page and assert the status logic.
 *
 * The bug this exists for: pressing Stop made a card read
 * "⚠ CRASHED (exit null)". The server no longer reports a stop as a crash, and
 * the card no longer renders a missing exit code as the word "null" — but only a
 * DOM can check that the rendering is right.
 *
 * Reads a PRE-RENDERED page (Jinja already expanded) from /tmp/hub_rendered.html
 * or the path in HUB_HTML, because jsdom cannot execute template tags.
 *
 * Run:  node hub_ui_check.js      (NODE_PATH must find jsdom)
 */
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const FILE = process.env.HUB_HTML || '/tmp/hub_rendered.html';
const html = fs.readFileSync(FILE, 'utf8');

const errs = [], T = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errs.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errs.push('console.error: ' + a.join(' ')));
vc.on('warn', () => {}); vc.on('log', () => {});

const APPS = ['calibration','reduction','average','background','quality',
              'analysis','reactor','analyzer','assistant'];
function frame(over) {
  const apps = {};
  for (const id of APPS)
    apps[id] = {running:false, healthy:false, port:5000, pid:null,
                summary:null, crashed:null};
  Object.assign(apps, over || {});
  return {apps, project_root:'/data/Auto_Run', ws_clients:0,
          event_bus:true, disk_free_gb:412.0, hub_error:null};
}

function stub(w) {
  w.fetch = async () => ({ok:true, status:200, json: async () => ({ok:true})});
  class ES {
    constructor(){ ES.last = this; setTimeout(() => this.onopen && this.onopen(), 0); }
    close(){}
    emit(d){ this.onmessage && this.onmessage({data: JSON.stringify(d)}); }
    fail(){ this.onerror && this.onerror(); }
  }
  w.EventSource = ES;
  w.WebSocket = class { constructor(){ this.readyState = 0; } close(){} send(){} };
  w.__ES = ES;
}

const dom = new JSDOM(html, {runScripts:'dangerously', pretendToBeVisual:true,
                             virtualConsole:vc, url:'http://localhost:5000/',
                             beforeParse:stub});
const w = dom.window;

(async () => {
  await new Promise(r => setTimeout(r, 200));
  const $ = id => w.document.getElementById(id);
  const ok = (c, m) => { if (!c) errs.push('ASSERT: ' + m); else T.push(m); };
  const ES = w.__ES;
  ok(!!ES.last, 'status stream opened');

  const lbl = id => ($(`lbl-${id}`) || {}).textContent || '';
  const box = id => $(`crash-${id}`);

  // 1. a plain stopped app
  ES.last.emit(frame());
  await new Promise(r => setTimeout(r, 60));
  ok(lbl('reduction') === 'Stopped', `stopped card reads "Stopped", got "${lbl('reduction')}"`);
  ok(!box('reduction'), 'no crash box on a stopped card');

  // 2. running and healthy
  ES.last.emit(frame({reduction:{running:true, healthy:true, port:5001, pid:42,
                                 summary:null, crashed:null}}));
  await new Promise(r => setTimeout(r, 60));
  ok(/running/i.test(lbl('reduction')), `healthy card, got "${lbl('reduction')}"`);

  // 3. THE REGRESSION: a crash with no exit code must never render "null"
  ES.last.emit(frame({reduction:{running:false, healthy:false, port:5001, pid:null,
      summary:null, crashed:{exit_code:null, reason:'reason unknown', at:1, tail:[]}}}));
  await new Promise(r => setTimeout(r, 60));
  ok(/CRASHED/.test(lbl('reduction')), 'crash is announced');
  ok(!/null/.test(lbl('reduction')),
     `"null" leaked into the card: "${lbl('reduction')}"`);
  ok(/reason unknown/.test(lbl('reduction')), 'the reason is shown');

  // 4. a real crash shows the reason and the log tail inline
  ES.last.emit(frame({reduction:{running:false, healthy:false, port:5001, pid:null,
      summary:null, crashed:{exit_code:1, reason:'exit 1', at:1,
      tail:['Traceback (most recent call last):',
            "  File \"reduction/app.py\", line 12",
            "FileNotFoundError: poni/atT_SAXS.poni"]}}}));
  await new Promise(r => setTimeout(r, 60));
  ok(/exit 1/.test(lbl('reduction')), `reason shown, got "${lbl('reduction')}"`);
  ok(!!box('reduction'), 'the log tail box was created');
  ok(/atT_SAXS\.poni/.test(box('reduction').textContent), 'the tail shows the cause');
  ok(/logs\/reduction\.log/.test(box('reduction').textContent),
     'the tail names the log file');

  // 5. signal deaths read as a signal, not a negative number
  ES.last.emit(frame({quality:{running:false, healthy:false, port:5006, pid:null,
      summary:null, crashed:{exit_code:-9, reason:'killed by SIGKILL', at:1, tail:[]}}}));
  await new Promise(r => setTimeout(r, 60));
  ok(/killed by SIGKILL/.test(lbl('quality')), `got "${lbl('quality')}"`);
  ok(!/-9/.test(lbl('quality')), 'a raw negative exit code was shown');

  // 6. recovery clears both the badge and the box
  ES.last.emit(frame({reduction:{running:true, healthy:true, port:5001, pid:99,
                                 summary:null, crashed:null}}));
  await new Promise(r => setTimeout(r, 60));
  ok(!/CRASHED/.test(lbl('reduction')), 'crash badge cleared after a restart');
  ok(!box('reduction'), 'crash box removed after a restart');

  // 7. a failed hub tick is reported, and does not wipe the cards
  ES.last.emit(frame({}));
  await new Promise(r => setTimeout(r, 40));
  ES.last.onmessage({data: JSON.stringify(
      {apps:{}, project_root:'/data/Auto_Run', hub_error:'RuntimeError: boom'})});
  await new Promise(r => setTimeout(r, 40));
  ok($('hubLink') && $('hubLink').style.display === 'block',
     'a failed status tick raises the banner');
  ok(/boom/.test($('hubLink').textContent), 'the banner names the error');

  // 8. a dropped stream is reported rather than silently freezing the page
  ES.last.emit(frame());
  await new Promise(r => setTimeout(r, 40));
  ok($('hubLink').style.display === 'none', 'banner clears on a good frame');
  ES.last.fail();
  await new Promise(r => setTimeout(r, 60));
  ok($('hubLink').style.display === 'block', 'a dropped stream raises the banner');
  ok(/reconnecting/.test($('hubLink').textContent), 'reconnect is announced');

  console.log('\n' + T.length + ' checks passed');
  if (errs.length) {
    console.log('\nFAILURES:'); errs.forEach(e => console.log(' ✗ ' + e));
    process.exit(1);
  }
  console.log('no runtime errors');
  w.close(); process.exit(0);
})();

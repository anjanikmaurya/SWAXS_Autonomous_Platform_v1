/*
 * tests/ui/test_stage_banner.cjs — Auto Watch's big stage banner.
 *
 * The bug this exists for: the banner read FITTING for the whole 20-minute
 * flush of the next condition. `pipeline.current_stage` is the last stage to
 * EMIT an event — a historical marker — so after a fit completed it stayed
 * 'fit' while the reactor moved on to flushing. Nothing was stuck; the label
 * was describing the past.
 *
 * The precedence under test, in order:
 *   1. a stall — the only thing the operator must act on
 *   2. the reactor's phase, whenever it has one — it is what is happening NOW,
 *      and it drives the loop. Concurrent pipeline work moves to the sub-line.
 *   3. a pipeline stage that is genuinely live (running/waiting), never one
 *      that is merely 'done'
 *   4. IDLE
 *
 * Run: node tests/ui/test_stage_banner.cjs
 * The banner logic is extracted from watchdog/templates/index.html and run
 * against a DOM stub, so there is no browser or jsdom dependency.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..', '..');
const HTML = fs.readFileSync(
  path.join(ROOT, 'watchdog', 'templates', 'index.html'), 'utf8');

// ── extract just the banner logic and its lookup tables ────────────────────
const script = HTML.split(/^<script>$/m)[1].split(/^<\/script>$/m)[0];
const BLOCKS = ['const STAGE_BANNER', 'const REACTOR_PHASE_BLURB',
                'const PIPE_VERB', 'function esc(',
                'function pipelineBusyNote(', 'function updateStageBanner('];
const src = BLOCKS.map(name => {
  const i = script.indexOf(name);
  assert.ok(i >= 0, `watchdog/templates/index.html no longer defines ${name}`);
  let j = script.length;
  for (const nxt of ['\nconst ', '\nfunction ', '\nlet ', '\nasync function ']) {
    const k = script.indexOf(nxt, i + name.length);
    if (k !== -1) j = Math.min(j, k);
  }
  return script.slice(i, j);
}).join('\n');

// ── DOM stub ───────────────────────────────────────────────────────────────
function harness() {
  const els = {};
  const el = id => els[id] || (els[id] = {
    id, textContent: '', innerHTML: '', attrs: {},
    classList: {toggle() {}},
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return (k in this.attrs) ? this.attrs[k] : null; },
  });
  const steps = ['collect', 'reduce', 'average', 'subtract', 'fit'].map(s => {
    const e = el('step-' + s);
    e.attrs['data-stage'] = s;
    e.classList = {toggle: (_c, on) => { e._cur = !!on; }};
    return e;
  });
  const document = {
    getElementById: el,
    querySelector: () => null,
    querySelectorAll: sel => (sel === '.step' ? steps : []),
  };
  const api = new Function('document', `
    let _metrics = null;
    ${src}
    return { run(m, loop) { _metrics = m; updateStageBanner(loop); } };`)(document);
  return {
    show(metrics, loop) {
      steps.forEach(s => { s._cur = false; });
      api.run(metrics, loop);
      const cur = steps.find(s => s._cur);
      return {
        name: el('stage-banner-name').textContent,
        sub: el('stage-banner-sub').innerHTML,
        state: el('stage-banner').getAttribute('data-state'),
        halo: cur ? cur.attrs['data-stage'] : null,
      };
    },
  };
}

const FLUSH = {phase: 'flushing', phase_label: 'FLUSHING',
               phase_detail: '14m 05s left · pump P3'};
let passed = 0;
function check(name, fn) {
  try { fn(harness()); passed++; console.log('  ok  ' + name); }
  catch (e) { console.error('FAIL  ' + name + '\n      ' + e.message); process.exitCode = 1; }
}

console.log('stage banner precedence');

check('a finished fit does not hold the banner through the flush', h => {
  const r = h.show({pipeline: {current_stage: 'fit'}},
                   {reactor: FLUSH, fit: {state: 'done', detail: '9.8 nm'}});
  assert.strictEqual(r.name, 'FLUSHING',
    'THE BUG: current_stage is still "fit" but the reactor is flushing');
  assert.strictEqual(r.state, 'waiting', 'a flush is waiting, not running or late');
  assert.strictEqual(r.halo, 'collect', 'the reactor circle is the one to halo');
});

check('concurrent pipeline work is reported on the sub-line', h => {
  const r = h.show({pipeline: {current_stage: 'fit'}},
    {reactor: FLUSH,
     average: {state: 'waiting', detail: '7/10 frames', recipe_id: 'Run11_r003'}});
  assert.strictEqual(r.name, 'FLUSHING');
  assert.ok(/meanwhile/.test(r.sub), 'the pipeline work must not be lost: ' + r.sub);
  assert.ok(/waiting to average/.test(r.sub), 'grammar: ' + r.sub);
  assert.ok(/Run11_r003/.test(r.sub));
});

check('a stall outranks the reactor phase', h => {
  const r = h.show({pipeline: {current_stage: 'average'}},
    {reactor: FLUSH, overdue: {stage: 'average', seconds: 1900}});
  assert.strictEqual(r.name, 'STALLED');
  assert.strictEqual(r.state, 'stalled');
  assert.ok(/31 min/.test(r.sub), r.sub);
});

check('a live pipeline stage shows when the reactor is idle', h => {
  const r = h.show({pipeline: {current_stage: 'fit'}},
    {reactor: {phase: 'idle'}, subtract: {state: 'running', recipe_id: 'Run11_r003'}});
  assert.strictEqual(r.name, 'SUBTRACTING');
  assert.strictEqual(r.state, 'running');
  assert.strictEqual(r.halo, 'subtract');
});

check('a merely-done stage reads IDLE, not its own name', h => {
  const r = h.show({pipeline: {current_stage: 'fit'}},
                   {reactor: {phase: 'idle'}, fit: {state: 'done'}});
  assert.strictEqual(r.name, 'IDLE',
    'nothing is running — the last thing to finish must not look current');
  assert.strictEqual(r.halo, null, 'nothing to halo when nothing is live');
});

check('a ready reactor says so rather than just IDLE', h => {
  const r = h.show({pipeline: {current_stage: 'fit'}},
    {reactor: {phase: 'ready', phase_label: 'READY TO START'}, fit: {state: 'done'}});
  assert.strictEqual(r.name, 'IDLE');
  assert.ok(/armed and waiting/.test(r.sub), r.sub);
});

check('collecting and ramping are distinguished by colour', h => {
  const c = h.show({pipeline: {current_stage: 'collect'}},
    {reactor: {phase: 'collecting', phase_label: 'COLLECTING',
               phase_detail: '10 frames · 30s exposure'}});
  assert.strictEqual(c.name, 'COLLECTING');
  assert.strictEqual(c.state, 'running');

  const m = h.show({pipeline: {current_stage: 'fit'}},
    {reactor: {phase: 'ramping', phase_label: 'RAMPING TO TEMPERATURE',
               phase_detail: '188 → 240 °C'}});
  assert.strictEqual(m.name, 'RAMPING TO TEMPERATURE');
  assert.strictEqual(m.state, 'waiting', 'ramping is legitimate waiting');
});

check('an e-stop is shown as a fault', h => {
  const r = h.show({pipeline: {current_stage: 'collect'}},
    {reactor: {phase: 'estop', phase_label: 'EMERGENCY STOP',
               phase_detail: 'check the reactor app'}});
  assert.strictEqual(r.name, 'EMERGENCY STOP');
  assert.strictEqual(r.state, 'stalled');
});

check('no loop data at all is IDLE, not a crash', h => {
  const r = h.show({}, {});
  assert.strictEqual(r.name, 'IDLE');
  assert.strictEqual(r.state, 'idle');
});

console.log(`\n${passed} checks passed`);

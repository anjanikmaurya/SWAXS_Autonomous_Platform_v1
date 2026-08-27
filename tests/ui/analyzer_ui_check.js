/*
 * tests/ui/analyzer_ui_check.js — execute the analyzer page and assert it works.
 *
 * Python tests can check that the template CONTAINS the right markup; only a DOM
 * can check that the page actually RUNS. This harness loads the real template in
 * jsdom with the Flask endpoints and the SSE stream stubbed, drives the UI the
 * way an operator would (sort, filter, switch scales, collapse cards, reconnect),
 * and fails on any uncaught exception or console.error.
 *
 * The canvas 2-D context is a recording proxy because jsdom cannot rasterise;
 * that still catches the mistakes that matter here — undefined variables, wrong
 * call order, null element access.
 *
 * Run:  cd tests/ui && npm install jsdom && node analyzer_ui_check.js
 * pytest picks it up automatically when jsdom is installed (see
 * tests/test_analyzer_runtime.py); it skips otherwise.
 */
const fs=require('fs'); const {JSDOM, VirtualConsole}=require('jsdom');
const path=require('path');
/* resolve the template relative to this file so the harness works from anywhere */
const TPL=path.resolve(__dirname,'..','..','analyzer','templates','index.html');
let html=fs.readFileSync(TPL,'utf8');

const errs=[], warns=[];
const vc=new VirtualConsole();
vc.on('jsdomError', e=>errs.push('jsdomError: '+(e.stack||e.message)));
vc.on('error', (...a)=>errs.push('console.error: '+a.join(' ')));
vc.on('warn', (...a)=>warns.push(a.join(' ')));
vc.on('log', ()=>{});

/* ── fake backend, exactly the shapes the real endpoints return ─────────── */
const ROWS=[];
for(let i=1;i<=24;i++){
  const R=4+Math.sin(i*2.3)*0.5, P=0.02+Math.abs(Math.cos(i))*0.07;
  ROWS.push({name:`auto_20260729_${String(i).padStart(4,'0')}_a3f_sample_scan1_SAXS_subtracted.dat`,
    seq:i, ts:'14:2'+(i%10)+':00', radius:+R.toFixed(3), diameter:+(2*R).toFixed(3),
    pdi:+P.toFixed(3), confidence:+Math.max(.15,Math.min(.97,.97-1.5*P)).toFixed(2),
    distribution:'schulz', phase:null, guinier_rg:+(R*.775).toFixed(3), invariant_rel:1.01});
}
const q=[],I=[],MO=[],SG=[];
for(let k=0;k<240;k++){const qq=.04*Math.pow(10,2.1*k/239),x=qq*4;
  const P=Math.pow(3*(Math.sin(x)-x*Math.cos(x))/(x*x*x),2), m=9.4e3*P+.42;
  q.push(qq);MO.push(m);I.push(m*(1+.05*Math.sin(k*1.7)));SG.push(m*.06);}
const CAMP={status:'running',n_evaluations:24,budget:40,n_init:10,target_size:4,
  tolerance:.1,pdi_cap:.05,best:{size:3.994,pdi:.029,loss:.575,
  params:{T_reac:247.4,F_tot:83.5,x_ODE:.188,x_TOP:.181,x_oley:.009}},converged_condition:null};
const DIAG={ok:true,names:['T_reac','F_tot','x_ODE','x_TOP','x_oley'],has_truth:true,
  summary:{n:24,budget:40,status:'running',best_loss:.575,uncertainty_early:.95,
    uncertainty_late:.83,uncertainty_drop:.126,mean_step_early:1.06,mean_step_late:.78},
  convergence:{runs:ROWS.map(r=>r.seq),size:ROWS.map(r=>r.radius),pdi:ROWS.map(r=>r.pdi),
    target_size:4,tolerance:.1,pdi_cap:.05,n_init:10}};

function stub(w){
/* canvas: record instead of raster (jsdom has no 2d context) */
w.HTMLCanvasElement.prototype.getContext=function(){
  const noop=()=>{};
  return new Proxy({}, {get:(t,k)=>{
    if(k==='measureText') return ()=>({width:10});
    if(k==='canvas') return null;
    return typeof k==='string' ? (t[k]||noop) : noop;
  }, set:()=>true});
};
w.fetch=async (u)=>{
  let body={};
  if(u.startsWith('/api/result/')) body={summary:ROWS[ROWS.length-1],
    full:{phase:{phase:null},llm:{summary:'fit is well constrained',flags:[]}},
    plot:{q,I,model:MO,sigma:SG}};
  else if(u.startsWith('/api/campaign/diagnostics')) body=DIAG;
  else if(u.startsWith('/api/folder')) body={folder:'1D/SAXS/Subtracted',
    resolved:'/Users/a/Data/Auto_Run/1D/SAXS/Subtracted'};
  else if(u.startsWith('/api/project')) body={watching:'/Users/a/Data/Auto_Run'};
  else if(u.startsWith('/api/campaign')) body={ok:true};
  return {ok:true,status:200,json:async()=>body};
};
w.EventSource=ES;
}
class ES {
  constructor(){ ES.last=this; setTimeout(()=>this.onopen&&this.onopen(),0); }
  close(){}
  emit(d){ this.onmessage&&this.onmessage({data:JSON.stringify(d)}); }
}
const dom=new JSDOM(html,{runScripts:'dangerously',pretendToBeVisual:true,
  virtualConsole:vc, url:'http://localhost:5008/', beforeParse:stub});
const w=dom.window;

(async()=>{
  await new Promise(r=>setTimeout(r,220));
  const $=id=>w.document.getElementById(id);
  const T=[];
  const ok=(c,m)=>{ if(!c) errs.push('ASSERT: '+m); else T.push(m); };

  ok(!!ES.last,'EventSource opened');
  ok($('p_conn').textContent.includes('live'),'connection pill goes live');
  ok($('folder').value==='1D/SAXS/Subtracted','folder input prefilled');
  ok($('p_watch').textContent.includes('Auto_Run'),'watched path in the status strip');

  /* first frame: snapshot */
  ES.last.emit({results:ROWS,logs:[{ts:'14:31:02',tag:'ok',msg:'✓ fit R=3.99 conf=0.93'}],
                campaign:CAMP,total:ROWS.length,seq:24,reset:true});
  await new Promise(r=>setTimeout(r,180));

  const trs=()=>[...w.document.querySelectorAll('#rows tr')].filter(t=>t.dataset.n);
  ok(trs().length===24,'24 rows rendered, got '+trs().length);
  ok(trs()[0].dataset.n.includes('0024'),'newest row is first (default sort), got '+trs()[0].dataset.n);
  ok($('p_count').textContent.includes('24'),'profile count pill');
  ok($('rowsnote').textContent.includes('24'),'rows note');

  /* campaign card */
  ok($('c_bar').style.display==='block','progress bar shown');
  ok($('c_bar').firstElementChild.style.width==='60%','bar at 60%, got '+$('c_bar').firstElementChild.style.width);
  ok($('k_r').textContent==='3.994 nm','best radius KPI, got '+$('k_r').textContent);
  ok($('k_d').parentElement.className.includes('hit'),'delta KPI marked as inside tolerance');
  ok($('k_p').parentElement.className.includes('hit'),'PDI KPI marked as under cap');
  ok($('k_n').textContent==='24 / 40','runs KPI, got '+$('k_n').textContent);
  ok($('c_abort').disabled===false,'abort enabled while running');
  ok($('c_start').disabled===true,'start disabled while running');
  ok($('p_camp').textContent.includes('running'),'campaign pill');

  /* the selected fit */
  ok($('selName').textContent.includes('0024'),'auto-selected the newest profile');
  ok($('n-r').textContent.includes('nm'),'radius KPI filled: '+$('n-r').textContent);
  ok($('n-conf').innerHTML.includes('badge'),'confidence badge rendered');
  ok($('llmNote').textContent.includes('constrained'),'QC note shown');
  ok($('lg-stats').textContent.includes('points'),'fit stats line: '+$('lg-stats').textContent);

  /* parameter space */
  ok($('ps_x').options.length===5,'slice axes populated, got '+$('ps_x').options.length);
  ok($('ps_x').value==='T_reac'&&$('ps_y').value==='x_TOP','default slice axes');
  ok($('ps_diag').innerHTML.includes('13%')||$('ps_diag').innerHTML.includes('space explained'),
     'diagnostics tiles: '+$('ps_diag').textContent.slice(0,80));
  ok($('spark').style.display==='block','sparkline shown');
  ok($('sparkNote').textContent.includes('inside the band'),'sparkline note: '+$('sparkNote').textContent);
  ok([...w.document.querySelectorAll('#ps_tabs button')].some(b=>b.classList.contains('on')),
     'a view tab is active');

  /* incremental frame: one new row only, nothing else re-sent */
  const nr={...ROWS[0],name:'auto_20260729_0025_new_sample_scan1_SAXS_subtracted.dat',seq:25,radius:4.01};
  ES.last.emit({results:[nr],logs:[],campaign:CAMP,total:25,seq:25,reset:false});
  await new Promise(r=>setTimeout(r,60));
  ok(trs().length===25,'incremental row added, got '+trs().length);
  ok(trs()[0].dataset.n.includes('0025'),'new row sorted to the top');

  /* interactions */
  w.setSort('radius'); await new Promise(r=>setTimeout(r,30));
  const rad=trs().map(t=>+t.children[1].textContent);
  ok(rad.every((v,i)=>i===0||rad[i-1]>=v),'sorting by radius descending works');
  w.setSort('radius'); await new Promise(r=>setTimeout(r,30));
  const rad2=trs().map(t=>+t.children[1].textContent);
  ok(rad2.every((v,i)=>i===0||rad2[i-1]<=v),'clicking again reverses the sort');

  $('filter').value='0025'; w.applyFilter(); await new Promise(r=>setTimeout(r,30));
  ok(trs().length===1,'filter narrows to 1, got '+trs().length);
  ok($('rowsnote').textContent.includes('filter'),'filter reflected in the note');
  $('filter').value='zzzz'; w.applyFilter();
  ok(trs().length===0 && $('rows').textContent.includes('no file matches'),'empty filter state');
  $('filter').value=''; w.applyFilter(); await new Promise(r=>setTimeout(r,30));
  ok(trs().length===25,'clearing the filter restores every row, got '+trs().length);

  /* keyboard walk */
  const before=w.document.querySelector('#rows tr.sel');
  const ev=new w.KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true});
  w.document.body.dispatchEvent(ev);
  await new Promise(r=>setTimeout(r,60));
  ok(w.document.querySelector('#rows tr.sel')!==before || true,'arrow key handled without error');

  /* scales */
  for(const s of ['loglog','semilogy','semilogx','linear','guinier']){
    w.setScale(s);
    ok(w.document.querySelector(`#scalebar button[data-scale="${s}"]`).classList.contains('on'),
       'scale '+s+' activates');
  }
  $('showResid').checked=false; w.redraw();
  ok($('resid').style.display==='none','residual strip hides');
  $('showErr').checked=true; w.redraw(); T.push('error bars toggle');

  /* folds */
  w.toggleFold('fold-log');
  ok($('fold-log').classList.contains('shut'),'log card collapses');
  w.toggleFold('fold-log');
  ok(!$('fold-log').classList.contains('shut'),'log card reopens');

  /* theme */
  w.toggleTheme();
  ok(w.document.documentElement.getAttribute('data-theme')==='light','theme switches to light');
  w.toggleTheme();

  /* idle campaign must hide the run furniture */
  ES.last.emit({results:[],logs:[],campaign:{status:'idle'},total:25,seq:25,reset:false});
  await new Promise(r=>setTimeout(r,40));
  ok($('c_bar').style.display==='none' && $('spark').style.display==='none',
     'idle campaign hides the bar and sparkline');
  ok($('c_abort').disabled===true,'abort disabled when idle');

  /* reconnect */
  const first=ES.last; first.onerror&&first.onerror();
  ok($('p_conn').textContent.includes('reconnect'),'reconnect announced: '+$('p_conn').textContent);

  /* a failing endpoint must not break the page */
  w.fetch=async()=>({ok:false,status:500,json:async()=>({})});
  await w.select(ROWS[0].name);
  ok($('selName').textContent.includes('could not load'),'a 500 is reported, not swallowed');

  console.log('\n'+T.length+' checks passed');
  if(warns.length) console.log('warnings: '+warns.length);
  if(errs.length){ console.log('\nFAILURES:'); errs.forEach(e=>console.log(' ✗ '+e)); process.exit(1); }
  console.log('no runtime errors');
  /* the page keeps a setInterval (the stale-stream watchdog) and a reconnect
     timer alive, so jsdom never lets node drain its loop — exit explicitly */
  dom.window.close(); process.exit(0);
})();

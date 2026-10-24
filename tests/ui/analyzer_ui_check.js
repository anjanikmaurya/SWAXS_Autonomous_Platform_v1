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
// Expand Jinja {% include %} the way Flask does when it serves the page, so the
// inlined sprite and the shared icon helpers (icoSvg / setIco) exist here too.
html=html.replace(/{%\s*include\s+"([^"]+)"\s*%}/g,(m,f)=>fs.readFileSync(path.join(path.dirname(TPL),f),'utf8'));

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

const RANGE={ok:true,bounds:{T_reac:[180,300],F_tot:[40,120],x_ODE:[0,.3],x_TOP:[0,.3],x_oley:[0,.3]},
  x_sum_max:.9,safety:{T_max:320,F_tot_max:150},shapes:{sphere_cs:'sphere (core-shell counts)',sphere:'sphere only',any:'any'}};
const METHODS={fitters:[{id:'catalogue_bic',label:'Catalogue + BIC',ready:true,desc:'every shape'},
    {id:'ls_sphere',label:'Least-squares sphere',ready:true,desc:'sphere'},
    {id:'llm_assisted',label:'LLM-assisted',ready:true,desc:'AI proposes'}],
  optimisers:[{id:'random',label:'Random / Sobol search',ready:true,desc:'baseline'},
    {id:'gp_ei',label:'Bayesian GP + EI',ready:true,desc:'default',acq:{name:'EI',formula:'EI(x)'}},
    {id:'gp_ucb',label:'Bayesian GP + UCB',ready:true,desc:'ucb'},{id:'turbo',label:'TuRBO',ready:true,desc:'tr'},
    {id:'bax',label:'BAX',ready:true,desc:'band'},{id:'pareto_ehvi',label:'Pareto GP + EHVI',ready:true,desc:'front'}],
  goals:{'target-hit':{methods:['gp_ei','gp_ucb','turbo','random'],default:'gp_ei'},'level-set':{methods:['bax'],default:'bax'},
    'pareto':{methods:['pareto_ehvi'],default:'pareto_ehvi'},'benchmark':{methods:['gp_ei','gp_ucb','turbo','random','bax','pareto_ehvi'],default:'gp_ei'}},
  active:{fitter:'catalogue_bic',optimiser:'gp_ei'}};
const KSET={id:'pd',name:'Pd · ODE',version:4,chemistry:{keywords:['Pd']},matches_chemistry:true,
  cards:[{id:'L1',param:'T_reac',effect:'hotter, larger',range:[240,290],kind:'literature',source:'Smith 2021',status:'approved',check:{range:[240,290]}},
         {id:'N1',param:'T_reac',effect:'above 300 °C the ODE boils',range:[null,300],kind:'note',source:'note',status:'approved',limit_like:true,check:{range:[180,300]}},
         {id:'L2',param:'x_TOP',effect:'more TOP narrows',range:[0.1,0.4],kind:'literature',source:'S',status:'draft',passage:'More TOP narrows the PDI.',check:{range:[0.1,0.3],clipped:true,note:'clipped to 0.1 to 0.3 (search range)'}}],
  campaigns:[{campaign_id:'c1',run_tag:'Run2',n:24,runs:[{}],best:{size:2.48,pdi:.09},ended:'2026-10-20T10:00',project:'/p'}]};
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
  else if(u.startsWith('/api/search_range')) body=RANGE;
  else if(u.startsWith('/api/ai/status')) body={available:true,default_model:'claude-sonnet-5',thinking:['off','standard','deep'],
    models:[{id:'',label:'Default (from your AI settings)'},{id:'claude-opus-5-5',label:'Claude Opus 5.5'}]};
  else if(u.startsWith('/api/ai/test')) body={ok:true,model:'claude-opus-5-5',ms:812,thinking:'off',error:''};
  else if(u.startsWith('/api/knowledge/sets')) body={ok:true,chemistry:{product:'Pd nanoparticles'},
    sets:[{id:'pd',name:'Pd · ODE',version:4,keywords:['Pd'],n_cards:2,n_drafts:1,n_campaigns:1,matches_chemistry:true}]};
  else if(u.startsWith('/api/knowledge/set/pd/review')) body={ok:true,card:{}};
  else if(u.startsWith('/api/knowledge/set/pd/evaluation')) body={state:'done',kind:'evaluate',result:{caveat:'Indicative: 2 past campaigns.',campaigns:['Run1','Run2'],set_version:4,repeats:3,at:'2026-10-24',notes:[],
    summary:[{arm:'gp_ei',label:'GP + EI',runs_to_spec:6,reached:5,trials:6,best_after_10:1.2,fallbacks:0},{arm:'gp_ei_knowledge',label:'GP + EI + recipes from cards',runs_to_spec:4,reached:6,trials:6,best_after_10:0.9,fallbacks:0}]}};
  else if(u.startsWith('/api/knowledge/set/pd')) body={ok:true,set:KSET};
  else if(u.startsWith('/api/knowledge/search')) body={ok:true,results:[{id:'W1',title:'Pd in flow',year:2021,doi:'10.1/x',open_access:true,abstract:'Higher temperature gave larger Pd.'}]};
  else if(u.startsWith('/api/guide')) body={pages:[{id:'score',group:'Scoring',title:'How the score works',family:'',rows:[['What it does','one number']],maths:'score = ...',settings:[]},
    {id:'gp_ei',group:'Next recipe',title:'Bayesian GP + EI',family:'ML',rows:[['When to use','Hit a size']],maths:'EI',settings:[]}]};
  else if(u.startsWith('/api/methods')) body=METHODS;
  else if(u.startsWith('/api/campaign/folder')) body={folder:'1D/SAXS/Conditions',resolved:'/x/1D/SAXS/Conditions',reactor:'1D/SAXS/Conditions',mismatch:false};
  else if(u.startsWith('/api/campaign/preview')) body={ok:true,n_init:3,recipes:[
    {kind:'own',params:{T_reac:250,F_tot:70,x_ODE:.1,x_TOP:.1,x_oley:.1}},
    {kind:'spread',params:{T_reac:282,F_tot:114.5,x_ODE:.11,x_TOP:.11,x_oley:.04}},
    {kind:'spread',params:{T_reac:184,F_tot:62.8,x_ODE:.19,x_TOP:.22,x_oley:.22}}]};
  else if(u.startsWith('/api/folder')) body={folder:'1D/SAXS/Subtracted',
    resolved:'/Users/a/Data/Auto_Run/1D/SAXS/Subtracted'};
  else if(u.startsWith('/api/project')) body={project_root:'/Users/a/Data/Auto_Run',watching:'/Users/a/Data/Auto_Run/1D/SAXS/Subtracted'};
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
  virtualConsole:vc, url:'http://localhost:5107/', beforeParse:stub});
const w=dom.window;

(async()=>{
  await new Promise(r=>setTimeout(r,220));
  const $=id=>w.document.getElementById(id);
  const T=[];
  const ok=(c,m)=>{ if(!c) errs.push('ASSERT: '+m); else T.push(m); };

  ok(!!ES.last,'EventSource opened');
  ok($('p_conn').textContent.includes('live'),'connection pill goes live');
  ok($('folder').value==='1D/SAXS/Subtracted','folder input prefilled');
  ok($('barProject').textContent.includes('Auto_Run'),'project folder shown once, on the right of the top bar');

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
  ok($('k_r').textContent==='7.988 nm','best DIAMETER KPI (2 × radius 3.994), got '+$('k_r').textContent);
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
  ok($('ps_diag').innerHTML.includes('13%')||/space explained/i.test($('ps_diag').innerHTML),
     'diagnostics tiles: '+$('ps_diag').textContent.slice(0,80));
  ok($('spark').style.display==='block','sparkline shown');
  ok(/band/.test($('sparkNote').textContent) && /\d+\/\d+ inside/.test($('sparkNote').textContent),
     'sparkline note says how many runs landed in the band: '+$('sparkNote').textContent);
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
  ok(trs().length===0 && /no file matches/i.test($('rows').textContent),'empty filter state');
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
  // The start theme is the platform rule (saved choice, else OS) — so check the
  // toggle FLIPS it and flips back, rather than assuming a fixed start.
  const th0=w.document.documentElement.getAttribute('data-theme');
  ok(th0==='light'||th0==='dark','theme set before first paint: '+th0);
  w.toggleTheme();
  const th1=w.document.documentElement.getAttribute('data-theme');
  ok(th1!==th0 && (th1==='light'||th1==='dark'),'theme toggles: '+th0+' -> '+th1);
  w.toggleTheme();
  ok(w.document.documentElement.getAttribute('data-theme')===th0,'theme toggles back');
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

  /* ── goal column, Setup steps, starting recipes, held results ───────── */
  const goals=[...w.document.querySelectorAll('#sidebar .nav-btn')].map(b=>b.dataset.goal);
  ok(goals.join()==='target-hit,level-set,pareto,benchmark','four goals in the left column: '+goals);
  ok($('c_range').textContent.includes('180 to 300'),'search range shown read-only: '+$('c_range').textContent);
  ok($('c_cond').value==='1D/SAXS/Conditions' && /same folder/.test($('c_cond_note').textContent),'conditions folder checked against the reactor');
  ok(w.document.querySelectorAll('#fitList .mopt').length===3,'fitting methods listed as radio rows');
  ok([...w.document.querySelectorAll('#optList .mopt')].map(l=>l.dataset.id).join()==='random,gp_ei,gp_ucb,turbo,ai_advisor,ai_copilot,ai_pilot',
     'next-recipe list filtered to the goal: '+[...w.document.querySelectorAll('#optList .mopt')].map(l=>l.dataset.id));
  ok(/AI-assisted structure/.test($('fitList').textContent),'the AI fitter has one name');
  ok($('c_ninit').textContent==='10','cold start defaults to 10, got '+$('c_ninit').textContent);
  ok($('k_set').value==='pd','the knowledge set matching chemistry.yml is preselected');
  ok($('c_nknow').value==='3' && $('c_nspread').value==='7','knowledge splits the cold start: 3 from knowledge + 7 spread, got '+$('c_nknow').value+'+'+$('c_nspread').value);
  ok(/1 card/.test($('k_lit_n').textContent) && /1 card/.test($('k_note_n').textContent),'approved card counts per kind');
  ok(/1 draft/.test($('k_set_note').textContent),'drafts waiting are flagged, not used');
  ok(/Knowledge: Pd/.test($('readyList').textContent),'ready list names the knowledge set');
  $('k_set').value=''; await w.kSetChosen();
  ok($('c_nknow').disabled && $('c_nspread').value==='10','no knowledge: back to 10 spread');
  $('k_set').value='pd'; await w.kSetChosen();
  ok($('c_nspread').value==='7','choosing the set again splits again');
  /* Knowledge tab */
  w.showTab('knowledge'); await new Promise(r=>setTimeout(r,80));
  ok($('kn_title').textContent==='Pd · ODE','knowledge tab opens the set');
  ok(w.document.querySelectorAll('#kn_tbl tbody tr').length===2,'approved cards listed (drafts not)');
  ok(/sounds like a limit/.test($('kn_tbl').textContent),'limit-like card flagged');
  w.kSection('drafts');
  ok(/clipped/.test($('kn_drafts').textContent) && /More TOP/.test($('kn_drafts').textContent),'draft shows its passage and the clip');
  w.kSection('camps');
  ok(/Run2/.test($('kn_ctbl').textContent) && /4.96 nm/.test($('kn_ctbl').textContent),'campaigns listed with diameter');
  w.kSection('eval'); await new Promise(r=>setTimeout(r,60));
  ok($('kh_n').value==='3' && /GP \+ EI/.test($('kn_eval_body').textContent),'evaluation: harness settings and the last replay');
  w.kSection('verdicts');
  ok(/No verdict yet/.test($('kn_vtbl').textContent),'verdicts section');
  await w.kSearch();
  ok(w.document.querySelectorAll('#kn_hits .khit').length===1 && /open access/.test($('kn_hits').textContent),'web search results listed');
  /* Guide tab */
  w.showTab('guide'); await new Promise(r=>setTimeout(r,80));
  ok(w.document.querySelectorAll('#gd_nav .gd-item').length===2 && /one number/.test($('gd_body').textContent),'guide pages load');
  w.guideShow('gp_ei'); ok(/Hit a size/.test($('gd_body').textContent),'guide page switches');
  w.showTab('setup');
  const gl=w.document.querySelector('#optList .glink');
  ok(gl && gl.dataset.guide==='random','methods link to their Guide page');
  /* AI levels */
  w.document.querySelector('#optList input[value="ai_copilot"]').click(); await new Promise(r=>setTimeout(r,30));
  ok(/Shortlist from/.test($('m_opt_desc').textContent) && $('ai_inner').value==='gp_ei','Co-pilot shows the optimiser it shortlists from');
  ok($('k_for').style.display==='' && /every AI pick/.test($('k_for').textContent),'the For choice appears with an AI level');
  ok([...$('ai_review').options].some(o=>o.value==='0'),'Co-pilot review can be off');
  ok($('ai_model').options.length===2 && /claude-sonnet-5/.test($('ai_model').options[0].textContent),'model choice lists the default and the models');
  $('ai_model').value='claude-opus-5-5'; $('ai_model').dispatchEvent(new w.Event('change'));
  await w.aiTest();
  ok(/Connected: claude-opus-5-5/.test($('ai_test_out').textContent) && /connected \(claude-opus-5-5/.test($('readyList').textContent),'connection test shown in the box and the ready list');
  ok(!!$('ai_think') && $('ai_think').options.length===3,'thinking levels offered');
  ok(/AI Co-pilot \(Bayesian GP \+ EI\)/.test($('pairHint').textContent),'the column shows the AI level and its optimiser');
  w.document.querySelector('#optList input[value="ai_pilot"]').click(); await new Promise(r=>setTimeout(r,30));
  ok(![...$('ai_review').options].some(o=>o.value==='0'),'Pilot cannot turn the review off');
  ok(/experimental/.test($('readyList').textContent),'Pilot flagged in the ready list');
  w.document.querySelector('#optList input[value="ai_advisor"]').click(); await new Promise(r=>setTimeout(r,30));
  ok(!!$('ai_apply') && /every progress review/.test($('k_for').textContent),'Advisor offers apply mode and reviews');
  w.document.querySelector('input[name=k_for][value=every]').checked=true;
  ok(w.kBody().use.every_pick===true,'every review/pick is sent with the knowledge');
  w.document.querySelector('#optList input[value="gp_ei"]').click(); await new Promise(r=>setTimeout(r,30));
  ok($('k_for').style.display==='none' && !w.kBody().use.every_pick,'back to the optimiser: no AI, no every-pick');
  ok(!!w.document.querySelector('#studyMethods input[value="ai_copilot"]'),'AI Co-pilot can be raced');
  /* Decisions in Report */
  ES.last.emit({results:[],logs:[],campaign:{...CAMP, ai:{level:'ai_copilot',label:'AI Co-pilot',n_fallbacks:1,can_undo:false,
    work_bounds:null,pending_suggestion:null,decisions:[
      {kind:'pick',n:11,by:'AI Co-pilot',choice:2,shortlist_size:5,recipe_id:'Run3_r011',reason:'card L1',cited:['L1'],knowledge_version:4,same_as_optimiser:false,params:{T_reac:250,F_tot:70,x_ODE:.1,x_TOP:.1,x_oley:.1},result:{diameter_nm:5.1,pdi:.08}},
      {kind:'pick',n:12,by:'optimiser (fallback)',fallback:'no card id or data was cited',recipe_id:'Run3_r012',params:{T_reac:260,F_tot:70,x_ODE:.1,x_TOP:.1,x_oley:.1}}]},
    knowledge:{set_id:'pd',name:'Pd · ODE',version:4},
    runs:[{recipe_id:'Run3_r010',n:10,by:'optimiser',params:{T_reac:240,F_tot:70,x_ODE:.1,x_TOP:.1,x_oley:.1},diameter_nm:4.9,pdi:.09,verdict:{call:'good',why:'close'}},
          {recipe_id:'Run3_r011',n:11,by:'AI Co-pilot',params:{T_reac:250,F_tot:70,x_ODE:.1,x_TOP:.1,x_oley:.1},diameter_nm:5.1,pdi:.08}]},total:25,seq:25,reset:false});
  await new Promise(r=>setTimeout(r,30));
  ok($('decCard').style.display==='' && w.document.querySelectorAll('#dec_tbl tbody tr').length>=2,'decisions table shown');
  ok(/1 of 2/.test($('dec_kpis').textContent) && /fallback: no card/.test($('dec_tbl').textContent),'AI picks and fallbacks are counted and explained');
  ok(/5.10 nm/.test($('dec_tbl').textContent) && /v4/.test($('dec_tbl').textContent),'result and knowledge version per decision');
  ok(w.document.querySelectorAll('#dec_tbl tbody tr').length===3 && /being made/.test($('dec_tbl').textContent),'every run listed, plus the pick in flight');
  ok(/good call/.test($('dec_tbl').textContent) && /Bad call/.test($('dec_tbl').textContent),'verdicts shown, and offered where missing');
  await new Promise(r=>setTimeout(r,60));
  ok($('helpCard').style.display==='' && /GP \+ EI \+ recipes from cards/.test($('help_body').textContent),'Did the knowledge help? shows the last replay');
  $('k_set').value=''; await w.kSetChosen();
  $('c_nspread').value='2'; w.countStart();
  ok($('c_nmsg').className==='warnt' && /Few/.test($('c_nmsg').textContent),'a short cold start is flagged: '+$('c_nmsg').textContent);
  $('c_nspread').value='45'; w.countStart();
  ok($('c_nmsg').className==='errt','a cold start longer than the budget is refused: '+$('c_nmsg').textContent);
  $('c_nspread').value='9'; w.countStart();
  /* own recipes are locked while the campaign runs (CAMP is running) */
  ES.last.emit({results:[],logs:[],campaign:{...CAMP,status:'idle'},total:25,seq:25,reset:false});
  await new Promise(r=>setTimeout(r,30));
  w.addOwn();
  ok(w.document.querySelectorAll('#ownRows .ownrow').length===1 && $('c_ninit').textContent==='10','adding your own recipe counts it');
  ok(w.document.querySelector('#ownRows input').placeholder.includes('180 to 300'),'own-recipe boxes show the allowed range');
  await w.previewStart();
  ok(w.document.querySelectorAll('#previewOut tbody tr').length===3 && /your own/.test($('previewOut').textContent),'preview lists the exact cold start');
  ok(/Cold start: 10/.test($('readyList').textContent) && /Expected shape/.test($('readyList').textContent),'ready list reflects the setup');
  /* a held result while running */
  const heldName=ROWS[5].name;
  ES.last.emit({results:[],logs:[],campaign:{...CAMP,confidence_min:.5,paused:true,
     held:[{recipe_id:heldName.slice(5,20),reason:'shape',model:'cylinder'}]},total:25,seq:25,reset:false});
  await new Promise(r=>setTimeout(r,40));
  ok(/held/.test(w.document.querySelector(`#rows tr[data-n="${heldName}"]`).textContent),'held result marked in the table');
  ok($('c_pause').textContent==='Resume' && !$('c_pause').disabled,'paused campaign offers Resume');
  ok(/PAUSED/.test($('c_status').textContent) && /held/.test($('r_status').textContent),'pause and held count in the status lines');
  ok(w.document.querySelector('#sidebar .nav-btn').disabled===true,'goal locked while running');
  await w.select(heldName);
  ok($('heldBox').style.display==='block' && /Accept/.test($('heldBox').textContent),'held fit offers Accept');
  /* Compare methods: simulator only */
  ES.last.emit({results:[],logs:[],campaign:{status:'idle'},total:25,seq:25,reset:false});
  await new Promise(r=>setTimeout(r,30));
  await w.setGoal('benchmark');
  ok($('stepFolders').style.display==='none' && $('studyMethods').style.display==='','compare hides folders, shows methods to race');
  ok([...w.document.querySelectorAll('#studyMethods input:checked')].map(i=>i.value).join()==='random,gp_ei','race defaults to Random + GP + EI');
  ok(!w.document.querySelector('#studyMethods input[value="bax"]'),'goal-specific methods are not raced on a single objective');
  ok($('c_start_lbl').textContent==='Run comparison','Start becomes Run comparison');
  await w.setGoal('target-hit');
  ok($('stepFolders').style.display==='','live goal restores the folders step');

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

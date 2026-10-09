/* Fake reactor backend for the layout mockup ONLY.
   Answers the same /api/* calls as reactor/app.py and streams status frames
   through a stand-in EventSource, so the real page script and its canvas plots
   run unchanged. Flush, background shot, arm, run, sample shot, flush, like the
   real sequence. Every setting and default comes from reactor/config.yml
   (window.__REACTOR_CFG, injected by build_flow_mockup.py). Durations are the
   real ones; only the clock runs faster (SPEED) so a run can be watched.
   Never used by the platform itself. */
(function(){
  var C=window.__REACTOR_CFG, PROJECT=C.project, SPEED=C.speed;
  var PUMPS=Object.keys(C.pumps), FLUSH_PUMP='ode_flush';
  var S={state:'idle',backend:C.backend,auto:false,pausing:false,runs:0,cur:null,phaseT:0,phaseLen:0,
    queue:[],recipes:{},n:0,estop:false,
    T:{current:C.temperature.cooldown_c,target:0,inBand:0},
    pumps:{}, limits:{},
    spec:{exposure_s:C.spec.exposure_s,frames:C.spec.frames,sample_tag:C.spec.sample_tag,bkg_tag:C.spec.bkg_tag,
          spec_lead_s:C.spec.spec_lead_s,data_dir:C.spec.data_dir},
    rs:{arm_mode:C.arming.default_mode,arm_wait_s:C.arming.default_wait_s,run_duration:C.run.default_duration,
        flush_rate:C.flush.rate,flush_duration:C.flush.duration,flush_pump:C.flush.pump},
    folder:C.folders.recipes,
    last_collect:null, logs:[], sampleShot:false, bstop:4.2e4, i0:1.25e5};
  PUMPS.forEach(function(p){var q=C.pumps[p];
    S.pumps[p]={actual:0,target:0,pressure:30,state_code:0,fault:false,idle:true,flow_ok:true,v_delivered:0,tareUntil:0};
    S.limits[p]={sensor_min:q.sensor_min,max_flow:q.max_flow,calibration_factor:q.calibration_factor};});
  var T0=Date.now()/1000, W0=T0;
  function now(){return W0+(Date.now()/1000-T0)*SPEED;}   /* simulated clock */
  function ts(){var d=new Date();return d.toTimeString().slice(0,8);}
  function log(msg,tag){S.logs.push({ts:ts(),msg:msg,tag:tag||'info'});}
  function addRecipe(r,src){S.n++;var id='M'+String(S.n).padStart(3,'0');r.recipe_id=id;S.recipes[id]=r;S.queue.push(id);
    log('＋ queued '+id+' ('+src+'): T '+r.T_reac+'°C, F '+r.F_tot+' µL/min','ok');return id;}
  addRecipe({T_reac:240,F_tot:80,x_ODE:0.2,x_TOP:0.1,x_oley:0.1},'conditions folder');
  addRecipe({T_reac:260,F_tot:100,x_ODE:0.25,x_TOP:0.15,x_oley:0.1},'conditions folder');
  log('Simulation mode: in-memory pumps and a simulated temperature ramp ('+C.temperature.mock_ramp+' °C/s).','info');
  log('Mockup clock runs ×'+SPEED+' so a '+(C.run.default_duration/60)+' min run can be watched; durations shown are the real ones.','warn');

  function check(r){var B=C.bounds,Sf=C.safety,xs=+r.x_ODE+(+r.x_TOP)+(+r.x_oley);
    if(r.T_reac<B.T_reac[0]||r.T_reac>B.T_reac[1])return 'T_reac '+r.T_reac+' outside ['+B.T_reac+'] °C';
    if(r.F_tot<B.F_tot[0]||r.F_tot>B.F_tot[1])return 'F_tot '+r.F_tot+' outside ['+B.F_tot+'] µL/min';
    var xn={x_ODE:r.x_ODE,x_TOP:r.x_TOP,x_oley:r.x_oley};for(var k in xn){if(xn[k]<B.x_each[0]||xn[k]>B.x_each[1])return k+' '+xn[k]+' outside ['+B.x_each+']';}
    if(xs>B.x_sum_max)return 'x_ODE+x_TOP+x_oley = '+xs.toFixed(3)+' exceeds '+B.x_sum_max;
    if(r.T_reac>Sf.T_max)return 'SAFETY: T_reac exceeds T_max '+Sf.T_max;
    var sp=recipeTargets(r);for(var p in sp){var l=S.limits[p]||{},v=sp[p];
      if(v>0&&v<l.sensor_min)return p+' setpoint '+v.toFixed(3)+' µL/min is below its minimum '+l.sensor_min+' µL/min (recipe rejected)';
      if(v>l.max_flow)return p+' setpoint '+v.toFixed(3)+' µL/min exceeds its maximum '+l.max_flow+' µL/min (recipe rejected)';}
    return '';}
  function setTargets(map){PUMPS.forEach(function(p){S.pumps[p].target=map[p]||0;});}
  function recipeTargets(r){var F=+r.F_tot,a=+r.x_ODE,b=+r.x_TOP,c=+r.x_oley;
    return {pd_top_precursor:F*(1-a-b-c),ode_dilution:F*a,top:F*b,oleylamine:F*c};}
  function flushTargets(){var m={},p=S.rs.flush_pump;m[p]=Math.min(+S.rs.flush_rate,S.limits[p]?S.limits[p].max_flow:+S.rs.flush_rate);return m;}
  function enter(state,len){S.state=state;S.phaseT=now();S.phaseLen=len||0;}
  function collect(role){S.last_collect={t:now(),role:role,recipe_id:S.cur?S.cur.recipe_id:'test'};
    log('📷 2D '+role+' collect: '+S.spec.frames+' × '+S.spec.exposure_s+' s → '+(S.cur?S.cur.recipe_id:'test')+'_'+(role==='background'?S.spec.bkg_tag:S.spec.sample_tag),'ok');}
  function begin(){if(!S.queue.length)return false;var id=S.queue.shift();S.cur=S.recipes[id];S.sampleShot=false;
    PUMPS.forEach(function(p){S.pumps[p].v_delivered=0;});
    log('▶ '+id+': blank rinse '+C.flush.blank_rinse_s+' s, then background shot','info');S.pre=true;setTargets(flushTargets());enter('flushing',C.flush.blank_rinse_s);return true;}
  function startArming(){S.T.target=+S.cur.T_reac;setTargets({});
    if(S.rs.arm_mode==='timed'){enter('arming',+S.rs.arm_wait_s||C.arming.default_wait_s);log('… arming: timed start in '+S.phaseLen+' s','info');}
    else{enter('arming',0);log('… arming: heating to '+S.T.target+' °C','info');}}
  function startRun(){setTargets(recipeTargets(S.cur));enter('running',+S.rs.run_duration||C.run.default_duration);log('● running '+S.cur.recipe_id+' for '+S.phaseLen+' s','ok');}
  function finishRun(){setTargets(flushTargets());S.T.target=0;S.pre=false;enter('flushing',+S.rs.flush_duration||C.flush.duration);log('↻ flushing line','info');}
  function done(){setTargets({});S.runs+=S.cur?1:0;if(S.cur)log('✓ '+S.cur.recipe_id+' complete','ok');S.cur=null;enter('ready');
    if(S.pausing){S.pausing=false;S.auto=false;log('Paused after this condition.','warn');}
    else if(S.auto&&S.queue.length)begin();}

  setInterval(function(){var t=now(),el=t-S.phaseT,dt=0.5*SPEED;
    if(S.state==='flushing'&&el>=S.phaseLen){ if(S.pre&&S.cur){collect('background');startArming();} else done(); }
    else if(S.state==='arming'){ var ib=Math.abs(S.T.current-S.T.target)<=C.temperature.tolerance;
      S.T.inBand=ib?S.T.inBand+dt:0;
      if(S.rs.arm_mode==='timed'?el>=S.phaseLen:S.T.inBand>=C.temperature.stable_hold) startRun(); }
    else if(S.state==='running'){ if(!S.sampleShot&&el>=S.phaseLen-(+S.spec.spec_lead_s||C.spec.spec_lead_s)){S.sampleShot=true;collect('sample');}
      if(el>=S.phaseLen) finishRun(); }
    // physics
    var tgt=S.T.target>0?S.T.target:C.temperature.cooldown_c, rate=C.temperature.mock_ramp;
    S.T.current+=Math.max(-rate*dt,Math.min(rate*dt,tgt-S.T.current))+(Math.random()-.5)*0.15;
    PUMPS.forEach(function(p){var q=S.pumps[p];q.actual+= (q.target-q.actual)*0.35+(q.target>0?(Math.random()-.5)*q.target*0.02:0);
      if(q.actual<0.05&&q.target===0)q.actual=0;
      q.pressure=30+q.actual*(p==='ode_flush'?0.9:8)+(Math.random()-.5)*4;q.idle=q.target===0;
      q.flow_ok=q.idle||Math.abs(q.actual-q.target)<Math.max(1,q.target*0.1);
      if(S.state==='running')q.v_delivered+=q.actual*dt/60;q.state_code=t<q.tareUntil?2:0;});
    S.i0=1.25e5*(1+(Math.random()-.5)*0.01);
    S.bstop=S.i0*(S.state==='running'?0.28:0.34)*(1+(Math.random()-.5)*0.01);
  },500);

  function status(){var t=now(),el=t-S.phaseT,busy=['arming','running','flushing'].indexOf(S.state)>=0;
    return {state:S.state,backend:S.backend,auto_run:S.auto,pausing:S.pausing,supervising:true,loop_faults:0,last_fault:null,
      run_settings:S.rs,spec:Object.assign({},S.spec,{locked:busy&&!!S.cur,lock_reason:busy?'Locked while a condition runs; unlocks when it has flushed.':''}),
      current_recipe:S.cur,pumps:JSON.parse(JSON.stringify(S.pumps)),
      temperature:{current:S.T.current,target:S.T.target,stable:S.T.target>0&&S.T.inBand>=C.temperature.stable_hold,source:'mock',stale:false,age_s:0.5,bstop:S.bstop,i0:S.i0},
      last_collect:S.last_collect,elapsed_s:S.state==='running'?el:null,duration_s:S.state==='running'?S.phaseLen:null,
      flush_remaining_s:S.state==='flushing'?Math.max(0,S.phaseLen-el):null,runs_completed:S.runs,queue:S.queue.slice(),
      paused_with_queue:!S.auto&&S.state==='ready'&&S.queue.length>0&&S.runs>0,arm_mode:S.rs.arm_mode,
      arm_total_s:S.state==='arming'&&S.rs.arm_mode==='timed'?S.phaseLen:null,
      arm_remaining_s:S.state==='arming'&&S.rs.arm_mode==='timed'?Math.max(0,S.phaseLen-el):null};}

  var quiet=function(){return ['idle','ready','estop'].indexOf(S.state)>=0;};
  var R={
    'GET /api/config':function(){return {backend:C.backend,flush:{rate:C.flush.rate,duration:C.flush.duration}};},
    'GET /api/project':function(){return {project_root:PROJECT};},
    'GET /api/health':function(){return {ok:true};},
    'GET /api/restart_notice':function(){return {level:'none'};},
    'GET /api/pumps':function(){return {limits:S.limits};},
    'POST /api/pumps':function(b){S.limits=b.limits;log('Pump limits saved','ok');return {ok:true,limits:S.limits};},
    'GET /api/recipes_folder':function(){return {folder:S.folder,resolved:S.folder.charAt(0)==='/'?S.folder:PROJECT+'/'+S.folder};},
    'POST /api/recipes_folder':function(b){S.folder=b.folder||C.folders.recipes;return {ok:true,resolved:(b.folder||'').charAt(0)==='/'?b.folder:PROJECT+'/'+b.folder};},
    'POST /api/recipe':function(b){var e=check(b);if(e)return {ok:false,error:e};addRecipe(b,'manual');return {ok:true};},
    'POST /api/auto_run':function(b){if(b.on){S.auto=true;S.pausing=false;log('Run autonomously: ON','ok');if(quiet()&&S.state!=='estop')begin();}
      else if(S.cur){S.pausing=true;log('Run autonomously: OFF after this condition','warn');}else{S.auto=false;}return {ok:true};},
    'POST /api/start':function(){if(!quiet()||S.state==='estop')return {error:'busy ('+S.state+')'};return begin()?{ok:true}:{error:'queue is empty'};},
    'POST /api/start_now':function(){if(S.state==='arming')startRun();return {ok:true};},
    'POST /api/abort':function(){if(S.state==='flushing'){setTargets({});S.cur=null;enter('idle');log('■ flush stopped','warn');}
      else if(S.state==='arming'||S.state==='running'){log('■ stopped by operator','warn');finishRun();}return {ok:true};},
    'POST /api/flush':function(b){if(['idle','ready'].indexOf(S.state)<0)return {error:'flush only from idle or ready'};
      if(b&&b.rate)S.rs.flush_rate=b.rate;if(b&&b.duration)S.rs.flush_duration=b.duration;S.cur=null;S.pre=false;setTargets(flushTargets());enter('flushing',+S.rs.flush_duration);log('↻ manual flush','info');return {ok:true};},
    'POST /api/reset':function(){setTargets({});S.cur=null;S.T.target=0;enter('idle');log('↺ reset','info');return {ok:true};},
    'POST /api/vent':function(){setTargets({});log('Vent: all pumps to 0 pressure','warn');return {ok:true};},
    'POST /api/estop':function(){setTargets({});S.auto=false;S.cur=null;S.T.target=0;enter('estop');log('■ EMERGENCY STOP: all pumps idled','error');return {ok:true};},
    'POST /api/run_settings':function(b){Object.keys(b).forEach(function(k){if(b[k]!==''&&b[k]!=null)S.rs[k]=isNaN(+b[k])?b[k]:+b[k];});return {ok:true};},
    'POST /api/spec_settings':function(b){if(status().spec.locked)return {ok:false,error:'Locked while a condition runs'};
      if(b.exposure_s!==''&&+b.exposure_s<0.1)return {ok:false,error:'exposure must be at least 0.1 s'};
      S.spec={exposure_s:+b.exposure_s||S.spec.exposure_s,frames:+b.frames||S.spec.frames,sample_tag:b.sample_tag||S.spec.sample_tag,
        bkg_tag:b.bkg_tag||S.spec.bkg_tag,spec_lead_s:+b.spec_lead_s||S.spec.spec_lead_s,data_dir:b.data_dir||''};return {ok:true};},
    'POST /api/collect_now':function(b){if(!quiet())return {ok:false,error:'idle only'};collect(b.role||'sample');return {ok:true};},
    'POST /api/tare':function(b){if(!quiet())return {ok:false,error:"can't tare while "+S.state};S.pumps[b.pump].tareUntil=now()+2*SPEED;log('Tare '+b.pump+' ('+b.kind+')','info');return {ok:true};},
    'POST /api/queue/clear':function(){var n=S.queue.length;S.queue=[];log('Queue cleared ('+n+')','warn');return {ok:true};},
    'POST /api/backend':function(b){return b.backend==='real'?{ok:false,error:'Hardware is not available in this mockup.'}:{ok:true};},
    'GET /api/checks':function(){return {checks:[{id:'pumps_present'},{id:'pumps_healthy'},{id:'pressure'},{id:'temperature'},{id:'beamline'}]};},
    'POST /api/checks/run':function(){var st=status(),P=st.pumps,n=Object.keys(P).length,mx=0,mn='';
      Object.keys(P).forEach(function(k){if(P[k].pressure>mx){mx=P[k].pressure;mn=k;}});
      return {ok:true,backend:'mock',results:[
        {id:'pumps_present',title:'All configured pumps connected',ok:true,value:n+'/'+n+' (simulated)'},
        {id:'pumps_healthy',title:'Every pump answers, no fault',ok:true,value:'all answering (simulated)'},
        {id:'pressure',title:"Chamber pressure within each pump's ceiling",ok:true,value:'max '+mx.toFixed(0)+' mbar ('+mn+')'},
        {id:'temperature',title:'Reactor temperature is a live reading',ok:true,value:st.temperature.current.toFixed(2)+' °C (simulated)'},
        {id:'beamline',title:'Beamline readings arriving (I₀, bstop)',ok:true,value:'I₀ '+st.temperature.i0.toFixed(0)+', bstop '+st.temperature.bstop.toFixed(0)+' (simulated)'}]};},
    'GET /api/browse':function(){return {current:PROJECT+'/1D/SAXS',parent:PROJECT+'/1D',dirs:['Averaged','Conditions','Reduction','Subtracted']};}
  };
  window.fetch=function(url,o){var u=String(url).split('?')[0],m=(o&&o.method)||'GET',b={};
    try{if(o&&o.body)b=JSON.parse(o.body);}catch(e){}
    var h=R[m+' '+u]||R['GET '+u],d=h?h(b):{error:'not in mockup: '+u};
    return Promise.resolve({ok:!d.error,status:d.error?400:200,json:function(){return Promise.resolve(d);}});};
  window.EventSource=function(){var self=this;setTimeout(function(){self.onopen&&self.onopen();},50);
    setInterval(function(){if(!self.onmessage)return;var logs=S.logs;S.logs=[];self.onmessage({data:JSON.stringify({status:status(),logs:logs})});},500);};
})();

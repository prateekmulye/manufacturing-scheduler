'use strict';

const $ = id => document.getElementById(id);
const clone = value => structuredClone(value);
const same = (a, b) => Boolean(a && b && a.session_id === b.session_id && a.revision === b.revision && a.input_hash === b.input_hash);
const state = {config:null, current:null, draft:null, tuple:null, revision:0, dirty:false, jsonDirty:false, epoch:0, job:null, solving:false, validating:false, aiBusy:false, result:null, proposal:null, diffs:[], decision:{state:'unreviewed',result_hash:null}, clock:null};
const errors = {integer_bounds:'Use a whole minute within the scenario horizon.',id:'Use 1–64 letters, numbers, underscores, dots or hyphens.',duplicate:'This ID is already in use.',unknown_resource:'Choose an existing resource.',calendar_order:'Enter sorted, nonoverlapping intervals with start before end.',count:'Check the number of entries against the stated limits.',fields:'Required fields are missing or unsupported fields are present.',version:'Use scenario version 1.',text:'Enter 1–512 characters on one line.',invalid_json:'JSON could not be read. Check brackets, commas and quoted names.',duplicate_key:'JSON contains a repeated property name.',size:'Input exceeds 1 MiB.',busy:'Compute is busy. Retry when the current request finishes.',stale:'Inputs changed. Review the current revision and try again.',forbidden:'Session could not be verified. Reconnect, then validate inputs.',not_found:'Result expired from the service. Solve again.',unavailable:'Service or model unavailable. Manual inputs remain here.',timeout:'Request reached its time limit. Inputs remain here.'};

function el(tag, attrs={}, ...children) {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'onClick') node.addEventListener('click',value);
    else if (value !== false && value != null) node.setAttribute(key,value === true ? '' : value);
  }
  for (const child of children.flat()) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
function button(text, action, cls='small') { return el('button',{type:'button',class:cls,onClick:action},text); }
function say(text) { $('notice').textContent = text; }
function clearErrors() {
  $('errors').hidden = true; $('errors').replaceChildren();
  document.querySelectorAll('[aria-invalid]').forEach(node=>node.removeAttribute('aria-invalid'));
  document.querySelectorAll('.field-error').forEach(node=>node.remove());
}
function fail(error) {
  const detail = error.detail || {code:error.message === 'Failed to fetch' ? 'unavailable' : 'request_failed',fields:[]};
  const message = errors[detail.code] || error.message || 'Request failed. Your current inputs remain here.';
  const list = el('ul');
  for (const field of detail.fields || []) {
    const path = field.path.replace(/^input\./,'');
    const target = [...document.querySelectorAll('[data-path]')].find(node=>node.dataset.path === path);
    const copy = `${path}: ${errors[field.code] || field.code.replaceAll('_',' ')}`;
    if (target) {
      target.setAttribute('aria-invalid','true');
      target.insertAdjacentElement('afterend',el('span',{class:'field-error'},errors[field.code] || field.code));
      list.append(el('li',{},el('a',{href:`#${target.id}`},copy)));
    } else list.append(el('li',{},copy));
  }
  $('errors').replaceChildren(el('h3',{},'Check this before continuing'),el('p',{},message),list);
  $('errors').hidden = false; $('errors').focus();
}
async function api(path, body, method='POST') {
  const response = await fetch(path,{method,headers:{'X-CSRF-Token':state.config?.csrf_token || '', 'X-Session-ID':state.config?.session_id || '', ...(body !== undefined ? {'Content-Type':'application/json'} : {})},...(body !== undefined ? {body:JSON.stringify(body)} : {}),cache:'no-store',signal:AbortSignal.timeout(55000)});
  const data = await response.json();
  if (!response.ok) { const error = new Error(errors[data.error?.code] || `Request failed (${response.status}).`); error.detail = data.error; throw error; }
  return data;
}
async function confirmAction(title, copy) {
  const dialog = $('confirm-dialog'); $('confirm-title').textContent = title; $('confirm-copy').textContent = copy;
  dialog.returnValue = 'cancel'; dialog.showModal();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue === 'confirm'),{once:true}));
}
async function forget(job) { if (job) try { await api(`/api/jobs/${encodeURIComponent(job)}`,undefined,'DELETE'); } catch { /* Expired jobs are already forgotten. */ } }
function cancelSolve(announce=true) {
  const job = state.job; state.job = null; state.solving = false; state.epoch++;
  clearInterval(state.clock); state.clock = null; $('elapsed').textContent = ''; forget(job);
  controls();
  if (announce) $('solve-state').textContent = 'Cancelled. Inputs preserved.';
}
function invalidate() {
  state.dirty = true; state.decision = {state:'unreviewed',result_hash:null};
  if (state.solving) cancelSolve(false); else state.epoch++;
  state.proposal = null; $('proposal').hidden = true; controls();
}
function canPlan() { return Boolean(state.config && state.tuple && state.current && !state.dirty && !state.jsonDirty && !state.validating); }
function canReview() { return Boolean(canPlan() && !state.solving && state.result && !state.result.superseded && same(state.result.tuple,state.tuple) && state.result.candidate?.checked && ['optimal','feasible'].includes(state.result.candidate.status)); }
function controls() {
  $('empty').hidden = Boolean(state.draft); $('editor').hidden = !state.draft;
  $('rule-workspace').hidden = !state.current; $('rules-empty').hidden = Boolean(state.current);
  $('revision').textContent = state.tuple ? `Revision ${state.tuple.revision}${state.dirty || state.jsonDirty ? ' · edits pending' : ''}` : 'No validated scenario';
  $('draft-state').textContent = state.dirty || state.jsonDirty ? 'Unsaved draft' : state.tuple ? 'Current inputs' : 'Not yet validated';
  $('validate').disabled = !state.config || !state.draft || state.jsonDirty || state.validating;
  $('use-json').disabled = !state.config || state.validating;
  $('discard').disabled = !state.current;
  $('solve').disabled = !canPlan() || state.solving;
  $('cancel').hidden = !state.solving;
  $('propose').disabled = !canPlan() || state.aiBusy;
  $('manual-form').querySelector('button').disabled = !canPlan();
  $('accept').disabled = $('reject').disabled = !canReview();
  $('download-proposal').disabled = !canReview();
  $('download-input').disabled = !state.current;
  $('explain').disabled = !canReview() || state.aiBusy;
  if (state.result) {
    $('result-notice').textContent = state.result.superseded ? 'Earlier solve retained for inspection. Complete a new solve before accepting.' : same(state.result.tuple,state.tuple) && !state.dirty && !state.jsonDirty ? `Checked against input revision ${state.result.tuple.revision}.` : `Previous revision ${state.result.tuple.revision} or pending edits. Validate and solve again before accepting.`;
  }
  if (!state.solving) $('solve-state').textContent = state.dirty || state.jsonDirty ? 'Finish or discard input edits before solving.' : state.current ? 'Ready to solve. No work dispatched.' : 'Validate inputs to begin.';
  $('decision').textContent = !canReview() ? 'No current checked proposal to review.' : state.decision.state === 'accepted' ? 'Accepted for planning. No work dispatched.' : state.decision.state === 'rejected' ? 'Rejected. Proposal remains available for inspection.' : 'Unreviewed planning proposal.';
}

function pathParts(path) { return path.replace(/\[(\d+)\]/g,'.$1').split('.'); }
function setPath(path,value) { const keys=pathParts(path); let target=state.draft; keys.slice(0,-1).forEach(key=>target=target[key]); target[keys.at(-1)]=value; }
function field(label,path,value,{type='text',options=null,hint=null,nullable=false}={}) {
  const id=`field-${path.replaceAll(/[^a-zA-Z0-9]/g,'-')}`;
  const input=options ? el('select',{id,'data-path':path},options.map(option=>el('option',{value:option.value ?? option},option.label ?? option))) : el('input',{id,'data-path':path,type,...(type==='number' ? {min:0,step:1} : {maxlength:path.endsWith('id') ? 64 : 512})});
  input.value=value ?? ''; if (nullable) input.dataset.nullable='true';
  return el('label',{},label,input,hint ? el('small',{class:'field-hint'},hint) : null);
}
function changeDraft(action) { invalidate(); action(); renderEditor(); }
function table(caption,headers,rows) {
  return el('table',{},el('caption',{},caption),el('thead',{},el('tr',{},headers.map(header=>el('th',{scope:'col'},header)))),el('tbody',{},rows.map(row=>el('tr',{},row.map(cell=>el('td',{},cell))))));
}
function nextID(prefix, existing) { let n=1; while (existing.includes(`${prefix}${n}`)) n++; return `${prefix}${n}`; }
function renderEditor() {
  if (!state.draft) return controls();
  const d=state.draft, root=$('structured-editor'); root.replaceChildren();
  $('summary').replaceChildren(...[[d.resources.length,'resources'],[d.orders.length,'orders'],[d.orders.reduce((n,o)=>n+o.operations.length,0),'operations'],[d.horizon,'minute horizon']].map(([n,label])=>el('span',{},el('strong',{},n),label)));
  root.append(el('div',{class:'fields'},field('Horizon, minutes','horizon',d.horizon,{type:'number'}),field('Source kind','source.kind',d.source.kind,{options:['synthetic','public']}),field('Source reference / author','source.reference',d.source.reference),field('Use-rights note','source.rights_note',d.source.rights_note)));
  const resources=el('div',{class:'editor-group'},el('div',{class:'group-heading'},el('h3',{},'Resources and availability'),button('Add resource',()=>changeDraft(()=>d.resources.push({id:nextID('M',d.resources.map(r=>r.id)),availability:[[0,d.horizon]]})))));
  d.resources.forEach((resource,i)=>{
    const row=el('div',{class:'resource-block'},el('div',{class:'resource-head'},field('Resource ID',`resources[${i}].id`,resource.id),button(`Remove resource ${resource.id}`,()=>changeDraft(()=>d.resources.splice(i,1)),'small quiet danger')));
    const intervals=el('div',{class:'intervals'});
    resource.availability.forEach((range,j)=>intervals.append(el('div',{class:'interval'},field('Start minute',`resources[${i}].availability[${j}][0]`,range[0],{type:'number'}),field('End minute',`resources[${i}].availability[${j}][1]`,range[1],{type:'number'}),button('Remove',()=>changeDraft(()=>resource.availability.splice(j,1)),'small quiet'))));
    row.append(intervals,button('Add availability',()=>changeDraft(()=>resource.availability.push([resource.availability.at(-1)?.[1] ?? 0,d.horizon]))),el('p',{class:'field-hint'},'An operation must fit inside one interval. Empty availability means this resource is unavailable.'));
    resources.append(row);
  }); root.append(resources);
  const orders=el('div',{class:'editor-group'},el('div',{class:'group-heading'},el('h3',{},'Orders and operation routes'),button('Add order',()=>changeDraft(()=>{const id=nextID('O',d.orders.map(o=>o.id));d.orders.push({id,release:0,due:d.horizon,deadline:null,operations:[{id:`${id}.1`,resource_id:d.resources[0]?.id || '',duration:60}]});}))));
  d.orders.forEach((order,i)=>{
    const box=el('div',{class:'order-block'},el('div',{class:'group-heading'},el('h3',{},`Order ${order.id}`),button(`Remove order ${order.id}`,()=>changeDraft(()=>d.orders.splice(i,1)),'small quiet danger')));
    box.append(el('div',{class:'fields order-fields'},field('Order ID',`orders[${i}].id`,order.id),field('Release minute',`orders[${i}].release`,order.release,{type:'number'}),field('Due minute',`orders[${i}].due`,order.due,{type:'number',hint:'Soft target. Late work is allowed.'}),field('Deadline minute',`orders[${i}].deadline`,order.deadline,{type:'number',nullable:true,hint:'Hard limit. Blank means none.'})));
    const route=el('details',{open:true},el('summary',{},`Operation route · ${order.operations.length} steps`));
    route.append(el('div',{class:'table-scroll'},table(`Ordered route for ${order.id}`,['Sequence','Operation ID','Resource','Duration, min','Order / remove'],order.operations.map((op,j)=>[
      j+1,field('Operation ID',`orders[${i}].operations[${j}].id`,op.id),field('Resource',`orders[${i}].operations[${j}].resource_id`,op.resource_id,{options:[...new Set([op.resource_id,...d.resources.map(r=>r.id)])]}),field('Duration',`orders[${i}].operations[${j}].duration`,op.duration,{type:'number'}),el('div',{class:'action-row'},button('Up',()=>changeDraft(()=>{if(j>0)[order.operations[j-1],order.operations[j]]=[op,order.operations[j-1]];})),button('Down',()=>changeDraft(()=>{if(j<order.operations.length-1)[order.operations[j+1],order.operations[j]]=[op,order.operations[j+1]];})),button('Remove',()=>changeDraft(()=>order.operations.splice(j,1)),'small quiet danger'))
    ]))));
    route.append(button('Add operation',()=>changeDraft(()=>order.operations.push({id:nextID(`${order.id}.`,d.orders.flatMap(o=>o.operations.map(p=>p.id))),resource_id:d.resources[0]?.id || '',duration:60}))));box.append(route);orders.append(box);
  }); root.append(orders);
  $('json-editor').value=JSON.stringify(d,null,2); state.jsonDirty=false; controls();
}
function blankScenario() { return {version:1,source:{kind:'synthetic',reference:'Independently authored scenario',rights_note:'Replace with the basis on which these inputs may be used.'},horizon:480,resources:[{id:'M1',availability:[[0,480]]}],orders:[{id:'A',release:0,due:480,deadline:null,operations:[{id:'A1',resource_id:'M1',duration:60}]}]}; }
function commit(input,tuple,{keepDiffs=false}={}) {
  cancelSolve(false);state.current=clone(input);state.draft=clone(input);state.tuple=tuple;state.revision=Math.max(state.revision,tuple.revision);state.dirty=false;state.jsonDirty=false;state.proposal=null;state.decision={state:'unreviewed',result_hash:null};if(!keepDiffs)state.diffs=[];
  $('proposal').hidden=true;clearErrors();renderEditor();renderRuleTargets();controls();
}
async function validateDraft(input=state.draft) {
  if(!state.config||state.validating)return false;
  clearErrors();cancelSolve(false);const epoch=state.epoch, revision=++state.revision, session=state.config.session_id;state.validating=true;controls();
  try { const result=await api('/api/validate',{session_id:session,revision,input}); if(epoch!==state.epoch){requireRevalidation(session);return false;}commit(result.input,result.tuple);say(`Inputs validated as revision ${result.tuple.revision}. Solve when ready.`);$('edit-details').open=false; return true; }
  catch(error){if(epoch===state.epoch)fail(error);return false;}finally{state.validating=false;controls();}
}
function requireRevalidation(session){if(state.config?.session_id===session&&state.current){state.tuple=null;state.dirty=true;state.decision={state:'unreviewed',result_hash:null};controls();say('Inputs changed during validation. Validate the editor again before solving.');}}
// JSON.parse checks syntax; this token walk also rejects duplicate property names before they can be lost.
function parseInput(text) {
  if(new TextEncoder().encode(text).length>1048576)throw Object.assign(new Error(errors.size),{detail:{code:'size',fields:[]}});
  let parsed;try{parsed=JSON.parse(text);}catch{throw Object.assign(new Error(errors.invalid_json),{detail:{code:'invalid_json',fields:[]}});}const tokens=text.match(/"(?:[^"\\]|\\.)*"|[{}\[\]:,]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null/g) || [];let i=0;
  function value(){const token=tokens[i++];if(token==='{'){const keys=new Set();while(tokens[i]!=='}'){const key=JSON.parse(tokens[i++]);if(keys.has(key))throw Object.assign(new Error(errors.duplicate_key),{detail:{code:'duplicate_key',fields:[]}});keys.add(key);i++;value();if(tokens[i]===',')i++;else break;}i++;}else if(token==='['){while(tokens[i]!==']'){value();if(tokens[i]===',')i++;else break;}i++;}}
  value();return parsed && parsed.input && parsed.export_version === 1 ? parsed.input : parsed;
}
async function replaceDraft(input) {
  if(state.draft && !await confirmAction('Replace current work?','This replaces the editor. Download first to keep a copy of your current inputs.'))return false;
  invalidate();state.draft=input;renderEditor();$('edit-details').open=true;say('Scenario loaded into the editor. Validate inputs before solving.');
  return true;
}
function renderRuleTargets() {
  const block=$('rule-type').value==='block_resource',deadline=$('rule-type').value==='set_deadline';
  $('rule-target').replaceChildren(...(block?state.current?.resources || []:state.current?.orders || []).map(item=>el('option',{value:item.id},item.id)));
  $('rule-end-label').hidden=!block;$('rule-end').required=block;$('rule-minute-label').textContent=block?'Start minute':'Minute';$('remove-deadline-label').hidden=!deadline;$('rule-minute').required=!(deadline&&$('remove-deadline').checked);$('rule-minute').disabled=deadline&&$('remove-deadline').checked;
}
function renderProposal() {
  const pending=state.proposal,root=$('proposal');root.replaceChildren();root.hidden=!pending;if(!pending)return;
  const p=pending.value;root.className='rule-diff';root.append(el('h3',{},pending.manual?'Review this manual rule change':'Review proposed rule changes'));
  if(p.error_code){root.append(el('p',{class:'danger'},`No changes made. ${p.error_code.replaceAll('_',' ')}. Manual editing remains available.`));return;}
  if(p.disposition!=='proposed'){root.append(el('p',{},p.disposition==='clarification'?'No changes proposed. Use an exact existing ID and explicit minute values.':'No supported change proposed. Use release, due, deadline or resource-closure rules.'));return;}
  root.append(el('p',{},`Based on revision ${pending.tuple.revision}. Inputs have not changed.`),el('div',{class:'table-scroll'},table('Complete rule diff',['Target / rule','Before','After','Constraint','Request and explanation'],p.edits.map(edit=>[
    `${edit.change.order_id || edit.change.resource_id} · ${edit.change.type.replaceAll('_',' ')}`,el('pre',{class:'code'},JSON.stringify(edit.before)),el('pre',{class:'code'},JSON.stringify(edit.after)),edit.classification,el('div',{},el('q',{},edit.quote.text),el('p',{},edit.explanation))
  ]))),el('div',{class:'action-row'},button('Apply these changes',applyProposal,'primary'),button('Reject changes',()=>{state.proposal=null;renderProposal();say('Changes rejected. Inputs unchanged.');},'')));
}
async function manualProposal(event) {
  event.preventDefault();if(!canPlan())return;clearErrors();
  const type=$('rule-type').value,id=$('rule-target').value,minute=Number($('rule-minute').value),end=Number($('rule-end').value),input=clone(state.current);let before,after,request,change;
  if(type==='block_resource'){
    const r=input.resources.find(r=>r.id===id);before=clone(r.availability);
    if(!Number.isInteger(minute)||!Number.isInteger(end)||minute<0||end<=minute||end>input.horizon)return fail(new Error('Closure needs whole minutes with 0 ≤ start < end ≤ horizon.'));
    r.availability=r.availability.flatMap(([a,b])=>b<=minute||a>=end?[[a,b]]:[[a,Math.min(b,minute)],[Math.max(a,end),b]].filter(([x,y])=>x<y));after=r.availability;request=`Block ${id} from minute ${minute} to minute ${end}`;change={type,resource_id:id,start:minute,end};
  }else{
    const key=type.slice(4),o=input.orders.find(o=>o.id===id);before=o[key];after=key==='deadline'&&$('remove-deadline').checked?null:minute;
    if(after!==null&&(!Number.isInteger(after)||after<0||after>input.horizon))return fail(new Error('Use a whole minute within the scenario horizon.'));
    o[key]=after;request=after===null?`Remove ${id} deadline`:`Set ${id} ${key} to minute ${after}`;change={type,order_id:id,minute:after};
  }
  state.proposal={manual:true,input,tuple:clone(state.tuple),request,value:{disposition:'proposed',error_code:null,edits:[{rule_id:`manual-${state.tuple.revision}-${type}-${id}`,change,before,after,quote:{start:0,end:request.length,text:request},classification:type==='set_due'?'soft':'hard',explanation:type==='set_due'?'Late work remains allowed and measured.':'This becomes a scheduling constraint after you approve it.'}]}};renderProposal();
}
async function applyProposal() {
  const pending=state.proposal;if(!pending||!canPlan()||!same(pending.tuple,state.tuple))return say('Proposal is stale. Review a new change.');
  const epoch=state.epoch;state.revision=Math.max(state.revision,pending.tuple.revision)+1;const revision=state.revision;state.validating=true;controls();clearErrors();
  try{
    const result=pending.manual?await api('/api/validate',{session_id:state.config.session_id,revision,input:pending.input}):await api('/api/apply',{tuple:pending.tuple,input:state.current,request:pending.request,proposal:pending.value});
    if(epoch!==state.epoch||!same(pending.tuple,state.tuple)){requireRevalidation(pending.tuple.session_id);return;}
    const diffs=pending.manual?pending.value.edits:(result.approved_diffs || pending.value.edits);
    commit(result.input,result.tuple,{keepDiffs:true});state.diffs.push(...diffs);state.diffs=state.diffs.slice(-80);say('Rules updated. Solve to review a new schedule.');
  }catch(error){if(epoch===state.epoch)fail(error);}finally{state.validating=false;controls();}
}
async function propose(event) {
  event.preventDefault();if(!canPlan()||state.aiBusy)return;const epoch=state.epoch,tuple=clone(state.tuple),request=$('request').value;state.aiBusy=true;controls();$('ai-progress').textContent='AI is preparing a bounded proposal…';clearErrors();
  try{const value=await api('/api/propose',{tuple,input:state.current,request});if(epoch!==state.epoch||!same(tuple,state.tuple)||!same(value.base_tuple,tuple))return;state.proposal={manual:false,tuple,request,value};renderProposal();}
  catch(error){if(epoch===state.epoch)fail(error);}finally{state.aiBusy=false;$('ai-progress').textContent='';controls();}
}

const statusCopy={optimal:'Optimal: best result proved for these inputs.',feasible:'Feasible: complete schedule checked; best result not proved.',infeasible:'Infeasible: no schedule satisfies these rules.',timeout_no_solution:'No solution before timeout: feasibility remains unknown.',cancelled:'Cancelled. No current scheduling result.',error:'Schedule could not be validated. Edit inputs or retry.',complete:'Complete schedule independently checked.',could_not_place_all_work:'Baseline could not place all work. This does not prove infeasibility.'};
function resultPanel(title,result,description) {
  const panel=el('article',{},el('h3',{},title),el('p',{},description),el('strong',{},statusCopy[result?.status] || 'No result available.'));
  if(result?.reason==='time_limit')panel.append(el('p',{},'Search stopped at its time limit.'));
  if(result?.checked && result.metrics)panel.append(el('div',{class:'metrics'},[['total_tardiness','Total tardiness, min'],['makespan','Latest completion, min'],['late_orders','Late orders']].map(([key,label])=>el('div',{},el('span',{class:'metric-value'},result.metrics[key]),el('span',{class:'metric-label'},label)))));
  return panel;
}
function renderResult() {
  const r=state.result;$('results').hidden=!r;if(!r)return;
  $('comparison').replaceChildren(resultPanel('Serial baseline',r.baseline,'Orders in input order, each operation in its earliest available slot.'),resultPanel('Scheduling result',r.candidate,'Constraint solver result, checked independently before review.'));
  $('delta').textContent='';
  if(r.baseline?.checked&&r.candidate?.checked){const b=r.baseline.metrics,c=r.candidate.metrics,d=c.total_tardiness-b.total_tardiness,m=c.makespan-b.makespan;$('delta').textContent=`Same-input comparison: ${d===0?'unchanged total tardiness':`${Math.abs(d)} minutes ${d<0?'less':'more'} total tardiness`}; ${m===0?'unchanged latest completion':`${Math.abs(m)} minutes ${m<0?'earlier':'later'} latest completion`}.${d>0||(d===0&&m>0)?' Candidate is worse than the baseline. Neither is accepted automatically.':''}`;}
  $('technical').textContent=JSON.stringify({revision:r.tuple.revision,input_hash:r.tuple.input_hash,result_hash:r.candidate?.result_hash,engine:r.candidate?.engine,checked:r.candidate?.checked,status:r.candidate?.status,reason:r.candidate?.reason},null,2);
  $('explanation').replaceChildren();$('schedule-view').value=r.candidate?.checked?'candidate':'baseline';renderSchedule();controls();
}
function svg(tag,attrs={},text=null){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==null)n.textContent=text;return n;}
function renderSchedule() {
  const r=state.result;if(!r)return;const result=r[$('schedule-view').value],input=r.input;
  $('timeline').replaceChildren();$('assignments').replaceChildren();$('outcomes').replaceChildren();
  if(!result?.checked||!result.assignments){$('timeline').append(el('p',{class:'notice'},'No checked full assignment for this view.'));return;}
  const chart=svg('svg',{viewBox:`0 0 1000 ${input.resources.length*62+68}`,class:'timeline-svg',role:'img','aria-label':'Resource timeline. Exact minute values are in the following assignment table.'});
  const defs=svg('defs'),pattern=svg('pattern',{id:'closure-pattern',width:7,height:7,patternUnits:'userSpaceOnUse'});pattern.append(svg('rect',{width:7,height:7,fill:'#efede5'}),svg('path',{d:'M-1 1L1-1 M0 7L7 0 M6 8L8 6',stroke:'#cbcfc5','stroke-width':1}));defs.append(pattern);chart.append(defs);
  const x=t=>145+t/input.horizon*825;
  for(let n=0;n<=5;n++){const at=Math.round(input.horizon*n/5);chart.append(svg('line',{x1:x(at),x2:x(at),y1:30,y2:input.resources.length*62+40,class:'timeline-grid'}),svg('text',{x:x(at),y:20,'text-anchor':'middle',class:'timeline-axis'},at));}
  input.resources.forEach((resource,i)=>{const y=44+i*62;chart.append(svg('text',{x:16,y:y+22,class:'timeline-label'},resource.id.length>17?`${resource.id.slice(0,16)}…`:resource.id));let end=0;for(const[a,b]of [...resource.availability,[input.horizon,input.horizon]]){if(a>end)chart.append(svg('rect',{x:x(end),y,width:x(a)-x(end),height:35,class:'timeline-gap'}));end=b;}
    result.assignments.filter(a=>a.resource_id===resource.id).forEach(a=>{const orderIndex=input.orders.findIndex(o=>o.operations.some(op=>op.id===a.operation_id));const bar=svg('g');bar.append(svg('title',{},`${a.operation_id}: minute ${a.start} to ${a.end}, ${resource.id}`),svg('rect',{x:x(a.start),y,width:Math.max(1,x(a.end)-x(a.start)),height:35,rx:3,class:`timeline-op${orderIndex%2?' alt':''}`}));if(x(a.end)-x(a.start)>50)bar.append(svg('text',{x:x(a.start)+7,y:y+22,class:'timeline-op-text'},a.operation_id.length>12?`${a.operation_id.slice(0,11)}…`:a.operation_id));chart.append(bar);});
  });$('timeline').append(chart);
  const rows=result.assignments.map(a=>{const o=input.orders.find(o=>o.operations.some(op=>op.id===a.operation_id)),j=o.operations.findIndex(op=>op.id===a.operation_id),op=o.operations[j];return[o.id,a.operation_id,a.resource_id,a.start,a.end,op.duration,j?o.operations[j-1].id:'None',o.release,o.due,o.deadline??'None'];});
  $('assignments').append(table('Exact operation assignments',['Order','Operation','Resource','Start','End','Duration','Predecessor','Release','Due','Deadline'],rows));
  $('outcomes').append(table('Order outcomes, minutes',['Order','Completion','Due','Tardiness'],result.metrics.orders.map(o=>[o.order_id,o.completion,input.orders.find(v=>v.id===o.order_id).due,o.tardiness])));
}
async function solve() {
  if(!canPlan()||state.solving)return;clearErrors();state.solving=true;if(state.result)state.result.superseded=true;state.decision={state:'unreviewed',result_hash:null};const epoch=state.epoch,tuple=clone(state.tuple),input=clone(state.current),started=performance.now();controls();$('solve-state').textContent='Searching for a schedule…';
  state.clock=setInterval(()=>{$('elapsed').textContent=`${((performance.now()-started)/1000).toFixed(1)} s elapsed`;},200);
  try{
    const accepted=await api('/api/solve',{session_id:tuple.session_id,revision:tuple.revision,input});
    if(epoch!==state.epoch||!same(accepted.tuple,tuple)){forget(accepted.job_id);return;}state.job=accepted.job_id;
    for(;;){await new Promise(resolve=>setTimeout(resolve,300));if(epoch!==state.epoch)return;const result=await api(`/api/jobs/${encodeURIComponent(state.job)}`,undefined,'GET');if(epoch!==state.epoch||!same(tuple,state.tuple)||!same(result.tuple,tuple))return;if(result.state==='complete'){state.result={...result,input};state.job=null;renderResult();say(statusCopy[result.candidate?.status] || 'Solve finished. Inspect the result.');break;}}
  }catch(error){if(epoch===state.epoch)fail(error);}finally{if(epoch===state.epoch){state.solving=false;clearInterval(state.clock);state.clock=null;$('elapsed').textContent='';controls();}}
}
async function explain() {
  if(!canReview()||state.aiBusy)return;const epoch=state.epoch,result=state.result;state.aiBusy=true;controls();$('explain-state').textContent='Selecting facts from checked evidence…';
  try{const answer=await api('/api/explain',{tuple:state.tuple,input:state.current,checked_result:result.candidate,approved_diffs:state.diffs.filter(diff=>diff.rule_id.startsWith('rule-'))});if(epoch!==state.epoch||!same(answer.tuple,state.tuple)||answer.result_hash!==result.candidate.result_hash)return;
    if(answer.error_code){$('explanation').replaceChildren(el('p',{class:'notice'},`Explanation unavailable: ${answer.error_code.replaceAll('_',' ')}. Checked evidence remains above.`));return;}
    $('explanation').replaceChildren(el('p',{class:'muted'},'AI selected these facts. The service rendered their text from checked evidence; this is not a causal diagnosis. Manual edits are reflected in the current inputs and checked results, not narrated as AI rule history.'),el('ul',{},answer.facts.map(f=>el('li',{},f.text))));
  }catch(error){if(epoch===state.epoch)fail(error);}finally{state.aiBusy=false;$('explain-state').textContent='';controls();}
}
function download(value,name){try{const blob=new Blob([JSON.stringify(value,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=el('a',{href:url,download:name});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);say('Download requested. This session remains open.');}catch{fail(new Error('Download could not be started. Your session is intact.'));}}
function exportProposal(){if(!canReview())return;const tuple={revision:state.tuple.revision,input_hash:state.tuple.input_hash};download({export_version:1,source:state.current.source,tuple,approved_diffs:state.diffs,input:state.current,baseline:state.result.baseline,candidate:state.result.candidate,decision:state.decision},'manufacturing-planning-proposal.json');}
async function boot() {
  cancelSolve(false);
  const guard=state.epoch;
  $('connection').textContent='Connecting to planning service…';
  try {
    const result=await fetch('/api/config',{cache:'no-store',signal:AbortSignal.timeout(10000)});
    if(!result.ok)throw new Error('Service unavailable.');
    const config=await result.json();
    if(guard!==state.epoch)return;
    state.config=config;state.tuple=null;
    if(state.current){state.draft=clone(state.current);state.dirty=true;state.decision={state:'unreviewed',result_hash:null};renderEditor();}
    state.proposal=null;$('proposal').hidden=true;
    $('example').replaceChildren(el('option',{value:''},'Choose an example'),...config.examples.map(item=>el('option',{value:item.url},item.title)));
    const hosted=config.hosting==='hosted',model=config.model||{};
    $('connection').textContent=`${hosted?'Hosted':'Local'} service connected · session only`;
    $('session-note').textContent=hosted
      ? 'Inputs are processed in server memory. Sessions expire after 30 minutes of inactivity; server solve results expire after 60 seconds. Reloading clears the browser workspace. AI actions send the planning request and relevant inputs or checked facts to Cloudflare Workers AI. No input database or content logs. Download to keep your work; avoid personal or confidential data.'
      : 'Inputs are processed on this machine. Sessions expire after 30 minutes of inactivity; server solve results expire after 60 seconds. Reloading clears the browser workspace. Download to keep your work. Reset does not erase model or operating-system buffers.';
    $('model-state').textContent=`${hosted?'Cloudflare Workers AI':'Local model'}: ${model.identity||model.model||'not available'}${model.available===false?' · unavailable':''}. ${hosted?'Runs only when you request AI. Shared daily quota; manual rules and scheduling remain available when AI is unavailable. No visitor API key needed.':'No remote fallback or model downloads.'}`;
    controls();
  } catch(error) {
    state.config=null;$('connection').textContent='Planning service unavailable. Open inputs remain here. Retry connection shortly.';controls();
  }
}

$('structured-editor').addEventListener('input',event=>{const target=event.target;if(!target.dataset.path)return;const value=target.type==='number'?(target.value===''?(target.dataset.nullable?null:''):Number(target.value)):target.value;setPath(target.dataset.path,value);invalidate();if(!state.jsonDirty)$('json-editor').value=JSON.stringify(state.draft,null,2);});
$('input-form').addEventListener('submit',event=>{event.preventDefault();if(state.draft&&state.config)validateDraft();});
$('discard').addEventListener('click',()=>{if(!state.current)return;cancelSolve(false);state.draft=clone(state.current);state.dirty=false;state.jsonDirty=false;clearErrors();renderEditor();say('Draft discarded. Current validated inputs restored.');});
$('start').addEventListener('click',()=>replaceDraft(blankScenario()));
$('load-example').addEventListener('click',async()=>{const path=$('example').value;if(!state.config?.examples.some(item=>item.url===path))return say('Choose an example first.');try{const response=await fetch(path,{cache:'no-store'});if(!response.ok)throw new Error('Example unavailable.');if(await replaceDraft(parseInput(await response.text())))await validateDraft();}catch(error){fail(error);}});
$('import-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;try{if(file.size>1048576)throw new Error(errors.size);const input=parseInput(await file.text());if(state.draft&&!await confirmAction('Import these inputs?','Current work will be replaced only if this file passes validation. Imported decisions will not be restored.'))return;if(await validateDraft(input))say('Imported inputs checked. Solve again to create a current proposal.');}catch(error){fail(error);}finally{event.target.value='';}});
$('json-editor').addEventListener('input',()=>{state.jsonDirty=true;invalidate();});
$('use-json').addEventListener('click',()=>{try{validateDraft(parseInput($('json-editor').value));}catch(error){fail(error);}});
$('refresh-json').addEventListener('click',()=>{$('json-editor').value=JSON.stringify(state.draft,null,2);state.jsonDirty=false;controls();});
$('rule-type').addEventListener('change',renderRuleTargets);$('remove-deadline').addEventListener('change',renderRuleTargets);$('manual-form').addEventListener('submit',manualProposal);$('ai-form').addEventListener('submit',propose);$('solve').addEventListener('click',solve);$('cancel').addEventListener('click',()=>cancelSolve());$('schedule-view').addEventListener('change',renderSchedule);$('explain').addEventListener('click',explain);
for(const [id,decision]of [['accept','accepted'],['reject','rejected']])$(id).addEventListener('click',()=>{if(!canReview())return;state.decision={state:decision,result_hash:state.result.candidate.result_hash};controls();say(decision==='accepted'?'Accepted for planning. No work dispatched.':'Planning proposal rejected. Inputs and evidence preserved.');});
$('download-input').addEventListener('click',()=>{if(state.current)download(state.current,'manufacturing-scenario.json');});$('download-proposal').addEventListener('click',exportProposal);
$('reset').addEventListener('click',async()=>{if(!await confirmAction('Clear this session?','Active work will be cancelled. Download first to keep a copy. Downloaded files and model buffers are not erased.'))return;cancelSolve(false);try{if(state.config){const result=await api('/api/reset',{session_id:state.config.session_id});state.config.session_id=result.session_id;state.config.csrf_token=result.csrf_token;}}catch(error){state.config=null;fail(error);}state.current=null;state.draft=null;state.tuple=null;state.revision=0;state.dirty=false;state.jsonDirty=false;state.result=null;state.proposal=null;state.diffs=[];state.decision={state:'unreviewed',result_hash:null};$('request').value='';$('json-editor').value='';$('proposal').hidden=true;$('results').hidden=true;controls();say('Session cleared. Downloaded files remain under your control.');});
$('reconnect').addEventListener('click',boot);boot();

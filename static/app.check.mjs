// Pure UI boundary checks. This does not replace browser interaction testing.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const context = vm.createContext({TextEncoder, errors:{size:'size', invalid_json:'invalid_json', duplicate_key:'duplicate_key'}});
vm.runInContext(source.slice(source.indexOf('function parseInput('), source.indexOf('async function replaceDraft(')), context);
assert.equal(context.parseInput('{"version":1}').version, 1);
assert.equal(context.parseInput('{"export_version":1,"input":{"version":1}}').version, 1);
assert.throws(() => context.parseInput('{"x":1,"x":2}'), /duplicate_key/);
assert.throws(() => context.parseInput('{"nested":[{"x":1,"x":2}]}'), /duplicate_key/);
assert.throws(() => context.parseInput('{"x":NaN}'), /invalid_json/);
assert.throws(() => context.parseInput('{"x":'), /invalid_json/);
assert.equal(context.parseInput('{"a":{"x":1},"b":{"x":2}}').b.x, 2);
assert.equal(context.parseInput('null'), null);
assert.throws(() => context.parseInput(' '.repeat(1048577)), /size/);

vm.runInContext(source.split('\n').find(line=>line.startsWith('const same =')), context);
vm.runInContext('var state = {};', context);
vm.runInContext(source.slice(source.indexOf('function canPlan('), source.indexOf('function controls(')), context);
const tuple = {session_id:'s', revision:1, input_hash:'h'};
Object.assign(context.state,{config:{}, current:{}, tuple, dirty:false, jsonDirty:false, validating:false, solving:false, result:{tuple, candidate:{checked:true,status:'optimal'}}});
assert.equal(context.canReview(), true);
for (const flag of ['dirty','jsonDirty','validating','solving']) {
  context.state[flag] = true;
  assert.equal(context.canReview(), false, flag);
  context.state[flag] = false;
}
context.state.result.superseded = true;
assert.equal(context.canReview(), false);
context.state.result.superseded = false;
for (const key of ['session_id','revision','input_hash']) {
  context.state.result.tuple = {...tuple,[key]:'different'};
  assert.equal(context.canReview(), false, key);
}
context.state.result.tuple = tuple;
context.state.result.candidate.checked = false;
assert.equal(context.canReview(), false);
// Execute the real async handlers. DOM painting is stubbed; lifecycle and API flow are not.
const nodes = new Map(), deleted = [], intervals = new Set();
const node = id => {
  if (!nodes.has(id)) nodes.set(id,{value:'',hidden:false,handlers:{},addEventListener(type, fn){this.handlers[type]=fn;},querySelector(){return node(`${id}-button`);},replaceChildren(){},focus(){}});
  return nodes.get(id);
};
const lifecycle = vm.createContext({TextEncoder,structuredClone,performance,console,
  document:{getElementById:node,querySelectorAll:()=>[]},
  setInterval:()=>{intervals.add(99);return 99;},clearInterval:id=>intervals.delete(id),
  setTimeout:fn=>{queueMicrotask(fn);},
});
vm.runInContext(source.replace("$('reconnect').addEventListener('click',boot);boot();","$('reconnect').addEventListener('click',boot);"),lifecycle);
vm.runInContext(`renderEditor=()=>{};renderRuleTargets=()=>{};renderResult=()=>{};clearErrors=()=>{};fail=()=>{};confirmAction=async()=>true;`,lifecycle);
const input = JSON.parse(fs.readFileSync(new URL('../examples/comparison.json',import.meta.url),'utf8'));
lifecycle.fixture = input;
const reset = () => vm.runInContext(`Object.assign(state,{config:{session_id:'s'},current:clone(fixture),draft:clone(fixture),tuple:{session_id:'s',revision:1,input_hash:'h'},revision:1,epoch:0,dirty:false,jsonDirty:false,validating:false,solving:false,job:null,clock:null,result:null,proposal:null,diffs:[]});`,lifecycle);
const inspect = () => vm.runInContext('structuredClone(state)',lifecycle);
reset();
let releaseSolve;
lifecycle.api = async (path, body, method) => {
  if (method==='DELETE') {deleted.push(path);return {};}
  if (path==='/api/solve') return new Promise(resolve=>{releaseSolve=resolve;});
  if (path==='/api/validate') return {input:body.input,tuple:{session_id:'s',revision:body.revision,input_hash:'new'}};
  throw new Error(`Unexpected call ${path}`);
};
const pendingSolve = lifecycle.solve();
assert.equal(inspect().solving,true);
await lifecycle.validateDraft(input);
assert.equal(inspect().solving,false);
assert.equal(inspect().job,null);
assert.equal(inspect().clock,null);
assert.equal(intervals.size,0);
releaseSolve({job_id:'late-job',tuple});
await pendingSolve;
assert.ok(deleted.includes('/api/jobs/late-job'));
assert.equal(inspect().result,null);
assert.equal(inspect().tuple.revision,2);

reset();intervals.add(99);
vm.runInContext(`state.solving=true;state.job='owned-job';state.clock=99;state.proposal={manual:true,input:clone(fixture),tuple:clone(state.tuple),value:{edits:[]}};`,lifecycle);
await lifecycle.applyProposal();
assert.equal(inspect().solving,false);
assert.equal(inspect().clock,null);
assert.equal(inspect().job,null);
assert.equal(intervals.size,0);
assert.ok(deleted.includes('/api/jobs/owned-job'));
assert.equal(inspect().tuple.revision,2);

// Invalid syntactically-valid imports never replace either copy of current work.
for (const invalid of ['null','{}','{"resources":[]}']) {
  reset();
  const before = inspect();
  lifecycle.api = async path => {assert.equal(path,'/api/validate');throw new Error('fields');};
  const target = {value:'invalid.json',files:[{size:invalid.length,text:async()=>invalid}]};
  await node('import-file').handlers.change({target});
  assert.equal(JSON.stringify(inspect().current),JSON.stringify(before.current));
  assert.equal(JSON.stringify(inspect().draft),JSON.stringify(before.draft));
  assert.equal(JSON.stringify(inspect().tuple),JSON.stringify(before.tuple));
  assert.equal(target.value,'');
  assert.equal(inspect().validating,false);
}
// Reset must adopt both credentials before the next validated request.
reset();
lifecycle.api = async (path,body) => {
  assert.equal(path,'/api/reset');
  assert.equal(body.session_id,'s');
  return {session_id:'rotated-session',csrf_token:'rotated-csrf'};
};
await node('reset').handlers.click();
assert.equal(inspect().config.session_id,'rotated-session');
assert.equal(inspect().config.csrf_token,'rotated-csrf');
assert.equal(inspect().current,null);
// Hosted mode must disclose server and model-provider processing before use.
lifecycle.AbortSignal=AbortSignal;
vm.runInContext('el=()=>({});',lifecycle);
for (const hosting of ['hosted','local']) {
  reset();
  lifecycle.fetch=async()=>({ok:true,json:async()=>({hosting,examples:[],session_id:'s',csrf_token:'t',model:{identity:'test-model',available:true}})});
  await lifecycle.boot();
  if(hosting==='hosted') {
    assert.match(node('session-note').textContent,/server memory/);
    assert.match(node('session-note').textContent,/Cloudflare Workers AI/);
    assert.doesNotMatch(node('model-state').textContent,/Local model|No remote/);
  } else {
    assert.match(node('session-note').textContent,/this machine/);
    assert.match(node('model-state').textContent,/No remote fallback/);
  }
}
console.log('19 pure boundary assertions and async validation, apply, stale-solve, invalid-import, reset and hosted privacy regressions passed. Browser rendering remains a separate check.');

const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createJobsPoller} = require('../../pauk/admin/static/jobs.js');

function fixture(initial = []) {
  let now = 0, version = 'job', responseGroups = initial, focused = false;
  let calls = 0, fail = false, pending = null;
  const select = {value: initial[0] || '', options: initial.map(value => ({value})),
    prepend(option) { this.options.unshift(option); }};
  const group = {contains: () => focused, querySelector: () => select,
    replaceChildren(node) { select.options = node.options.map(value => ({value})); select.value = select.options[0]?.value || ''; }};
  const other = {childNodes: [], contains: () => false, replaceChildren(){}, querySelectorAll: () => []};
  const marker = {dataset: {active: 'true', page: '1', groupsActive: 'job'}};
  const box = {replaceChildren(){}, append(){}, hidden:true};
  const elements = {'jobs-fragment':marker,'jobs-groups':group,'jobs-active':other,'jobs-history':other,
    'jobs-graph-banner':other,'jobs-refresh-status':box};
  const document = {hidden:false,activeElement:{},getElementById:id=>elements[id],addEventListener(){},
    createElement:()=>({}),createTextNode:s=>s};
  const env = {document,AbortController,Date:{now:()=>now},location:{search:'',pathname:'/jobs',hash:''},
    history:{state:null,replaceState(){}},setTimeout:()=>1,clearTimeout(){},
    DOMParser:class {parseFromString(body) { return {getElementById:id => {
      if (body === 'groups') return id === 'jobs-groups' ? {childNodes:[{options:responseGroups}],querySelector:()=>({})} : null;
      if (id === 'jobs-fragment') return {dataset:{active:'true',page:'1',groupsActive:version}};
      return other;
    }};}},
    fetch: async url => {
      if (url.startsWith('/jobs/groups')) {
        calls++;
        if (pending) return pending;
        if (fail) throw new Error('offline');
        return {ok:true,status:200,redirected:false,text:async()=> 'groups'};
      }
      return {ok:true,status:200,redirected:false,text:async()=> 'fragment'};
    }};
  return {env,select,setGroups:g=>{responseGroups=g;},setVersion:v=>{version=v;},
    time:t=>{now=t;},focus:v=>{focused=v;},failed:v=>{fail=v;},setPending:p=>{pending=p;},calls:()=>calls};
}

test('new group is loaded on collect completion and selection survives', async()=>{
  const f=fixture(['old']); const p=createJobsPoller(f.env); f.setGroups(['old','new']); f.setVersion('');
  await p.poll(); assert.equal(f.calls(),1); assert.equal(f.select.value,'old');
  assert.deepEqual(f.select.options.map(x=>x.value),['old','new']);
});

test('first group appears and removed choice is not silently changed', async()=>{
  const f=fixture(); const p=createJobsPoller(f.env); f.setGroups(['one']); f.setVersion('');
  await p.poll(); assert.equal(f.select.value,'one');
  f.setGroups(['two']); f.time(120000); await p.poll(); assert.equal(f.select.value,'');
  f.time(240000); await p.poll(); assert.equal(f.select.value,'');
});

test('focused select is preserved and updated on a later poll', async()=>{
  const f=fixture(['old']); const p=createJobsPoller(f.env); f.setGroups(['old','new']); f.setVersion('');
  f.focus(true); await p.poll(); assert.equal(f.calls(),0); f.focus(false); await p.poll(); assert.equal(f.calls(),1);
});

test('fallback refresh and failures are bounded per tab', async()=>{
  const a=fixture(), b=fixture(); const pa=createJobsPoller(a.env), pb=createJobsPoller(b.env);
  a.time(119999); await pa.poll(); assert.equal(a.calls(),0);
  a.time(120000); b.time(120000); a.setGroups(['new']); a.failed(true); await pa.poll(); await pb.poll();
  assert.deepEqual(a.select.options, []);
  assert.equal(a.calls(),1); assert.equal(b.calls(),1); await pa.poll(); assert.equal(a.calls(),1);
  a.time(240000); a.failed(false); await pa.poll(); assert.equal(a.calls(),2);
});

test('groups requests do not overlap', async()=>{
  const f=fixture(); const p=createJobsPoller(f.env); let finish;
  f.setPending(new Promise(resolve=>{finish=resolve;})); f.setVersion('');
  const first=p.poll(); await new Promise(resolve => setImmediate(resolve)); await p.poll();
  assert.equal(f.calls(),1); finish({ok:true,status:200,redirected:false,text:async()=> 'groups'}); await first;
});

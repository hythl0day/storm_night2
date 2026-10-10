const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {buildPayload, createClient} = require('../scripts/seedance');
const {selectModel, inferCapabilities} = require('../scripts/seedance-wrapper');
const retired = ['doubao-seedance-1.5-pro', 'doubao-seedance-1-5-pro-251215', 'doubao-seedance-1-0-pro-250528', 'DOUBAO-SEEDANCE-1.5-PRO'];
const active = {
  'doubao-seedance-2.0':'doubao-seedance-2-0-260128',
  'doubao-seedance-2.0-fast':'doubao-seedance-2-0-fast-260128',
  'doubao-seedance-2.0-mini':'doubao-seedance-2-0-mini-260615',
  'doubao-seedance-2.5':'doubao-seedance-2.5'
};
function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'seedance-retirement-'));
  for (const dir of ['scripts','references']) fs.cpSync(path.join(__dirname,'..',dir),path.join(root,dir),{recursive:true});
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  return root;
}
test('all retired IDs rejected before media reads and API submission', async()=>{
  const old=global.fetch; let calls=0;
  global.fetch=async()=>{calls++;throw Error('unexpected network');};
  try {
    const client=await createClient({apiKey:'ark-fixture-not-real'});
    for (const model of retired) {
      await assert.rejects(buildPayload({model,prompt:'test',imageFile:['/nonexistent/image.png']}),/已下线/);
      await assert.rejects(client.createTask({model,content:[]}),/已下线/);
    }
    assert.equal(calls,0);
  } finally {global.fetch=old;}
});
test('active models keep exact request IDs, frame roles, audio and search parameters',async()=>{
  for (const [model,id] of Object.entries(active)) {
    const p=await buildPayload({model,prompt:'test',duration:5,resolution:'720p',ratio:'adaptive',generateAudio:true,enableWebSearch:true,imageUrl:['https://example.com/1.png','https://example.com/2.png']});
    assert.equal(p.model,id); assert.equal(p.duration,5); assert.equal(p.generate_audio,true);
    assert.deepEqual(p.tools,[{type:'web_search'}]);
    assert.deepEqual(p.content.slice(1).map(x=>x.role),['first_frame','last_frame']);
    assert.equal((await buildPayload({model:id,prompt:'test'})).model,id);
    for (const opt of [{draft:true},{serviceTier:'flex'}]) await assert.rejects(buildPayload({model,prompt:'test',...opt}),/draft.*flex/);
  }
});
test('automatic standard, fast and 4K routes remain; impossible/draft/flex fail',()=>{
  assert.equal(selectModel(inferCapabilities([]),null,'test').model,'doubao-seedance-2.0');
  assert.equal(selectModel(inferCapabilities([]),'fast','test').model,'doubao-seedance-2.0-fast');
  assert.equal(selectModel(inferCapabilities(['--resolution','4k']),'fast','test').model,'doubao-seedance-2.0');
  for (const args of [['--draft','true'],['--service-tier','flex'],['--duration','30','--resolution','4k']]) assert.throws(()=>selectModel(inferCapabilities(args),null,'test'),/没有可用模型/);
});
test('saved active preferences survive; retired preference and override cannot restore retired model',t=>{
  const root=fixture(t), prefs=path.join(root,'.user-preference.json');
  const wrapper=require(path.join(root,'scripts/seedance-wrapper'));
  for (const model of Object.keys(active)) {
    fs.writeFileSync(prefs,JSON.stringify({user:{preferred_model:model}}));
    assert.equal(wrapper.selectModel(wrapper.inferCapabilities([]),null,'user').model,model);
  }
  for (const model of retired) {
    const original=JSON.stringify({user:{preferred_model:model}});
    fs.writeFileSync(prefs,original);
    fs.writeFileSync(path.join(root,'.user-model-overrides.json'),JSON.stringify({[model]:{...wrapper.loadModelMatrix()['doubao-seedance-2.0']}}));
    assert.ok(!Object.hasOwn(wrapper.loadModelMatrix(),model));
    const result=wrapper.selectModel(wrapper.inferCapabilities([]),null,'user');
    assert.equal(result.model,'doubao-seedance-2.0'); assert.match(result.change_reason.reason,/已下线/);
    assert.equal(fs.readFileSync(prefs,'utf8'),original);
  }
});
test('wrapper blocks explicit, equals flag, environment, payload and preference entrypoints locally',t=>{
  const root=fixture(t), payload=path.join(root,'payload.json');
  fs.writeFileSync(payload,JSON.stringify({model:retired[0],content:[{type:'text',text:'test'}]}));
  // A sentinel child guarantees this test can never submit a live generation.
  fs.writeFileSync(path.join(root,'scripts/seedance.js'),"console.error('UNEXPECTED_CHILD');process.exit(99);");
  for (const [args,env] of [
    [['--model',retired[0]],{}], [['--model='+retired[1]],{}],
    [[],{ARK_SEEDANCE_MODEL:retired[0]}], [['--payload-file',payload],{}],
    [['--save-model-preference',retired[0]],{}]
  ]) {
    const result=spawnSync(process.execPath,[path.join(root,'scripts/seedance-wrapper.js'),'create',...args],{encoding:'utf8',cwd:root,env:{PATH:process.env.PATH,ARK_SEEDANCE_SAVE_PATH:path.join(root,'downloads'),...env}});
    assert.notEqual(result.status,0);assert.match(result.stderr,/已下线/);assert.doesNotMatch(result.stderr,/UNEXPECTED_CHILD/);
  }
  assert.ok(!fs.existsSync(path.join(root,'.user-preference.json')));
  assert.ok(!fs.existsSync(path.join(root,'downloads')));
});
test('payload retirement check and active explicit override apply consistently',async t=>{
  const root=fixture(t), file=path.join(root,'payload.json');
  fs.writeFileSync(file,JSON.stringify({model:retired[0],content:[{type:'text',text:'test'}]}));
  await assert.rejects(buildPayload({payloadFile:file}),/已下线/);
  assert.equal((await buildPayload({payloadFile:file,model:'doubao-seedance-2.0'})).model,active['doubao-seedance-2.0']);
});
test('historical task lookup and list filtering remain available',async()=>{
  const old=global.fetch, urls=[];
  global.fetch=async(url,options)=>{urls.push(String(url));assert.equal(options.redirect,'error');return {ok:true,text:async()=>JSON.stringify({id:'cgt-history',model:retired[0],status:'succeeded'})};};
  try {
    const client=await createClient({apiKey:'ark-fixture-not-real'});
    assert.equal((await client.getTask('cgt-history')).model,retired[0]);
    await client.listTasks({model:retired[0]});
    assert.equal(urls.length,2);assert.ok(urls[1].includes('filter.model='));
  } finally {global.fetch=old;}
});

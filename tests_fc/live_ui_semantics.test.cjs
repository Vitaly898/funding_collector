'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const target = path.resolve(__dirname, '../funding_collector/live_ui/app.js');
const reply = (data, status=200) => ({ok:status<400,status,json:async()=>data});
const deferred = () => {let resolve; const promise=new Promise(r=>resolve=r);return {promise,resolve};};
test('production credential serializer preserves canonical uint64 vault exactly',()=>{
 const {serializeSetupSecret}=require(target);
 for(const vault of ['0','9007199254740993','18446744073709551615']){
  const data={api_key:'synthetic',stark_private_key:'0x1',stark_public_key:'0x2',vault};
  assert.deepEqual(JSON.parse(JSON.stringify(serializeSetupSecret(data,true))),data);
 }
 for(const vault of [true,1,'','01','+1','-1',' 1','1e3','١','18446744073709551616'])
  assert.throws(()=>serializeSetupSecret({vault},true));
 assert.equal(serializeSetupSecret({secret:'synthetic'},false),'synthetic');
});
test('production leg serializer rejects unsafe numeric IDs before Number conversion',()=>{
 const {serializeLeg}=require(target);
 const values={deployment:'lighter-core-mainnet',account:'synthetic',signer:'synthetic',asset_id:'9007199254740991',api_key_index:'4',symbol:'ETH',max_quantity:'1',max_notional:'1',max_slippage_bps:'1'};
 const serialize=()=>serializeLeg(name=>values[name]);
 assert.equal(serialize().asset_id,Number.MAX_SAFE_INTEGER);assert.equal(serialize().identity.api_key_index,4);
 for(const field of ['asset_id','api_key_index']){
  const before=values[field];
  for(const invalid of ['9007199254740992','9007199254740993','-1','1.1','','01','1e3']){
   values[field]=invalid;assert.throws(serialize);
  }
  values[field]=before;
 }
 values.deployment='extended-mainnet-starknet';values.api_key_index='';values.fee_rate_cap='0.001';
 assert.equal(serialize().identity.api_key_index,null);assert.equal(serialize().fee_rate_cap,'0.001');
});
test('delayed logout UI completion does not gate a newly authenticated session',async()=>{
 const {createClient,logoutAndRefresh}=require(target);const delayed=deferred();let failures=0;const gates=[];
 const client=createClient({onAuth:value=>gates.push(value),fetch:async(path)=>{
  if(path==='/api/auth/status')return reply({authenticated:false,csrf_token:'challenge'});
  if(path==='/api/auth/login')return reply({authenticated:true,csrf_token:'fresh-session'});
  if(path==='/api/auth/logout')return delayed.promise;
  return reply({running:false});
 }});
 await client.bootstrap();await client.login('old-password');
 const leaving=logoutAndRefresh(client,()=>{failures++;});
 await client.login('new-password');delayed.resolve(reply({authenticated:false}));await leaving;
 assert.equal(client.authenticated,true);assert.equal(gates.at(-1),true);assert.equal(failures,0);
 await client.mutate('lock',{});
});
test('single flight bootstrap and anonymous gate make no private request',async()=>{
 const {createClient}=require(target);const wait=deferred();const calls=[];
 const client=createClient({fetch:async(p,o)=>{calls.push([p,o]);return wait.promise;}});
 const a=client.bootstrap(), b=client.bootstrap();assert.equal(a,b);
 await assert.rejects(client.status(),/AUTH_REQUIRED/);
 assert.equal(calls.length,1);wait.resolve(reply({authenticated:false,csrf_token:'challenge'}));await a;
 assert.equal(calls[0][0],'/api/auth/status');assert.equal(calls[0][1].redirect,'error');assert.equal(calls[0][1].credentials,'same-origin');
});
test('late private 401 cannot erase re-login; logout fences late status',async()=>{
 const {createClient}=require(target);let pending;
 const client=createClient({fetch:async(p,o)=>{
  if(p==='/api/auth/status')return reply({authenticated:false,csrf_token:'challenge'});
  if(p==='/api/auth/login')return reply({authenticated:true,csrf_token:'session'});
  if(p==='/api/auth/logout')return reply({authenticated:false});
  pending=deferred();return pending.promise;
 }});
 await client.bootstrap();await client.login('synthetic-password');
 const old=client.status();await client.logout();await client.bootstrap();await client.login('new-password');
 pending.resolve(reply({error:'authentication_required'},401));
 await assert.rejects(old,/STALE/);assert.equal(client.authenticated,true);
 const late=client.status();await client.logout();pending.resolve(reply({running:true}));
 await assert.rejects(late,/STALE/);assert.equal(client.authenticated,false);
});
test('failed login preserves challenge for retry without page reload',async()=>{
 const {createClient}=require(target);let attempts=0;
 const client=createClient({fetch:async(p,o)=>{
  if(p==='/api/auth/status')return reply({authenticated:false,csrf_token:'challenge'});
  assert.equal(o.headers['X-CSRF-Token'],'challenge');
  return ++attempts===1?reply({error:'login_failed'},401):reply({authenticated:true,csrf_token:'session'});
 }});
 await client.bootstrap();await assert.rejects(client.login('wrong-password'));
 await client.login('synthetic-password');assert.equal(client.authenticated,true);
});
test('expired private request obtains fresh login challenge before retry',async()=>{
 const {createClient}=require(target);let checks=0;
 const client=createClient({fetch:async(p,o)=>{
  if(p==='/api/auth/status')return reply({authenticated:checks++===0,csrf_token:checks===1?'old-session':'fresh-challenge'});
  if(p==='/api/live/status')return reply({error:'expired'},401);
  assert.equal(o.headers['X-CSRF-Token'],'fresh-challenge');return reply({authenticated:true,csrf_token:'new-session'});
 }});
 await client.bootstrap();await assert.rejects(client.status());
 await client.login('synthetic-password');assert.equal(client.authenticated,true);
});

test('status polling single-flight, mutation csrf and allowlist',async()=>{
 const {createClient}=require(target);const wait=deferred();const calls=[];
 const client=createClient({fetch:async(p,o)=>{calls.push([p,o]);
  if(p==='/api/auth/status')return reply({authenticated:true,csrf_token:'csrf'});
  return wait.promise;
 }});
 await client.bootstrap();const a=client.status(),b=client.status();assert.equal(a,b);
 wait.resolve(reply({running:false}));await a;
 await assert.rejects(client.mutate('/evil',{}),/INVALID_PATH/);
 await client.mutate('lock',{});assert.equal(calls.at(-1)[1].headers['X-CSRF-Token'],'csrf');
});

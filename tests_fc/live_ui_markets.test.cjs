'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createMarketLoader,createClient}=require('../funding_collector/live_ui/app.js');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const HL='hyperliquid-mainnet-native',CORE='lighter-core-mainnet',RH='lighter-rh-mainnet',ENT='entropy-mainnet-io';
test('venue changes clear synchronously and late responses cannot replace either leg',async()=>{
 const waits=[],events=[];const loader=createMarketLoader({client:{markets:()=>{const w=deferred();waits.push(w);return w.promise;}},onChange:(n,s)=>events.push([n,s])});
 const old=loader.load(0,HL),other=loader.load(1,RH),latest=loader.load(0,ENT);
 assert.deepEqual(events.at(-1),[0,{status:'loading',rows:[]}]);
 waits[2].resolve({markets:[{symbol:'io:NVDA'}]});await latest;
 waits[1].resolve({markets:[{symbol:'ETH'}]});await other;
 const before=events.length;waits[0].resolve({markets:[{symbol:'OLD'}]});await old;
 assert.equal(events.length,before);assert.equal(events.at(-2)[1].rows[0].symbol,'io:NVDA');assert.equal(events.at(-1)[1].rows[0].symbol,'ETH');
});
test('reset fences old success/error and new session never reuses old in-flight metadata',async()=>{
 const waits=[],events=[];const loader=createMarketLoader({client:{markets:()=>{const w=deferred();waits.push(w);return w.promise;}},onChange:(n,s)=>events.push(s)});
 const old=loader.load(0,HL);loader.reset();
 const fresh=loader.load(0,HL);assert.equal(waits.length,2);
 waits[0].reject(Error('STALE'));await old;assert.equal(events.length,2);
 waits[1].resolve({markets:[{symbol:'ETH'}]});await fresh;assert.equal(events.at(-1).status,'ready');
});
test('empty and failed metadata are explicit and retry does not use stale rows',async()=>{
 let count=0;const events=[];
 const loader=createMarketLoader({client:{markets:async()=>{if(++count===1)throw Error('offline');return {markets:[]};}},onChange:(n,s)=>events.push(s)});
 await loader.load(0,HL);assert.deepEqual(events.at(-1),{status:'error',rows:[]});
 await loader.load(0,HL);assert.deepEqual(events.at(-1),{status:'empty',rows:[]});assert.equal(count,2);
});
test('actual loader/client GET only selected venue; Core down cannot gate HL/RH or Entropy',async()=>{
 const calls=[],events=[];
 const client=createClient({fetch:async(path,options)=>{
  calls.push([path,options]);
  if(path==='/api/auth/status')return {ok:true,status:200,json:async()=>({authenticated:true,csrf_token:'synthetic'})};
  const dep=path.split('/').at(-1);
  return {ok:dep!==CORE,status:dep===CORE?409:200,json:async()=>({markets:[{symbol:dep===ENT?'io:NVDA':'ETH'}]})};
 }});
 await client.bootstrap();calls.length=0;
 const loader=createMarketLoader({client,onChange:(n,s)=>events.push([n,s])});
 await Promise.all([loader.load(0,HL),loader.load(1,RH)]);
 assert.deepEqual(calls.map(c=>c[0]),['/api/live/markets/'+HL,'/api/live/markets/'+RH]);
 for(const [,options] of calls){assert.equal(options.method,undefined);assert.equal(options.body,undefined);assert.equal(options.cache,'no-store');}
 assert.deepEqual(events.slice(-2).map(([n,s])=>[n,s.status]),[[0,'ready'],[1,'ready']]);
 await loader.load(0,ENT);assert.equal(events.at(-1)[1].rows[0].symbol,'io:NVDA');
 await loader.load(1,CORE);assert.deepEqual(events.at(-1),[1,{status:'error',rows:[]}]);
 assert.equal(events.filter(([n,s])=>n===0&&s.status==='error').length,0);
});
test('only simultaneous requests for the same venue are shared',async()=>{
 const calls=[],w=deferred();const loader=createMarketLoader({client:{markets:dep=>{calls.push(dep);return w.promise;}},onChange:()=>{}});
 const a=loader.load(0,HL),b=loader.load(1,HL);assert.deepEqual(calls,[HL]);
 w.resolve({markets:[]});await Promise.all([a,b]);
});

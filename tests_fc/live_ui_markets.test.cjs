'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createMarketLoader}=require('../funding_collector/live_ui/app.js');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
test('venue changes clear synchronously and late responses cannot replace either leg',async()=>{
 const waits=[],events=[];const loader=createMarketLoader({client:{mutate:()=>{const w=deferred();waits.push(w);return w.promise;}},onChange:(n,s)=>events.push([n,s])});
 const old=loader.load(0,'hyperliquid-mainnet-native');
 const other=loader.load(1,'lighter-rh-mainnet');
 const latest=loader.load(0,'entropy-mainnet-io');
 assert.deepEqual(events.at(-1),[0,{status:'loading',rows:[]}]);
 waits[2].resolve({markets:[[{symbol:'io:NVDA'}],[]]});await latest;
 waits[1].resolve({markets:[[],[{symbol:'ETH'}]]});await other;
 const before=events.length;waits[0].resolve({markets:[[{symbol:'OLD'}],[]]});await old;
 assert.equal(events.length,before);assert.equal(events.at(-2)[1].rows[0].symbol,'io:NVDA');assert.equal(events.at(-1)[1].rows[0].symbol,'ETH');
});
test('reset fences old success/error and new session never reuses old in-flight metadata',async()=>{
 const waits=[],events=[];const loader=createMarketLoader({client:{mutate:()=>{const w=deferred();waits.push(w);return w.promise;}},onChange:(n,s)=>events.push(s)});
 const old=loader.load(0,'hyperliquid-mainnet-native');loader.reset();
 const fresh=loader.load(0,'hyperliquid-mainnet-native');assert.equal(waits.length,2);
 waits[0].reject(Error('STALE'));await old;assert.equal(events.length,2);
 waits[1].resolve({markets:[[{symbol:'ETH'}],[]]});await fresh;assert.equal(events.at(-1).status,'ready');
});
test('empty and failed metadata are explicit and retry does not use stale rows',async()=>{
 let count=0;const events=[];
 const loader=createMarketLoader({client:{mutate:async()=>{if(++count===1)throw Error('offline');return {markets:[[],[]]};}},onChange:(n,s)=>events.push(s)});
 await loader.load(0,'hyperliquid-mainnet-native');assert.deepEqual(events.at(-1),{status:'error',rows:[]});
 await loader.load(0,'hyperliquid-mainnet-native');assert.deepEqual(events.at(-1),{status:'empty',rows:[]});assert.equal(count,2);
});
const fs=require('node:fs');
const HL='hyperliquid-mainnet-native',CORE='lighter-core-mainnet',RH='lighter-rh-mainnet',ENT='entropy-mainnet-io';
test('automatic initial lists share one public catalog request; legs stay independent',async()=>{
 const html=fs.readFileSync(require('node:path').join(__dirname,'../funding_collector/live_ui/index.html'),'utf8');
 assert.doesNotMatch(html,/pair-search-|load-markets|Загрузите рынки/);
 const calls=[],events=[],wait=deferred();const client={mutate:(action,body)=>{calls.push([action,body]);return wait.promise;}};
 const loader=createMarketLoader({client,onChange:(n,state)=>events.push([n,state])});
 const a=loader.load(0,HL),b=loader.load(1,CORE);
 assert.deepEqual(calls,[['markets',{primary:HL,hedge:CORE}]]);
 assert.deepEqual(events.map(([n,s])=>[n,s.status]),[[0,'loading'],[1,'loading']]);
 wait.resolve({markets:[[{symbol:'ETH'}],[{symbol:'BTC'}]]});await Promise.all([a,b]);
 assert.deepEqual(events.slice(-2).map(([n,s])=>[n,s.rows[0].symbol]),[[0,'ETH'],[1,'BTC']]);
});

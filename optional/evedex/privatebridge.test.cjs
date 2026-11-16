'use strict';
const test=require('node:test'); const assert=require('node:assert/strict');
for(const name of ['node:http','node:https','node:net','node:tls']) {
 const m=require(name); for(const fn of ['request','get','connect','createConnection']) if(fn in m) m[fn]=()=>{throw Error('NETWORK BLOCKED');};
}
globalThis.fetch=()=>{throw Error('NETWORK BLOCKED');};
const key='0x'+'11'.repeat(32), signer='0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A';
const binding={deployment:'evedex-production-161803',chainId:161803,origin:'https://trading-api.evedex.com',account:'dummy',owner:'0x'+'22'.repeat(20),signer,instrument:'ETHUSD',leverage:1};
const id='00479:'+'a'.repeat(26);
test('private calls default disabled; registration validates existing OCT; real SDK IOC serialization',async()=>{
 const {PrivateBridge}=require('./privatebridge.cjs'); const requests=[];
 const http={authRequest:async r=>{requests.push(r); const p=new URL(r.url).pathname;
 if(p==='/api/user/me')return {data:{id:'dummy',wallet:binding.owner,email:'DO NOT PROJECT'}};
 if(p==='/api/user/oct/delegate')return {data:{list:[{delegate:signer}]}};
 if(p==='/api/v2/order/limit')return {data:{secret:'DO NOT PROJECT'}};
 throw Error('unexpected path');}};
 const b=new PrivateBridge({binding,mode:'trade',credentials:{api_key:'dummy-api',oct_private_key:key},token:'t'.repeat(64),httpClient:http});
 await assert.rejects(b.handle({op:'registration'}),/DISABLED/); assert.equal(requests.length,0);
 let seq=0; const ticket=(op,extra={})=>({token:'t'.repeat(64),nonce:require('node:crypto').randomBytes(16).toString('hex'),binding,seq:++seq,expires:Date.now()+1000,op,...extra});
 const reg=await b.handle({op:'registration',ticket:ticket('registration')}); assert.equal(reg.oct_registered,true); assert.equal(JSON.stringify(reg).includes('DO NOT'),false);
 const order={id,instrument:'ETHUSD',side:'sell',quantity:'0.01',limitPrice:'1800',leverage:1,kind:'IOC'};
 const ack=await b.handle({op:'submit',order,ticket:ticket('submit',{trade:true,reserved_id:id})});
 assert.deepEqual(ack,{id,status:'ACK_ONLY',confirmed_fill_base:null});
 const payload=JSON.parse(requests.at(-1).body); assert.equal(payload.timeInForce,'IOC'); assert.equal(payload.quantity,'0.01'); assert.equal(payload.limitPrice,'1800'); assert.equal(payload.chainId,'161803'); assert.match(payload.signature,/^0x[0-9a-f]{130}$/i); assert.equal(payload.postOnly,undefined);
 await assert.rejects(b.handle({op:'submit',order,ticket:ticket('submit',{trade:true,reserved_id:id})}),/DUPLICATE/);
});

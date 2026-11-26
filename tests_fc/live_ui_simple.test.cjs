'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
const app=require('../funding_collector/live_ui/app.js');
const fs=require('node:fs');
test('simple configuration whitelists real worker fields and hides metadata identifiers',()=>{
 const quote={observed_at:Date.now()/1000,max_age_seconds:60,base:'ETH',legs:[{symbol:'ETH',asset_id:17,reference:'3000',price:'2970'},{symbol:'ETH',asset_id:89,reference:'3000',price:'2985'}]};
 const identities=[{deployment:'hyperliquid-mainnet-native',account:'a',signer:'b',api_key_index:null},{deployment:'lighter-core-mainnet',account:'c',signer:'d',api_key_index:4}];
 const data={id:'one',quantity:'0.1',side:'buy',max_notional:'5000',max_slippage_bps:'200',lifetime:'120',poll_seconds:'1',auto_close:'yes',interval:'60'};
 const c=app.buildSimpleConfig(data,identities,quote);
 assert.deepEqual(Object.keys(c).sort(),['intent','legs','lifetime','poll_seconds','version']);
 assert.equal(c.legs[0].asset_id,17);assert.equal(c.legs[1].asset_id,89);
 assert.equal(c.intent.primary_price,'2970');assert.equal(c.intent.hedge_price,'2985');
 assert.equal(c.legs[0].max_quantity,'0.1');
 assert.ok(!JSON.stringify(c).includes('auto_close'));assert.ok(!JSON.stringify(c).includes('interval'));
 assert.throws(()=>app.buildSimpleConfig(data,identities,{...quote,observed_at:0}),/STALE/);
});
test('volume farming is a collapsed disabled placeholder outside execution form',()=>{
 const html=fs.readFileSync(require('node:path').join(__dirname,'../funding_collector/live_ui/index.html'),'utf8');
 const block=html.match(/<details id="volume-farming"[\s\S]*?<\/details>/)?.[0];
 assert.ok(block,'placeholder exists');assert.ok(!block.split('>')[0].includes('open'));
 assert.match(block,/fieldset disabled/);assert.match(block,/пока недоступно/);
 assert.match(block,/Auto-close/);assert.match(block,/Infinite loop/);assert.match(block,/Min \/ max delay/);
 assert.ok(!/<(?:input|select)[^>]*\bname=/.test(block));
});

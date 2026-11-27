const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
const {buildSimpleConfig}=require('../funding_collector/live_ui/app.js');
test('all live-supported venue selectors and Extended credential fields are wired',()=>{
 const source=fs.readFileSync(path.join(__dirname,'../funding_collector/live_ui/app.js'),'utf8');
 for(const venue of ['aster-mainnet','extended-mainnet-starknet'])assert.ok(source.includes("['"+venue+"',"));
 for(const field of ['api_key','stark_private_key','stark_public_key','vault','fee_rate_cap'])assert.ok(source.includes("'"+field+"'"));
 assert.ok(!source.includes('serializeSetupSecret(data,false)'));
});
test('simple Extended config preserves wide asset strings and explicit fee cap only on Extended',()=>{
 const common={quantity:'1',max_notional:'10000',max_slippage_bps:'50',id:'dynamic',side:'buy',lifetime:'60',poll_seconds:'1'};
 const identities=[{deployment:'extended-mainnet-starknet',account:'23',signer:'0x1',api_key_index:null},{deployment:'aster-mainnet',account:'x',signer:'y',api_key_index:null}];
 const q={observed_at:Date.now()/1000,max_age_seconds:60,legs:[{symbol:'XRP',asset_id:'9007199254740993',price:'2',reference:'2'},{symbol:'DOGE',asset_id:'123456789012345678901234567890',price:'1',reference:'1'}]};
 const result=buildSimpleConfig(common,identities,q,['0.0005',null]);
 assert.equal(result.legs[0].asset_id,'9007199254740993');assert.equal(result.legs[0].fee_rate_cap,'0.0005');
 assert.equal(result.legs[1].asset_id,q.legs[1].asset_id);assert.ok(!('fee_rate_cap' in result.legs[1]));
});

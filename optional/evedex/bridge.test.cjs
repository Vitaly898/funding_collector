'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {validateRequest, publicGateway} = require('./bridge.cjs');
test('public allowlist rejects auth, private, redirect origins and invalid book bounds', () => {
  assert.equal(validateRequest({method:'get', url:'https://trading-api.evedex.com/api/market/instrument'}), true);
  for (const url of ['https://trading-api.evedex.io/api/market/instrument','https://trading-api.evedex.com/api/user/me','https://evil.test/api/market/instrument','https://trading-api.evedex.com/api/market/ETHUSD/deep?maxLevel=1000&roundPrice=0.1']) {
    assert.throws(()=>validateRequest({method:'get',url}));
  }
  assert.throws(()=>validateRequest({method:'post',url:'https://trading-api.evedex.com/api/market/instrument'}));
  assert.throws(()=>validateRequest({method:'get',url:'https://trading-api.evedex.com/api/market/instrument',headers:{Authorization:'not-a-secret'}}));
});
test('real SDK imported and production gateway has no session; no network call',()=>{
 const {gateway, close} = publicGateway();
 assert.equal(gateway.options.exchangeURI,'https://trading-api.evedex.com');
 assert.equal(gateway.session,undefined);
 assert.equal(typeof gateway.fetchMarketDepth,'function');
 close();
});

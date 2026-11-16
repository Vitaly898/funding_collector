'use strict';
/** Credential-free, one-shot public bridge. SDK owns endpoint serialization. */
const sdk = require('@evedex/exchange-bot-sdk');
const axios = require('axios');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const ORIGIN = 'https://trading-api.evedex.com';
/** @param {{method?:string,url?:string,headers?:object}} request */
function validateRequest(request) {
  const u = new URL(request.url || '');
  const headers = Object.keys(request.headers || {}).map(k=>k.toLowerCase());
  if ((request.method || '').toUpperCase() !== 'GET' || u.origin !== ORIGIN || u.username || u.password || u.hash || headers.some(k=>['authorization','x-api-key','cookie'].includes(k))) throw Error('public allowlist refused');
  if (u.pathname === '/api/market/instrument' && !u.search) return true;
  if (/^\/api\/market\/[A-Z0-9_-]{1,30}\/deep$/.test(u.pathname) && [...u.searchParams.keys()].sort().join(',') === 'maxLevel,roundPrice' && u.searchParams.getAll('maxLevel').length === 1 && u.searchParams.get('maxLevel') === '100' && u.searchParams.getAll('roundPrice').length === 1 && u.searchParams.get('roundPrice') === '0.1') return true;
  throw Error('public path/query refused');
}
/** @param {string|undefined} artifacts */
function publicGateway(artifacts) {
  const records = [];
  const req = axios.interceptors.request.use(config=>{
    validateRequest(config);
    config.timeout = 10000; config.maxRedirects = 0;
    config.maxContentLength = 2*1024*1024; config.maxBodyLength = 0;
    config.headers['Cache-Control'] = 'no-cache';
    config.transformResponse = [body=>body];
    config.fcStarted = Date.now()/1000;
    return config;
  });
  function record(response, error) {
    if (!response) return;
    const body = typeof response.data === 'string' ? response.data : JSON.stringify(response.data);
    const r = {url:response.config.url, requested_at_unix_s:response.config.fcStarted,
      received_at_unix_s:Date.now()/1000, status:response.status, headers:response.headers.toJSON(),body:body.slice(0,2*1024*1024), error};
    records.push(r);
    if (artifacts) {fs.mkdirSync(artifacts,{recursive:true});fs.writeFileSync(path.join(artifacts,'evedex-'+crypto.randomUUID()+'.json'),JSON.stringify(r,null,2));}
  }
  const res = axios.interceptors.response.use(response=>{
    record(response);
    response.data = JSON.parse(response.data);
    return response;
  },error=>{record(error.response,'public HTTP failure');throw Error('EVEDEX public transport failed: '+(error.response?.status || error.code || 'unknown'));});
  const container = new sdk.ProdContainer({wallets:{},apiKeys:{}},false);
  return {gateway:container.gateway(),records,close(){axios.interceptors.request.eject(req);axios.interceptors.response.eject(res);container.closeWsConnection();}};
}
/** @param {{op:string,symbol?:string,artifacts:string}} input */
async function run(input) {
  if (!input || !['catalog','snapshot'].includes(input.op) || Object.keys(input).some(k=>!['op','symbol','artifacts'].includes(k)) || typeof input.artifacts !== 'string' || input.artifacts.length>4096) throw Error('invalid bounded public operation');
  if (input.op==='snapshot' && (typeof input.symbol!=='string' || !/^[A-Z0-9_-]{1,30}$/.test(input.symbol))) throw Error('exact symbol required');
  const client = publicGateway(input.artifacts);
  try {
    const instruments = await client.gateway.fetchInstruments();
    let book = null;
    if (input.op==='snapshot') {
      if (!Array.isArray(instruments) || instruments.filter(i=>i.name===input.symbol).length!==1) throw Error('exact instrument missing/ambiguous');
      book = await client.gateway.fetchMarketDepth({instrument:input.symbol,maxLevel:100,roundPrice:sdk.OrderBookRoundPrices.OneTenth});
    }
    return {sdk_version:'1.2.8',environment:'production',exchange_uri:ORIGIN,instruments,book,records:client.records,trading_enabled:false};
  } finally {client.close();}
}
module.exports={validateRequest,publicGateway,run};
if (require.main===module) {
  let input='';
  const deadline=setTimeout(()=>{process.stderr.write('public bridge deadline\n');process.exit(2);},25000);
  process.stdin.setEncoding('utf8');
  process.stdin.on('data',chunk=>{input+=chunk;if(input.length>8192)process.exit(2);});
  process.stdin.on('end',async()=>{try {process.stdout.write(JSON.stringify(await run(JSON.parse(input))));}catch {process.stderr.write('EVEDEX public bridge refused or unavailable; inspect public artifacts\n');process.exitCode=2;}finally{clearTimeout(deadline);}});
}

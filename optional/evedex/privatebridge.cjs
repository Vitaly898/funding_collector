'use strict';
// Typed child-only bridge. No registration, withdrawal, leverage or arbitrary HTTP API.
const crypto=require('node:crypto');
const util=require('node:util');
const path=require('node:path');
const sdk=require('@evedex/exchange-bot-sdk');
const api=require('@evedex/exchange-api');
const Big=require('big.js');
const sdkRoot=path.dirname(require.resolve('@evedex/exchange-bot-sdk'));
const {Wallet}=require(path.join(sdkRoot,'utils/wallet.js'));
const ORIGIN='https://trading-api.evedex.com';
const orderID=/^[0-9]{5}:[0-9a-fA-F]{26}$/;
function amount(x,positive=false){if(!/^-?\d+(\.\d+)?$/.test(String(x)))throw Error('SCHEMA');const n=new Big(String(x));if(positive&&n.lte(0))throw Error('SCHEMA');return n.toFixed();}
function eq(a,b){return JSON.stringify(Object.keys(a).sort().map(k=>[k,a[k]]))===JSON.stringify(Object.keys(b).sort().map(k=>[k,b[k]]));}
class PrivateBridge{
 #binding;#token;#mode;#gateway;#account;#wallet;#seq=0;#nonces=new Set();#ids=new Set();#registered=false;
 constructor({binding,credentials,token,mode='read',httpClient}){
  if(!binding||binding.deployment!=='evedex-production-161803'||binding.chainId!==161803||binding.origin!==ORIGIN||!['ETHUSD','BTCUSD'].includes(binding.instrument)||binding.leverage!==1||!/^0x[0-9a-fA-F]{40}$/.test(binding.owner)||!/^0x[0-9a-fA-F]{40}$/.test(binding.signer)||binding.owner.toLowerCase()===binding.signer.toLowerCase()||typeof binding.account!=='string'||!binding.account||binding.account.length>80||!['read','trade'].includes(mode)||typeof token!=='string'||token.length!==64||!credentials||typeof credentials.api_key!=='string'||!credentials.api_key||credentials.api_key.length>512)throw Error('BINDING');
  this.#binding=Object.freeze({...binding});this.#token=token;this.#mode=mode;
  this.#wallet=new Wallet({privateKey:credentials.oct_private_key,chain:'161803'});
  const key=credentials.api_key;
  const upstream=httpClient||{authRequest:async r=>{
   const axios=require('axios');
   const response=await axios.request({url:r.url,method:r.method,headers:{...r.headers,'x-api-key':key},data:r.body,timeout:10000,maxRedirects:0,maxContentLength:2000000,maxBodyLength:16384,proxy:false,validateStatus:s=>s===200||s===201});
   return {data:response.data};
  }};
  const guarded={authRequest:async r=>{
   const u=new URL(r.url);const p=u.pathname;
   const get=r.method==='GET'&&(new Set(['/api/user/me','/api/user/oct/delegate','/api/position','/api/order/opened']).has(p)||/^\/api\/order\/[0-9]{5}:[0-9a-fA-F]{26}(\/fill)?$/.test(p));
   if(u.origin!==ORIGIN||u.search||u.hash||u.username||u.password||!(get||r.method==='POST'&&p==='/api/v2/order/limit'))throw Error('ROUTE');
   try{return await upstream.authRequest(r);}catch(_){throw Error('UPSTREAM_UNAVAILABLE');}
  }};
  this.#gateway=new api.ExchangeRestGateway({exchangeURI:ORIGIN,httpClient:guarded});
  this.#account=new sdk.WalletAccount({wallet:this.#wallet,gateway:{exchangeGateway:this.#gateway},exchangeAccount:{id:binding.account}});
 }
 [util.inspect.custom](){return '<EvedexPrivateBridge credentials=REDACTED>';}
 toJSON(){return {trading_enabled:false,credentials:'REDACTED'};}
 #ticket(op,t){
  const now=Date.now();
  if(!t||typeof t.token!=='string'||t.token.length!==this.#token.length||!crypto.timingSafeEqual(Buffer.from(t.token),Buffer.from(this.#token))||t.op!==op||!Number.isSafeInteger(t.seq)||t.seq!==this.#seq+1||!Number.isSafeInteger(t.expires)||t.expires<now||t.expires>now+5000||!t.binding||!eq(t.binding,this.#binding)||!/^[a-f0-9]{32}$/.test(t.nonce)||this.#nonces.has(t.nonce)||this.#nonces.size>=10000)throw Error('DISABLED');
  this.#seq=t.seq;this.#nonces.add(t.nonce);
 }
 async #registration(){
  const me=await this.#gateway.me();
  const delegates=await this.#gateway.authGet('/api/user/oct/delegate');
  const address=await this.#wallet.getAddress();
  if(me.id!==this.#binding.account||typeof me.wallet!=='string'||me.wallet.toLowerCase()!==this.#binding.owner.toLowerCase()||address.toLowerCase()!==this.#binding.signer.toLowerCase()||!Array.isArray(delegates.list)||delegates.list.filter(d=>typeof d.delegate==='string'&&d.delegate.toLowerCase()===address.toLowerCase()).length!==1){this.#registered=false;throw Error('REGISTRATION');}
  this.#registered=true;return {account_verified:true,oct_registered:true};
 }
 #rows(raw){const rows=Array.isArray(raw)?raw:raw?.list;if(!Array.isArray(rows)||rows.length>1000||(raw.total!==undefined&&raw.total!==rows.length))throw Error('INCOMPLETE');return rows;}
 #scope(row){if(row.user!==this.#binding.account||row.instrument!==this.#binding.instrument)throw Error('ROW_SCOPE');}
 async handle(request){
  const {op,ticket}=request||{};this.#ticket(op,ticket);
  if(!['registration','positions','open_orders','order_status','fills','submit'].includes(op))throw Error('DISABLED');
  if(op==='registration')return await this.#registration();
  if(!this.#registered)throw Error('REGISTRATION_REQUIRED');
  if(op==='submit'){
   const o=request.order;
   if(this.#mode!=='trade'||ticket.trade!==true||!o||ticket.reserved_id!==o.id||!orderID.test(o.id)||o.instrument!==this.#binding.instrument||o.leverage!==this.#binding.leverage||!['buy','sell'].includes(o.side)||!['LIMIT','IOC'].includes(o.kind)||Object.keys(o).sort().join(',')!=='id,instrument,kind,leverage,limitPrice,quantity,side')throw Error('DISABLED');
   if(this.#ids.has(o.id))throw Error('DUPLICATE');
   const qty=amount(o.quantity,true),price=amount(o.limitPrice,true);
   const min=o.instrument==='ETHUSD'?'0.01':'0.001';
   if(new Big(qty).lt(min)||!new Big(qty).mod('0.001').eq(0))throw Error('SIZE');
   // Recheck delegation before every new signature. WAL reservation is owned by Python.
   await this.#registration();this.#ids.add(o.id);
   const signed=await this.#account.signLimitOrderV2({id:o.id,instrument:o.instrument,side:o.side==='buy'?sdk.Side.Buy:sdk.Side.Sell,quantity:qty,limitPrice:price,leverage:o.leverage,postOnly:o.kind==='LIMIT'});
   const payload={...signed,timeInForce:o.kind==='IOC'?'IOC':'GTC'};
   try{await this.#gateway.createLimitOrderV2(payload);return {id:o.id,status:'ACK_ONLY',confirmed_fill_base:null};}
   catch(_){return {id:o.id,status:'UNKNOWN',confirmed_fill_base:null};}
  }
  if(op==='positions')return this.#rows(await this.#gateway.getPositions()).map(r=>{this.#scope(r);if(!['buy','sell'].includes(r.side))throw Error('SIDE');return {symbol:this.#binding.instrument.replace('USD',''),quantity:new Big(amount(r.quantity,true)).times(r.side==='buy'?1:-1).toFixed()};});
  if(op==='open_orders')return this.#rows(await this.#gateway.getOpenedOrders()).map(r=>{this.#scope(r);if(!orderID.test(r.id))throw Error('ORDER_ID');return {id:r.id};});
  if(!orderID.test(request.order_id))throw Error('ORDER_ID');
  if(op==='order_status'){
   const r=await this.#gateway.getOrder(request.order_id);this.#scope(r);if(r.id!==request.order_id)throw Error('ORDER_ID');
   const qty=amount(r.quantity,true),remaining=amount(r.unFilledQuantity);
   if(new Big(remaining).lt(0)||new Big(remaining).gt(qty))throw Error('QUANTITY');
   const status={filled:'filled',new:'open',partiallyFilled:'open',cancelled:'canceled',canceled:'canceled',rejected:'rejected',expired:'expired'}[r.status];
   if(!status)throw Error('ORDER_STATUS');
   return {order_id:r.id,status,filled_base:new Big(qty).minus(remaining).toFixed()};
  }
  const rows=this.#rows(await this.#gateway.authGet('/api/order/'+request.order_id+'/fill'));
  const ids=new Set();return rows.map(r=>{
   if(typeof r.id!=='string'||!r.id||ids.has(r.id)||!Array.isArray(r.fee)||r.fee.length!==1||!['USDT','USDC','USD'].includes(r.fee[0].coin))throw Error('FILL');ids.add(r.id);
   return {id:r.id,quantity:amount(r.fillQuantity,true),price:amount(r.fillPrice,true),fee:amount(r.fee[0].quantity),fee_currency:r.fee[0].coin};
  });
 }
}
module.exports={PrivateBridge};

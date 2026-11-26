'use strict';
(function(root){
function createClient({fetch:fetcher=root.fetch.bind(root),onAuth=()=>{}}={}){
 let authenticated=false,csrf='',generation=0,boot=null,poll=null;
 const allowed=new Set(['configure','setup','check','start','lock']);
 function reset(){authenticated=false;csrf='';boot=null;poll=null;onAuth(false);}
 async function request(path,body,epoch=generation,token=csrf){
  const options={credentials:'same-origin',redirect:'error',cache:'no-store',headers:{}};
  if(body!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':token};options.body=JSON.stringify(body);}
  const response=await fetcher(path,options);
  const data=await response.json();
  if(epoch!==generation)throw Error('STALE');
  if(response.status===401){if(path==='/api/auth/login')throw Error('LOGIN_FAILED');generation++;reset();throw Error('AUTH_REQUIRED');}
  if(!response.ok)throw Error(response.status===429?'RATE_LIMITED':'OPERATION_REFUSED');
  return data;
 }
 function bootstrap(){
  if(boot)return boot;
  const epoch=generation;
  const pending=request('/api/auth/status',undefined,epoch).then(data=>{
   authenticated=data.authenticated===true;csrf=data.csrf_token||'';onAuth(authenticated);return data;
  }).finally(()=>{if(boot===pending)boot=null;});
  boot=pending;return pending;
 }
 async function login(password){
  if(!csrf)await bootstrap();
  generation++;poll=null;const epoch=generation;
  const data=await request('/api/auth/login',{password},epoch);
  authenticated=true;csrf=data.csrf_token;onAuth(true);return data;
 }
 async function logout(){
  const token=csrf;generation++;const epoch=generation;reset();
  return request('/api/auth/logout',{},epoch,token);
 }
 function status(){
  if(!authenticated)return Promise.reject(Error('AUTH_REQUIRED'));
  if(poll)return poll;
  const pending=request('/api/live/status').finally(()=>{if(poll===pending)poll=null;});poll=pending;return pending;
 }
 async function mutate(action,body){
  if(!allowed.has(action))throw Error('INVALID_PATH');
  if(!authenticated)throw Error('AUTH_REQUIRED');
  return request('/api/live/'+action,body);
 }
 return {bootstrap,login,logout,status,mutate,get authenticated(){return authenticated;},get generation(){return generation;}};
}
function decimalInteger(value,max){
 if(typeof value!=='string'||!/^(0|[1-9][0-9]*)$/.test(value)||value.length>max.length||value.length===max.length&&value>max)throw Error('INVALID_INTEGER');
 return value;
}
function safeInteger(value){return Number(decimalInteger(value,'9007199254740991'));}
function serializeSetupSecret(data,extended){
 return extended?{api_key:data.api_key,stark_private_key:data.stark_private_key,stark_public_key:data.stark_public_key,vault:decimalInteger(data.vault,'18446744073709551615')}:data.secret;
}
function serializeLeg(get){
 const deployment=get('deployment');
 const leg={identity:{deployment,account:get('account'),signer:get('signer'),api_key_index:deployment.startsWith('lighter-')?safeInteger(get('api_key_index')):null},symbol:get('symbol'),asset_id:safeInteger(get('asset_id')),max_quantity:get('max_quantity'),max_notional:get('max_notional'),max_slippage_bps:get('max_slippage_bps')};
 if(deployment==='extended-mainnet-starknet')leg.fee_rate_cap=get('fee_rate_cap');return leg;
}
async function logoutAndRefresh(client,onFailure){
 try{await client.logout();await client.bootstrap();}
 catch(error){if(error.message!=='STALE'&&!client.authenticated)onFailure();}
}
if(typeof module!=='undefined'&&module.exports)module.exports={createClient,serializeSetupSecret,serializeLeg,logoutAndRefresh};
if(typeof document==='undefined')return;
const $=id=>document.getElementById(id);
const sensitive=()=>document.querySelectorAll('input[type=password]');
function clearSecrets(){sensitive().forEach(el=>{el.value='';});}
let timer=null,uiEpoch=0;
function gate(auth){
 uiEpoch++;clearSecrets();$('login-panel').hidden=auth;$('workspace').hidden=!auth;$('logout').hidden=!auth;
 clearTimeout(timer);
 if(!auth){$('ledger').replaceChildren();$('accounts').replaceChildren();$('summary').textContent='Исполнение заблокировано';$('message').textContent='';}
}
const client=createClient({onAuth:gate});
const venues=[['hyperliquid-mainnet-native','Hyperliquid · native'],['lighter-core-mainnet','Lighter · Core'],['lighter-rh-mainnet','Lighter · RH'],['aster-mainnet','Aster'],['extended-mainnet-starknet','Extended · Starknet'],['entropy-mainnet-io','Entropy · io:NVDA']];
const fields=[['account','Аккаунт / owner address'],['signer','Выделенный signer / Stark public key'],['symbol','Приватный базовый символ'],['asset_id','Asset / market ID'],['api_key_index','API key index (только Lighter)'],['max_quantity','Лимит количества (base)'],['max_notional','Лимит номинала'],['max_slippage_bps','Лимит отклонения (bps)'],['fee_rate_cap','Extended fee rate cap (доля)']];
function input(label,name,{password=false,required=true,value=''}={}){
 const wrap=document.createElement('label');wrap.textContent=label;
 const el=document.createElement('input');el.name=name;el.type=password?'password':'text';el.required=required;el.value=value;
 if(password){el.autocomplete='off';el.maxLength=1024;}else el.autocomplete='off';
 wrap.append(el);return wrap;
}
for(let n=0;n<2;n++){
 const box=$('leg-'+n);const wrap=document.createElement('label');wrap.textContent='Развёртывание';
 const select=document.createElement('select');select.name='deployment';
 venues.forEach(([value,label])=>{const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);});
 select.value=n?'lighter-core-mainnet':'hyperliquid-mainnet-native';wrap.append(select);box.append(wrap);
 fields.forEach(([name,label])=>box.append(input(label,name,{required:!['api_key_index','fee_rate_cap'].includes(name)})));
 const setup=$('setup-'+n);
 setup.append(input('Выделенный execution key (никогда owner-wallet key)', 'secret',{password:true}));
 for(const [name,label]of [['api_key','Extended API key'],['stark_private_key','Extended Stark execution key'],['stark_public_key','Extended Stark public key'],['vault','Extended signing vault ID']]){
  const el=input(label,name,{password:true,required:false});el.dataset.extended='true';setup.append(el);
 }
 setup.append(input('Новая локальная passphrase (не менее 16 символов)','passphrase',{password:true}));
 const button=document.createElement('button');button.type='submit';button.textContent='Зашифровать ключ '+(n===0?'primary':'hedge');setup.append(button);
 function conditional(){
  const extended=select.value==='extended-mainnet-starknet';
  box.querySelector('[name=fee_rate_cap]').parentElement.hidden=!extended;
  box.querySelector('[name=fee_rate_cap]').required=extended;
  box.querySelector('[name=api_key_index]').parentElement.hidden=!select.value.startsWith('lighter-');
  setup.querySelector('[name=secret]').parentElement.hidden=extended;setup.querySelector('[name=secret]').required=!extended;
  setup.querySelectorAll('[data-extended]').forEach(label=>{label.hidden=!extended;label.querySelector('input').required=extended;});
 }
 select.addEventListener('change',conditional);conditional();
}
function values(form){return Object.fromEntries(new FormData(form));}
function take(form){const data=values(form);form.querySelectorAll('input[type=password]').forEach(el=>el.value='');return data;}
function textTable(headers,rows){
 const table=document.createElement('table'),thead=document.createElement('thead'),tr=document.createElement('tr');
 headers.forEach(v=>{const th=document.createElement('th');th.textContent=v;tr.append(th);});thead.append(tr);table.append(thead);
 const tbody=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(value=>{const td=document.createElement('td');td.textContent=String(value??'—');tr.append(td);});tbody.append(tr);});table.append(tbody);return table;
}
function render(row){
 $('summary').textContent=(row.running?'Работает':'Заблокировано')+' · '+(row.trading_enabled?'authority активна':'authority закрыта')+' · '+(row.checked?'аккаунты проверены':'нужна свежая проверка');
 $('vault-state').textContent=(row.vaults||[]).map((v,n)=>(n?'Hedge':'Primary')+': '+(v.configured?'ключ зашифрован':'ключ не настроен')).join(' · ');
 const out=$('ledger');out.replaceChildren();const s=row.state;
 if(!s){out.textContent='Нет сохранённой задачи. Здесь появятся подтверждённые сделки, комиссии и журнал.';return;}
 const title=document.createElement('h3');title.textContent=(s.intent?.id||'')+' · '+s.status+' / '+s.phase;out.append(title);
 const p=document.createElement('p');p.textContent='Primary '+s.primary_filled+' · Hedge '+s.hedge_filled+' · Остаток '+s.residual_base+' · Комиссии '+Object.entries(s.fees_by_currency||{}).map(([k,v])=>v+' '+k).join(', ');out.append(p);
 const stamp=document.createElement('p');stamp.className='muted';stamp.textContent='Сохранённое наблюдение: '+(s.observed_at==null?'время неизвестно':new Date(s.observed_at*1000).toLocaleString('ru-RU'))+'. Это не текущая связь с биржей.';out.append(stamp);
 const fills=Object.entries(s.fills||{}).flatMap(([leg,rows])=>rows.map(f=>[leg,f.id,f.quantity,f.price,f.fee+' '+f.fee_currency]));
 out.append(textTable(['Нога','Trade ID','Количество','Цена','Комиссия'],fills));
 out.append(textTable(['№','Время','Событие'],(s.journal||[]).map(e=>[e.sequence,new Date(e.at*1000).toLocaleTimeString('ru-RU'),e.event])));
}
function renderAccounts(row){
 const out=$('accounts');out.replaceChildren();
 (row.accounts||[]).forEach((a,n)=>{
  const h=document.createElement('h3');h.textContent=(n?'Hedge':'Primary')+' · регистрация '+(a.registration_valid?'подтверждена':'не подтверждена')+' · открытых ордеров: '+a.open_order_count;out.append(h);
  if(a.positions.length)out.append(textTable(['Фактическая позиция','Количество'],a.positions.map(p=>[p.symbol,p.quantity])));
  else {const p=document.createElement('p');p.textContent='Фактических позиций нет.';out.append(p);}
 });
 const time=document.createElement('p');time.className='muted';time.textContent='Readonly-проверка: '+new Date().toLocaleString('ru-RU')+'. Оба signer закрыты после чтения.';out.append(time);
}
async function poll(){
 const epoch=uiEpoch;
 try {const row=await client.status();if(epoch===uiEpoch)render(row);}catch(error){if(error.message!=='STALE'&&client.authenticated)$('message').textContent='Не удалось обновить статус. Проверьте соединение.';}
 if(epoch===uiEpoch&&client.authenticated)timer=setTimeout(poll,2000);
}
async function action(form,fn){
 const epoch=uiEpoch;const buttons=form.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);$('message').textContent='Выполняется…';
 try{const result=await fn();if(epoch===uiEpoch){if(result?.mode)render(result);$('message').textContent='Операция выполнена.';}}
 catch(error){if(error.message!=='STALE'&&epoch===uiEpoch)$('message').textContent=error.message==='RATE_LIMITED'?'Слишком часто. Подождите минуту.':'Операция отклонена. Проверьте поля и доступ. Секреты не выводятся.';}
 finally{form.querySelectorAll('input[type=password]').forEach(el=>el.value='');buttons.forEach(b=>b.disabled=false);}
}
$('login').addEventListener('submit',event=>{
 event.preventDefault();const data=take(event.currentTarget);
 action(event.currentTarget,async()=>{await client.login(data.password);poll();});
});
$('logout').addEventListener('click',async()=>{
 clearSecrets();await logoutAndRefresh(client,()=>{gate(false);$('message').textContent='Связь потеряна. Authority ограничена TTL; проверьте сервер.';});
});
$('config').addEventListener('submit',event=>{
 event.preventDefault();const common=values(event.currentTarget);
 action(event.currentTarget,()=>{
 const legs=[0,1].map(n=>{
  const box=$('leg-'+n),get=name=>box.querySelector('[name='+name+']').value;
  return serializeLeg(get);
 });
 const intent={};for(const key of ['id','quantity','side','primary_price','hedge_price','primary_reference','hedge_reference'])intent[key]=common[key];
 return client.mutate('configure',{config:{version:1,legs,intent,lifetime:Number(common.lifetime),poll_seconds:Number(common.poll_seconds)}});
 });
});
for(let n=0;n<2;n++)$('setup-'+n).addEventListener('submit',event=>{
 event.preventDefault();const data=take(event.currentTarget);const extended=$('leg-'+n).querySelector('[name=deployment]').value==='extended-mainnet-starknet';
 action(event.currentTarget,()=>client.mutate('setup',{leg:n,secret:serializeSetupSecret(data,extended),passphrase:data.passphrase}));
});
for(const name of ['check','start'])$(name).addEventListener('submit',event=>{
 event.preventDefault();const data=take(event.currentTarget),body={passphrases:[data.primary_passphrase,data.hedge_passphrase]};
 if(name==='start')body.confirmation=data.confirmation;
 action(event.currentTarget,async()=>{const row=await client.mutate(name,body);if(name==='check')renderAccounts(row);return row;});
});
$('lock').addEventListener('click',()=>action($('lock-form'),()=>client.mutate('lock',{})));
clearSecrets();client.bootstrap().then(()=>{if(client.authenticated)poll();}).catch(()=>{$('message').textContent='Сервер недоступен. Обновите страницу.';});
})(typeof window!=='undefined'?window:globalThis);

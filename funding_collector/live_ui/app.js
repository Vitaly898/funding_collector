'use strict';
(function(root){
function createClient({fetch:fetcher=root.fetch.bind(root),onAuth=()=>{}}={}){
 let authenticated=false,csrf='',generation=0,boot=null,poll=null;
 const allowed=new Set(['configure','setup','check','start','lock','markets','quote']);
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
function buildSimpleConfig(common,identities,quote){
 if(!quote||!Number.isFinite(quote.observed_at)||Date.now()/1000-quote.observed_at<0||Date.now()/1000-quote.observed_at>quote.max_age_seconds)throw Error('STALE_QUOTE');
 if(!Array.isArray(quote.legs)||quote.legs.length!==2)throw Error('INVALID_PAIR');
 const legs=quote.legs.map((leg,n)=>({identity:identities[n],symbol:leg.symbol,asset_id:leg.asset_id,max_quantity:common.quantity,max_notional:common.max_notional,max_slippage_bps:common.max_slippage_bps}));
 return {version:1,legs,intent:{id:common.id,quantity:common.quantity,side:common.side,primary_price:quote.legs[0].price,hedge_price:quote.legs[1].price,primary_reference:quote.legs[0].reference,hedge_reference:quote.legs[1].reference},lifetime:Number(common.lifetime),poll_seconds:Number(common.poll_seconds)};
}
if(typeof module!=='undefined'&&module.exports)module.exports={createClient,serializeSetupSecret,serializeLeg,logoutAndRefresh,buildSimpleConfig};
if(typeof document==='undefined')return;
const $=id=>document.getElementById(id);
const sensitive=()=>document.querySelectorAll('input[type=password]');
function clearSecrets(){sensitive().forEach(el=>{el.value='';});}
let timer=null,uiEpoch=0;
function gate(auth){
 uiEpoch++;clearSecrets();$('login-panel').hidden=auth;$('workspace').hidden=!auth;$('logout').hidden=!auth;
 clearTimeout(timer);
 if(!auth){$('strategy-modal').close();$('strategy-list').replaceChildren();cardsSignature='';$('strategy-detail').hidden=true;$('strategies').hidden=false;invalidate(true);$('ledger').replaceChildren();$('accounts').replaceChildren();$('summary').textContent='Исполнение заблокировано';$('message').textContent='';}
}
const client=createClient({onAuth:gate});
const venues=[['hyperliquid-mainnet-native','Hyperliquid · native'],['lighter-core-mainnet','Lighter · Core'],['lighter-rh-mainnet','Lighter · RH'],['entropy-mainnet-io','Entropy · io']];
const fields=[['account','Аккаунт / owner address'],['signer','Выделенный signer / public key'],['api_key_index','API key index · только Lighter']];
function input(label,name,{password=false,required=true,value=''}={}){
 const wrap=document.createElement('label');wrap.textContent=label;
 const el=document.createElement('input');el.name=name;el.type=password?'password':'text';el.required=required;el.value=value;
 if(password){el.autocomplete='off';el.maxLength=1024;}else el.autocomplete='off';
 wrap.append(el);return wrap;
}
let marketRows=[[],[]],quoteData=null,marketGeneration=0;
function invalidate(markets=false){
 marketGeneration++;quoteData=null;$('save-config').disabled=true;$('quote-status').textContent='Цена ещё не рассчитана.';
 if(markets){marketRows=[[],[]];for(let n=0;n<2;n++){$('pair-select-'+n).replaceChildren(new Option('Загрузите рынки',''));$('instrument-'+n).textContent='Инструмент не выбран.';}$('market-status').textContent='Выбор бирж изменён. Обновите рынки.';}
}
const deploymentPair=()=>({primary:$('primary-deployment').value,hedge:$('hedge-deployment').value});
function instrumentSummary(n){const row=marketRows[n].find(r=>r.symbol===$('pair-select-'+n).value);$('instrument-'+n).textContent=row?row.label+' · base '+row.base+' · '+row.units:'Инструмент не выбран.';}
function pairOptions(n){
 const select=$('pair-select-'+n),selected=select.value,search=$('pair-search-'+n).value.trim().toUpperCase();
 select.replaceChildren(new Option('Выберите инструмент этой биржи',''));
 marketRows[n].filter(p=>p.symbol.toUpperCase().includes(search)).forEach(p=>{const o=new Option(p.label+(p.supported?'':' · недоступно: '+p.reason),p.symbol);o.disabled=!p.supported;select.append(o);});
 if([...select.options].some(o=>o.value===selected))select.value=selected;
 else invalidate();
 instrumentSummary(n);
}
const tabs=['trade','connections','diagnostics'];
function openTab(name){clearSecrets();tabs.forEach(t=>{const active=t===name;$('tab-'+t).setAttribute('aria-selected',String(active));$('tab-'+t).tabIndex=active?0:-1;$(t).hidden=!active;});}
tabs.forEach((name,n)=>{const button=$('tab-'+name);button.addEventListener('click',()=>openTab(name));button.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?2:(n+(event.key==='ArrowRight'?1:2))%3;openTab(tabs[next]);$('tab-'+tabs[next]).focus();}});});
for(let n=0;n<2;n++){
 const box=$('leg-'+n),select=$(n?'hedge-deployment':'primary-deployment');
 venues.forEach(([value,label])=>{const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);});
 select.value=n?'lighter-core-mainnet':'hyperliquid-mainnet-native';
 const title=document.createElement('p');title.className='connection-venue';box.append(title);
 fields.forEach(([name,label])=>box.append(input(label,name,{required:name!=='api_key_index'})));
 const setup=$('setup-'+n);
 setup.append(input('Выделенный execution key · не owner-wallet key','secret',{password:true}));
 setup.append(input('Новая локальная passphrase · минимум 16 символов','passphrase',{password:true}));
 const button=document.createElement('button');button.type='submit';button.textContent='Зашифровать ключ';setup.append(button);
 function conditional(){
  title.textContent=select.selectedOptions[0].textContent;
  box.querySelector('[name=api_key_index]').parentElement.hidden=!select.value.startsWith('lighter-');
  invalidate(true);clearSecrets();
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
let cardsSignature='';
function showDetail(){$('strategy-modal').close();$('strategies').hidden=true;$('strategy-detail').hidden=false;clearSecrets();}
$('new-strategy').addEventListener('click',()=>{$('strategy-modal').showModal();});
$('close-strategy').addEventListener('click',()=>{$('strategy-modal').close();});
$('back-strategies').addEventListener('click',()=>{$('strategies').hidden=false;$('strategy-detail').hidden=true;clearSecrets();});
function renderStrategies(row){
 const rows=row.strategies||[],signature=JSON.stringify(rows);
 if(signature===cardsSignature)return;cardsSignature=signature;
 const out=$('strategy-list');out.replaceChildren();
 if(!rows.length){out.textContent='Стратегий пока нет. Соберите первую стратегию.';return;}
 rows.forEach(strategy=>{
  const card=document.createElement('article');card.className='strategy-card';
  const title=document.createElement('h2');title.textContent=strategy.id;card.append(title);
  const markets=document.createElement('p');markets.textContent=strategy.legs.map(l=>(venues.find(v=>v[0]===l.deployment)?.[1]||l.deployment)+' · '+l.symbol).join(' ↔ ');card.append(markets);
  const state=document.createElement('p');state.textContent=strategy.status+' · '+(strategy.intent.side==='buy'?'Long primary / short hedge':'Short primary / long hedge')+' · target '+strategy.intent.quantity;card.append(state);
  for(const leg of ['primary','hedge']){const p=document.createElement('p');p.textContent=leg+' P&L: не рассчитан · realised: нет данных · unrealised: нет данных';card.append(p);}
  const live=document.createElement('button');live.type='button';live.textContent='Лайв · открыть детали';live.addEventListener('click',showDetail);card.append(live);out.append(card);
 });
 $('new-strategy').disabled=Boolean(row.state||row.running);
}
function render(row){
 renderStrategies(row);
 $('summary').textContent=(row.running?'Работает':'Заблокировано')+' · '+(row.trading_enabled?'authority активна':'authority закрыта')+' · '+(row.checked?'аккаунты проверены':'нужна свежая проверка');
 $('vault-state').textContent=(row.vaults||[]).map((v,n)=>(n?'Hedge':'Primary')+': '+(v.configured?'ключ зашифрован':'ключ не настроен')).join(' · ');
 const out=$('ledger');out.replaceChildren();const s=row.state;
 if(!s){out.textContent='Нет сохранённой задачи. Здесь появятся подтверждённые сделки, комиссии и журнал.';return;}
 const title=document.createElement('h3');title.textContent=(s.intent?.id||'')+' · '+s.status+' / '+s.phase;out.append(title);
 const p=document.createElement('p');p.textContent='Primary '+s.primary_filled+' · Hedge '+s.hedge_filled+' · Остаток '+s.residual_base+' · Комиссии '+Object.entries(s.fees_by_currency||{}).map(([k,v])=>v+' '+k).join(', ');out.append(p);
 const stamp=document.createElement('p');stamp.className='muted';stamp.textContent='Сохранённое наблюдение: '+(s.observed_at==null?'время неизвестно':new Date(s.observed_at*1000).toLocaleString('ru-RU'))+'. Это не текущая связь с биржей.';out.append(stamp);
 const analytics=document.createElement('div');analytics.className='pair';
 for(const label of ['primary','hedge']){
  const a=s.analytics?.[label],panel=document.createElement('article');panel.className='strategy-card';
  const h=document.createElement('h3');h.textContent=label+' · фактические fills';panel.append(h);
  const p=document.createElement('p');p.textContent='Средняя цена: '+(a?.average_price??'нет данных')+' · base: '+(a?.quantity??'нет данных')+' · signed exposure: '+(a?.signed_base??'нет данных');panel.append(p);
  const fee=document.createElement('p');fee.textContent='Уплаченные комиссии: '+(a?.fees_by_currency?Object.entries(a.fees_by_currency).map(([c,v])=>v+' '+c).join(' / '):'нет данных');panel.append(fee);
  const pnl=document.createElement('p');pnl.textContent='Realised / unrealised P&L: не рассчитан · funding: нет данных, не включён';panel.append(pnl);analytics.append(panel);
 }
 out.append(analytics);
 const fills=Object.entries(s.fills||{}).flatMap(([leg,rows])=>rows.map(f=>[leg,f.id,f.quantity,f.price,f.executed_at==null?'нет данных':new Date(f.executed_at*1000).toLocaleString('ru-RU'),f.fee+' '+f.fee_currency]));
 out.append(textTable(['Нога','Trade ID','Количество','Цена','Время сделки (биржа)','Комиссия'],fills));
 const missingTime=document.createElement('p');missingTime.className='muted';missingTime.textContent='Если adapter не сохранил exchange timestamp, время сделки недоступно. Время журнала ниже — время наблюдения, не исполнения.';out.append(missingTime);
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
$('load-markets').addEventListener('click',async()=>{
 invalidate(true);const generation=marketGeneration,epoch=uiEpoch;
 $('market-status').textContent='Чтение публичной metadata…';$('load-markets').disabled=true;
 try{const row=await client.mutate('markets',deploymentPair());if(generation!==marketGeneration||epoch!==uiEpoch)return;
  marketRows=row.markets;pairOptions(0);pairOptions(1);$('market-status').textContent='Рынки бирж: '+row.markets.map(rows=>rows.length).join(' / ')+' · metadata '+new Date(row.observed_at*1000).toLocaleTimeString('ru-RU')+' · срок '+row.max_age_seconds+' с. Выберите инструменты независимо и проверьте соответствие. '+row.warning;
 }catch(error){if(epoch===uiEpoch&&generation===marketGeneration)$('market-status').textContent='Публичная metadata недоступна или пара не поддерживается. Старые данные не используются. Обновите пары.';}
 finally{$('load-markets').disabled=false;}
});
for(let n=0;n<2;n++){$('pair-search-'+n).addEventListener('input',()=>pairOptions(n));$('pair-select-'+n).addEventListener('change',()=>{invalidate();instrumentSummary(n);});}
for(const name of ['side','offset_pct','protection_bps'])$('config').elements[name].addEventListener('input',()=>invalidate());
$('quote').addEventListener('click',async()=>{
 invalidate();const generation=marketGeneration,epoch=uiEpoch,common=values($('config'));
 $('quote-status').textContent='Чтение свежего рынка и расчёт…';$('quote').disabled=true;
 try{const row=await client.mutate('quote',{...deploymentPair(),primary_symbol:$('pair-select-0').value,hedge_symbol:$('pair-select-1').value,side:common.side,offset_pct:common.offset_pct,protection_bps:common.protection_bps});
  if(generation!==marketGeneration||epoch!==uiEpoch)return;
  quoteData=row;$('save-config').disabled=false;
  $('quote-status').textContent='Primary LIMIT '+row.legs[0].price+' · Hedge IOC защита '+row.legs[1].price+' · ref '+row.legs.map(l=>l.reference).join(' / ')+' · '+new Date(row.observed_at*1000).toLocaleTimeString('ru-RU')+' · действительно '+row.max_age_seconds+' с. Минимум base: '+row.legs.map(l=>l.min_size).join(' / ')+'. Шаг base: '+row.legs.map(l=>l.size_step).join(' / ')+'.';
 }catch(error){if(generation===marketGeneration&&epoch===uiEpoch)$('quote-status').textContent='Свежие цены недоступны или пара не поддерживается. Исполнение не подготовлено.';}
 finally{$('quote').disabled=false;}
});
$('config').addEventListener('submit',event=>{
 event.preventDefault();const common=values(event.currentTarget);
 action(event.currentTarget,()=>{
 const identities=[0,1].map(n=>{const box=$('leg-'+n),deployment=$(n?'hedge-deployment':'primary-deployment').value;return {deployment,account:box.querySelector('[name=account]').value,signer:box.querySelector('[name=signer]').value,api_key_index:deployment.startsWith('lighter-')?safeInteger(box.querySelector('[name=api_key_index]').value):null};});
 common.id=(common.label?common.label+'-':'dn-')+root.crypto.randomUUID();
 common.max_notional=$('max-notional').value;common.max_slippage_bps=$('max-slippage').value;common.lifetime=$('lifetime').value;common.poll_seconds=$('poll-seconds').value;
 return client.mutate('configure',{config:buildSimpleConfig(common,identities,quoteData)}).then(row=>{$('strategy-modal').close();$('strategies').hidden=false;$('strategy-detail').hidden=true;return row;});
 });
});
for(let n=0;n<2;n++)$('setup-'+n).addEventListener('submit',event=>{
 event.preventDefault();const data=take(event.currentTarget);
 action(event.currentTarget,()=>client.mutate('setup',{leg:n,secret:serializeSetupSecret(data,false),passphrase:data.passphrase}));
});
for(const name of ['check','start'])$(name).addEventListener('submit',event=>{
 event.preventDefault();const data=take(event.currentTarget),body={passphrases:[data.primary_passphrase,data.hedge_passphrase]};
 if(name==='start')body.confirmation=data.confirmation;
 action(event.currentTarget,async()=>{const row=await client.mutate(name,body);if(name==='check')renderAccounts(row);return row;});
});
$('lock').addEventListener('click',()=>action($('lock-form'),()=>client.mutate('lock',{})));
clearSecrets();client.bootstrap().then(()=>{if(client.authenticated)poll();}).catch(()=>{$('message').textContent='Сервер недоступен. Обновите страницу.';});
})(typeof window!=='undefined'?window:globalThis);

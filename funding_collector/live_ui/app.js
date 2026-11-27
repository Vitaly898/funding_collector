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
function createMarketLoader({client,onChange}){
 const flights=new Map(),versions=[0,0];let epoch=0;
 function reset(){epoch++;flights.clear();versions[0]++;versions[1]++;}
 async function load(n,deployment){
  const version=++versions[n],session=epoch;
  onChange(n,{status:'loading',rows:[]});
  const lighter=deployment.startsWith('lighter-');
  // The public API returns a compatible pair catalog. Use a fixed public
  // companion for each venue; the other selected leg must not gate this list.
  // Actual selected-pair compatibility/fresh IDs are still checked by quote.
  const pair={primary:lighter?'hyperliquid-mainnet-native':deployment,hedge:lighter?deployment:'lighter-core-mainnet'};
  const key=JSON.stringify(pair);
  let pending=flights.get(key);
  if(!pending){pending=client.mutate('markets',pair);flights.set(key,pending);}
  try{
   const result=await pending;
   if(session!==epoch||version!==versions[n])return;
   const rows=result.markets[lighter?1:0];
   onChange(n,{status:rows.length?'ready':'empty',rows});
  }catch(error){if(session===epoch&&version===versions[n])onChange(n,{status:'error',rows:[]});}
  finally{if(flights.get(key)===pending)flights.delete(key);}
 }
 return {load,reset};
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
function buildFarmingPolicy(data,deployments){
 if(data.farming_enabled!=='on')return undefined;
 if(!deployments.every(v=>['hyperliquid-mainnet-native','lighter-core-mainnet','lighter-rh-mainnet'].includes(v)))throw Error('UNSUPPORTED_FARMING');
 const p={};
 for(const [key,lo,hi] of [['hold_seconds',60,86400],['min_delay_seconds',1,86400],['max_delay_seconds',1,86400],['max_cycles',1,10]]){
  p[key]=safeInteger(data[key]);if(p[key]<lo||p[key]>hi)throw Error('INVALID_FARMING_BOUND');
 }
 if(p.min_delay_seconds>p.max_delay_seconds)throw Error('INVALID_FARMING_DELAY');
 for(const key of ['max_turnover','max_fees']){if(typeof data[key]!=='string'||! /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(data[key])||!Number.isFinite(Number(data[key]))||Number(data[key])<=0)throw Error('INVALID_FARMING_BUDGET');p[key]=data[key];}
 return p;
}
function statusText(value){return ({DONE:'Циклы завершены · обе ноги закрыты',NEW:'Готова к запуску',RUNNING:'Исполняется',UNKNOWN:'Результат неизвестен — нужна сверка',ATTENTION:'Требуется ручная сверка',COMPLETED_CONFIRMED:'Исполнено',CONFIGURED:'Настроена'})[value]||'Ожидает запуска';}
function strategyName(id){const name=String(id||'').replace(/-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i,'');return !name||name==='dn'?'Ручная стратегия':name;}
function eventText(value){return ({farming_created:'Созданы ограниченные автоматические циклы',opening_turnover_reserved:'Учтён лимит оборота открытия',both_legs_hedged_hold_started:'Обе ноги подтверждены; начался отсчёт удержания',write_ahead_close_primary:'Подготовлено reduce-only закрытие первой ноги',write_ahead_close_hedge:'Подготовлено reduce-only закрытие второй ноги',close_primary_ack_not_fill:'Ответ на закрытие первой ноги — это ещё не сделка',close_hedge_ack_not_fill:'Ответ на закрытие второй ноги — это ещё не сделка',close_primary_confirmed_trades:'Подтверждены сделки закрытия первой ноги',close_hedge_confirmed_trades:'Подтверждены сделки закрытия второй ноги',both_legs_confirmed_flat_done:'Обе ноги закрыты; циклы завершены',flat_random_delay_persisted:'Обе ноги закрыты; сохранена задержка перед следующим циклом',next_bounded_cycle:'Начат следующий ограниченный цикл',farming_requires_attention:'Циклы остановлены; нужна ручная сверка позиций и заявок',created:'Стратегия сохранена',reconciled_flat_baseline:'Проверены позиции и открытые заявки',reconciled_before_hedge:'Первый объём подтверждён; проверена вторая биржа',write_ahead_primary:'Подготовлена заявка на первой бирже',write_ahead_hedge:'Подготовлена встречная заявка на второй бирже',primary_ack_not_fill:'Первая биржа ответила на заявку — это ещё не сделка',hedge_ack_not_fill:'Вторая биржа ответила на заявку — это ещё не сделка',primary_confirmed_trades:'Подтверждены сделки на первой бирже',hedge_confirmed_trades:'Подтверждены сделки на второй бирже',unknown_primary:'Результат первой заявки неизвестен; повтор запрещён',unknown_hedge:'Результат второй заявки неизвестен; повтор запрещён',failure_requires_attention:'Исполнение остановлено: нужна ручная сверка',completed_confirmed_positions_remain_open:'Обе ноги исполнены; позиции остаются открытыми'})[value]||'Состояние обновлено';}
if(typeof module!=='undefined'&&module.exports)module.exports={createMarketLoader,createClient,serializeSetupSecret,serializeLeg,logoutAndRefresh,buildSimpleConfig,buildFarmingPolicy,statusText,eventText,strategyName};
if(typeof document==='undefined')return;
const $=id=>document.getElementById(id);
const sensitive=()=>document.querySelectorAll('input[type=password]');
function clearSecrets(){sensitive().forEach(el=>{el.value='';});}
let timer=null,uiEpoch=0;
function gate(auth){
 uiEpoch++;clearSecrets();$('login-panel').hidden=auth;$('workspace').hidden=!auth;$('logout').hidden=!auth;
 clearTimeout(timer);
 if(!auth){$('strategy-modal').close();$('strategy-list').replaceChildren();cardsSignature='';$('strategy-detail').hidden=true;$('strategies').hidden=false;invalidate(true);$('ledger').replaceChildren();$('raw-state').textContent='';$('accounts').replaceChildren();$('summary').textContent='Исполнение заблокировано';$('message').textContent='';}
}
const client=createClient({onAuth:gate});
const venues=[['hyperliquid-mainnet-native','Hyperliquid · native'],['lighter-core-mainnet','Lighter · Core'],['lighter-rh-mainnet','Lighter · RH'],['entropy-mainnet-io','Entropy · io']];
const fields=[['account','Адрес аккаунта'],['signer','Публичный ключ торгового подписанта'],['api_key_index','Номер API-ключа · только Lighter']];
function input(label,name,{password=false,required=true,value=''}={}){
 const wrap=document.createElement('label');wrap.textContent=label;
 const el=document.createElement('input');el.name=name;el.type=password?'password':'text';el.required=required;el.value=value;
 if(password){el.autocomplete='off';el.maxLength=1024;}else el.autocomplete='off';
 wrap.append(el);return wrap;
}
let marketRows=[[],[]],quoteData=null,marketGeneration=0;
function invalidate(markets=false){
 marketGeneration++;quoteData=null;$('save-config').disabled=true;$('quote-status').textContent='Цена ещё не рассчитана.';
 if(markets){marketLoader.reset();marketRows=[[],[]];for(let n=0;n<2;n++){$('pair-select-'+n).replaceChildren(new Option('Выберите инструмент',''));$('pair-select-'+n).disabled=true;$('retry-markets-'+n).hidden=true;$('instrument-'+n).textContent='Инструмент не выбран.';}}
}
function farmingAvailability(){
 const supported=[$('primary-deployment').value,$('hedge-deployment').value].every(v=>['hyperliquid-mainnet-native','lighter-core-mainnet','lighter-rh-mainnet'].includes(v));
 const opt=$('farming-enabled');opt.disabled=!supported;if(!supported)opt.checked=false;
 $('farming-fields').disabled=!supported||!opt.checked;
 $('farming-capability').textContent=supported?'Опционально: открытие → удержание после хеджа → reduce-only закрытие → обе ноги без позиции → задержка → следующий цикл.':'Для этой пары автоматические циклы недоступны. Ручное открытие не меняется.';
}
$('farming-enabled').addEventListener('change',farmingAvailability);
const deploymentPair=()=>({primary:$('primary-deployment').value,hedge:$('hedge-deployment').value});
function instrumentSummary(n){const row=marketRows[n].find(r=>r.symbol===$('pair-select-'+n).value);$('instrument-'+n).textContent=row?row.label+' · базовый актив '+row.base+' · '+row.units:'Инструмент не выбран.';}
function pairOptions(n){
 const select=$('pair-select-'+n);
 select.replaceChildren(new Option('Выберите инструмент',''));
 marketRows[n].forEach(p=>{const o=new Option(p.label,p.symbol);o.disabled=!p.supported;o.title=p.reason||'';select.append(o);});
 instrumentSummary(n);
}
const marketLoader=createMarketLoader({client,onChange(n,state){
 invalidate();marketRows[n]=state.rows;
 const select=$('pair-select-'+n);select.disabled=state.status!=='ready';
 $('retry-markets-'+n).hidden=!['empty','error'].includes(state.status);
 if(state.status==='ready')pairOptions(n);
 else {select.replaceChildren(new Option(({loading:'Загрузка инструментов…',empty:'Нет доступных инструментов',error:'Не удалось загрузить инструменты'})[state.status],''));instrumentSummary(n);}
 $('market-state-'+n).textContent=({loading:'Загрузка инструментов…',ready:'Список биржи загружен.',empty:'Биржа вернула пустой список. Попробуйте ещё раз.',error:'Не удалось получить публичный список. Старые данные не используются.'})[state.status];
}});
function loadLeg(n){return marketLoader.load(n,$(n?'hedge-deployment':'primary-deployment').value);}
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
 setup.append(input('Выделенный торговый ключ · не ключ основного кошелька','secret',{password:true}));
 setup.append(input('Новый пароль шифрования ключа · минимум 16 символов','passphrase',{password:true}));
 const button=document.createElement('button');button.type='submit';button.textContent='Зашифровать ключ';setup.append(button);
 function conditional(){
  title.textContent=select.selectedOptions[0].textContent;
  box.querySelector('[name=api_key_index]').parentElement.hidden=!select.value.startsWith('lighter-');
  clearSecrets();
 }
 select.addEventListener('change',()=>{conditional();farmingAvailability();if(client.authenticated&&$('strategy-modal').open)loadLeg(n);else invalidate(true);});conditional();
}
farmingAvailability();
function values(form){return Object.fromEntries(new FormData(form));}
function take(form){const data=values(form);form.querySelectorAll('input[type=password]').forEach(el=>el.value='');return data;}
function textTable(headers,rows){
 const table=document.createElement('table'),thead=document.createElement('thead'),tr=document.createElement('tr');
 headers.forEach(v=>{const th=document.createElement('th');th.textContent=v;tr.append(th);});thead.append(tr);table.append(thead);
 const tbody=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(value=>{const td=document.createElement('td');td.textContent=String(value??'—');tr.append(td);});tbody.append(tr);});table.append(tbody);return table;
}
let cardsSignature='';
function showDetail(){$('strategy-modal').close();$('strategies').hidden=true;$('strategy-detail').hidden=false;clearSecrets();}
$('new-strategy').addEventListener('click',()=>{$('strategy-modal').showModal();loadLeg(0);loadLeg(1);});
$('close-strategy').addEventListener('click',()=>{$('strategy-modal').close();});
$('back-strategies').addEventListener('click',()=>{$('strategies').hidden=false;$('strategy-detail').hidden=true;clearSecrets();});
function renderStrategies(row){
 const rows=row.strategies||[],signature=JSON.stringify(rows);
 if(signature===cardsSignature)return;cardsSignature=signature;
 const out=$('strategy-list');out.replaceChildren();
 if(!rows.length){out.textContent='Стратегий пока нет. Соберите первую стратегию.';return;}
 rows.forEach(strategy=>{
  const card=document.createElement('article');card.className='strategy-card';
  const title=document.createElement('h2');title.textContent=strategyName(strategy.id);card.append(title);
  const markets=document.createElement('p');markets.textContent=strategy.legs.map(l=>(venues.find(v=>v[0]===l.deployment)?.[1]||l.deployment)+' · '+l.symbol).join(' ↔ ');card.append(markets);
  const state=document.createElement('p');state.textContent=statusText(strategy.status)+' · '+(strategy.intent.side==='buy'?'Покупка на первой / продажа на второй':'Продажа на первой / покупка на второй')+' · объём '+strategy.intent.quantity;card.append(state);
  for(const leg of ['primary','hedge']){const p=document.createElement('p');p.textContent=(leg==='primary'?'Первая биржа':'Вторая биржа')+' · Прибыль: не рассчитана';card.append(p);}
  const live=document.createElement('button');live.type='button';live.textContent='Лайв · открыть детали';live.addEventListener('click',showDetail);card.append(live);out.append(card);
 });
 $('new-strategy').disabled=Boolean(row.state||row.running);
}
function render(row){
 renderStrategies(row);
 $('raw-state').textContent=JSON.stringify(row.state?{intent_id:row.state.intent?.id,status:row.state.status,phase:row.state.phase,orders:row.state.orders,fills:row.state.fills,journal:row.state.journal}:null,null,2);
 $('summary').textContent=(row.running?'Работает':'Заблокировано')+' · '+(row.trading_enabled?'торговля разрешена':'торговля запрещена')+' · '+(row.checked?'аккаунты проверены':'нужна свежая проверка');
 $('vault-state').textContent=(row.vaults||[]).map((v,n)=>(n?'Вторая биржа':'Первая биржа')+': '+(v.configured?'ключ зашифрован':'ключ не настроен')).join(' · ');
 const out=$('ledger');out.replaceChildren();const s=row.state;
 if(!s){out.textContent='Нет сохранённой задачи. Здесь появятся подтверждённые сделки, комиссии и журнал.';return;}
 const title=document.createElement('h3');title.textContent=strategyName(s.intent?.id)+' · '+statusText(s.status);out.append(title);
 const p=document.createElement('p');p.textContent='Исполнено на первой: '+s.primary_filled+' · на второй: '+s.hedge_filled+' · Незахеджированный остаток '+s.residual_base+' · Комиссии '+Object.entries(s.fees_by_currency||{}).map(([k,v])=>v+' '+k).join(', ');out.append(p);
 if(s.farming){
  const f=s.farming,info=document.createElement('p');info.id='farming-progress';
  const phases={OPENING:'Открытие',HOLDING:'Удержание',CLOSING_PRIMARY:'Закрытие первой ноги',CLOSING_HEDGE:'Закрытие второй ноги',DELAY:'Пауза между циклами',DONE:'Обе ноги закрыты'};
  info.textContent='Автоматические циклы · '+(phases[s.phase]||'Нужна сверка')+' · Цикл '+f.cycle+' / '+f.policy.max_cycles+' · Резерв оборота '+f.turnover_reserved+' / '+f.policy.max_turnover;
  const deadline=s.phase==='HOLDING'?f.hold_deadline:s.phase==='DELAY'?f.delay_deadline:null;
  if(deadline!=null)info.textContent+=' · Сохранённый срок: '+new Date(deadline*1000).toLocaleString('ru-RU');out.append(info);
  const closes=Object.entries(f.close_fills||{}).flatMap(([leg,rows])=>rows.map(r=>[leg==='primary'?'Первая биржа':'Вторая биржа',r.quantity,r.price,r.fee+' '+r.fee_currency]));
  if(closes.length){const h=document.createElement('h3');h.textContent='Подтверждённые сделки закрытия';out.append(h,textTable(['Биржа','Количество','Цена','Комиссия'],closes));}
 }
 const stamp=document.createElement('p');stamp.className='muted';stamp.textContent='Сохранённое наблюдение: '+(s.observed_at==null?'время неизвестно':new Date(s.observed_at*1000).toLocaleString('ru-RU'))+'. Это не текущая связь с биржей.';out.append(stamp);
 const analytics=document.createElement('div');analytics.className='pair';
 for(const label of ['primary','hedge']){
  const a=s.analytics?.[label],panel=document.createElement('article');panel.className='strategy-card';
  const h=document.createElement('h3');h.textContent=(label==='primary'?'Первая биржа':'Вторая биржа')+' · исполненные сделки';panel.append(h);
  const p=document.createElement('p');p.textContent='Средняя цена: '+(a?.average_price??'нет данных')+' · Исполненный объём: '+(a?.quantity??'нет данных');panel.append(p);
  const fee=document.createElement('p');fee.textContent='Уплаченные комиссии: '+(a?.fees_by_currency?Object.entries(a.fees_by_currency).map(([c,v])=>v+' '+c).join(' / '):'нет данных');panel.append(fee);
  const pnl=document.createElement('p');pnl.textContent='Прибыль: не рассчитана · Платежи финансирования: нет данных, не включены';panel.append(pnl);analytics.append(panel);
 }
 out.append(analytics);
 const fills=Object.entries(s.fills||{}).flatMap(([leg,rows])=>rows.map(f=>[leg==='primary'?'Первая биржа':'Вторая биржа',f.quantity,f.price,f.executed_at==null?'нет данных':new Date(f.executed_at*1000).toLocaleString('ru-RU'),f.fee+' '+f.fee_currency]));
 out.append(textTable(['Биржа','Количество','Цена','Время сделки (биржа)','Комиссия'],fills));
 const missingTime=document.createElement('p');missingTime.className='muted';missingTime.textContent='Если биржа не передала время сделки, показано «нет данных». Время событий ниже — время наблюдения, не исполнения.';out.append(missingTime);
 out.append(textTable(['№','Время','Событие'],(s.journal||[]).map(e=>[e.sequence,new Date(e.at*1000).toLocaleTimeString('ru-RU'),eventText(e.event)])));
}
function renderAccounts(row){
 const out=$('accounts');out.replaceChildren();
 (row.accounts||[]).forEach((a,n)=>{
  const h=document.createElement('h3');h.textContent=(n?'Вторая биржа':'Первая биржа')+' · регистрация '+(a.registration_valid?'подтверждена':'не подтверждена')+' · открытых ордеров: '+a.open_order_count;out.append(h);
  if(a.positions.length)out.append(textTable(['Фактическая позиция','Количество'],a.positions.map(p=>[p.symbol,p.quantity])));
  else {const p=document.createElement('p');p.textContent='Фактических позиций нет.';out.append(p);}
 });
 const time=document.createElement('p');time.className='muted';time.textContent='Проверка без торговли: '+new Date().toLocaleString('ru-RU')+'. Доступ к обоим ключам закрыт после чтения.';out.append(time);
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
for(let n=0;n<2;n++){$('retry-markets-'+n).addEventListener('click',()=>loadLeg(n));$('pair-select-'+n).addEventListener('change',()=>{invalidate();instrumentSummary(n);});}
for(const name of ['side','offset_pct','protection_bps'])$('config').elements[name].addEventListener('input',()=>invalidate());
$('quote').addEventListener('click',async()=>{
 invalidate();const generation=marketGeneration,epoch=uiEpoch,common=values($('config'));
 $('quote-status').textContent='Чтение свежего рынка и расчёт…';$('quote').disabled=true;
 try{const row=await client.mutate('quote',{...deploymentPair(),primary_symbol:$('pair-select-0').value,hedge_symbol:$('pair-select-1').value,side:common.side,offset_pct:common.offset_pct,protection_bps:common.protection_bps});
  if(generation!==marketGeneration||epoch!==uiEpoch)return;
  quoteData=row;$('save-config').disabled=false;
  $('quote-status').textContent='Первая биржа · лимитная цена '+row.legs[0].price+' · Вторая биржа · защитная цена '+row.legs[1].price+' · Опорные цены '+row.legs.map(l=>l.reference).join(' / ')+' · '+new Date(row.observed_at*1000).toLocaleTimeString('ru-RU')+' · действительно '+row.max_age_seconds+' с. Минимальный объём: '+row.legs.map(l=>l.min_size).join(' / ')+'. Шаг объёма: '+row.legs.map(l=>l.size_step).join(' / ')+'.';
 }catch(error){if(generation===marketGeneration&&epoch===uiEpoch)$('quote-status').textContent='Свежие цены недоступны или пара не поддерживается. Исполнение не подготовлено.';}
 finally{$('quote').disabled=false;}
});
$('config').addEventListener('submit',event=>{
 event.preventDefault();const common=values(event.currentTarget);
 action(event.currentTarget,()=>{
 const identities=[0,1].map(n=>{const box=$('leg-'+n),deployment=$(n?'hedge-deployment':'primary-deployment').value;return {deployment,account:box.querySelector('[name=account]').value,signer:box.querySelector('[name=signer]').value,api_key_index:deployment.startsWith('lighter-')?safeInteger(box.querySelector('[name=api_key_index]').value):null};});
 common.id=(common.label?common.label+'-':'dn-')+root.crypto.randomUUID();
 common.max_notional=$('max-notional').value;common.max_slippage_bps=$('max-slippage').value;common.lifetime=$('lifetime').value;common.poll_seconds=$('poll-seconds').value;
 const config=buildSimpleConfig(common,identities,quoteData),farming=buildFarmingPolicy(common,identities.map(i=>i.deployment));
 if(farming)config.farming=farming;
 return client.mutate('configure',{config}).then(row=>{$('strategy-modal').close();$('strategies').hidden=false;$('strategy-detail').hidden=true;return row;});
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

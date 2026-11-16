'use strict';
const $ = id => document.getElementById(id);
const fields = ['strategy_mode','primary_venue','direction','market','quantity','execution','hedge_venue','hedge_market','primary_fee_bps','hedge_fee_bps','max_notional_usd','max_roundtrip_cost_usd','max_book_age_seconds','scenario'];
let token = '', draft = null, busy = false, submitted = false;
const names = {core:'Lighter Core',rh:'Lighter RH',extended:'Extended',hyperliquid:'Hyperliquid native',entropy:'Entropy io',evedex:'EVEDEX'};
function node(tag, text, className) { const n = document.createElement(tag); if (text !== undefined) n.textContent = String(text); if (className) n.className = className; return n; }
function decimalParts(value) {const s=String(value ?? '0'); if(!/^-?\d+(\.\d+)?$/.test(s)) throw new Error('Некорректный объём в сохранённом состоянии'); const digits=(s.split('.')[1]||'').length; return [BigInt(s.replace('.','')),digits];}
function decimalAdd(a,b) {const [x,p]=decimalParts(a),[y,q]=decimalParts(b),n=Math.max(p,q);const result=x*10n**BigInt(n-p)+y*10n**BigInt(n-q);const sign=result<0n?'-':'';const raw=(result<0n?-result:result).toString().padStart(n+1,'0');return sign+(n?(raw.slice(0,-n)+'.'+raw.slice(-n)).replace(/\.?0+$/,''):raw);}
function equalQty(a,b) {return decimalAdd(a,'-'+String(b).replace(/^-/,''))==='0';}
function roles(i) {const keys=Object.keys(i.preview.legs);return keys.includes('leg_a')?['leg_a','leg_b']:keys;}
function unknown(i) {return Number(i.pending_unknown_hedge_base)>0 || /UNKNOWN/i.test(i.stop_reason || '');}
function strategyStage(i) {
  const [a,b]=roles(i), filled=i.filled_base, target=i.preview.quantity_base;
  if (i.status==='STOPPED_SYNTHETIC'||unknown(i)) return 'Требует внимания';
  if (i.status==='COMPLETED_SYNTHETIC') return Number(i.residual_base)===0 && equalQty(filled[a],target) && equalQty(filled[b],target)?'Готово':'Требует внимания';
  if (i.journal.some(e=>e.kind==='market_hedge_intent')) return 'Хеджируем';
  if (equalQty(filled[a],target)) return 'Первая нога готова';
  if (Number(filled[a])>0) return 'Набираем первую ногу';
  return 'Ожидание';
}
function executionLines(i) {
  const [a,b]=roles(i), unit=i.preview.symbol, dn=i.strategy_mode==='DN_FULL_LIMIT_MARKET_V3', out=[];
  let count=0,total='0', complete=false;
  const line=(e,text,warning=false)=>out.push({text,warning,time:e.recorded_at_unix_s,sequence:e.sequence});
  i.journal.forEach(e=>{
    if(e.kind==='fill'&&e.venue===a) {
      count++; total=decimalAdd(total,e.quantity);
      line(e,`${dn?'Лимитка №'+count+' исполнена':'Исполнение первой ноги'}: ${e.quantity} ${unit} · цена ${e.vwap}`);
      if(dn&&!complete&&equalQty(total,i.preview.quantity_base)) {line(e,`Первая нога собрана: ${total} ${unit}`);complete=true;}
    } else if(e.kind==='market_hedge_intent') line(e,`Маркетный хедж подготовлен: ${e.quantity_base} ${unit} · запись намерения, не подтверждение отправки`);
    else if(e.kind==='fill'&&e.venue===b) line(e,`${dn?'Маркетный хедж исполнен':'Исполнение второй ноги'}: ${e.quantity} ${unit} · цена ${e.vwap}`);
    else if(e.kind==='stop') line(e,/UNKNOWN/i.test(e.reason||'')||Number(e.unknown_hedge_base)>0?'Результат заявки неизвестен · повторная заявка не создаётся':'Исполнение прервано · проверьте причину ниже',true);
    else if(e.kind==='complete') line(e,`Обе ноги собраны · остаток ${e.residual_base} ${unit}`);
    else if(e.kind==='reconcile_synthetic') line(e,'Сохранённые исполнения сверены с локальным журналом');
  });
  if(unknown(i)&&!out.some(e=>e.text.includes('Результат заявки неизвестен'))) line({},'Результат заявки неизвестен · требуется сверка',true);
  return out;
}
function message(text, error=false) {$('message').textContent=text;$('message').className=error?'error':'';}
function params() {return Object.fromEntries(fields.map(key=>[key,$(key).value]));}
function ageExpired() {if(!draft||submitted)return false;return Object.values(draft.preview.deployments).some(d=>{const age=Date.now()/1000-Number(d.book_observed_at_unix_s);return age<0||age>Number(draft.params.max_book_age_seconds);});}
function controls() {
  document.querySelectorAll('button, #intent-form input, #intent-form select, #acknowledge').forEach(n=>{n.disabled=busy;});
  $('paper-button').disabled=busy||!draft||submitted||!$('acknowledge').checked||ageExpired();
  $('preview-button').hidden=!!draft&&!submitted;
  $('preview-button').textContent=busy?'Обработка…':'Рассчитать и проверить';
  $('preview-age').textContent=ageExpired()?'Расчёт устарел. Нажмите «Изменить» и рассчитайте заново.':'';
  $('fee-state').textContent=$('primary_fee_bps').value!==''&&$('hedge_fee_bps').value!==''?`— допущения ${$('primary_fee_bps').value} / ${$('hedge_fee_bps').value} bps, не тариф аккаунта`:'— комиссии не заданы';
  $('base-unit').textContent=($('market').value||'base')+' (base)';
}
async function api(path,value) {
  const options=value===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(value)};
  const response=await fetch(path,{...options,cache:'no-store',credentials:'same-origin'});
  const data=await response.json();if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);return data;
}
async function task(action) {if(busy)return;busy=true;controls();try{await action();}catch(e){message('Операция отклонена: '+e.message+'. Проверьте сохранённый статус перед повтором.',true);}finally{busy=false;controls();}}
function detail(title,data) {const d=node('details');d.append(node('summary',title),node('pre',JSON.stringify(data,null,2)));return d;}
function renderPreview(p) {
  const box=$('preview');box.replaceChildren();
  const legs=node('div',undefined,'leg-summary');Object.entries(p.legs).forEach(([k,l],index)=>{
    const card=node('div',undefined,'leg');card.append(node('strong',`${index===0?'1 · Лимитная':'2 · Хедж'} / ${names[l.connection_id]||l.connection_id||k}`),node('p',`${l.side==='buy'?'Купить':'Продать'} ${l.quantity} ${p.symbol} · ${l.market_id}`),node('p',index===0?`Лимитная цена: ${l.limit_price ?? 'см. детали'}`:`Оценка цены: ${l.vwap}`));legs.append(card);
  });box.append(legs);
  const cost=node('div',undefined,'cost');cost.append(node('span','Оценка затрат · вход + выход'),node('strong',`${p.roundtrip_cost_usd} USD`));box.append(cost);
  box.append(node('p',`Комиссии: ${Object.values(p.fees_bps_assumed_not_account_verified).join(' / ')} bps — допущения, не тариф аккаунта. Спред и глубина учтены; funding исключён. Статическая оценка, не гарантия исполнения.`, 'hint'));
  if(Object.values(p.legs).some(l=>['hyperliquid','entropy'].includes(l.connection_id)))box.append(node('p','Разные валюты цены и обеспечения: USD/USDT и USDC не гарантируют паритет. Рыночный хедж может исполняться защищённым IOC, возможен остаток.','risk-note'));
  const technical=detail('Детали расчёта · книги, комиссии, проскальзывание',p);
  if(Object.values(p.legs).some(l=>l.connection_id==='extended'))technical.append(node('p','Extended: Public подключён · Private не настроен. Аккаунт не прочитан; MARKET = защищённый IOC, возможен остаток.','hint'));
  box.append(technical);
}
function metric(title,value,qty,target) {const d=node('div',undefined,'metric');d.append(node('span',title),node('strong',value));if(qty!==undefined){const p=node('progress');p.max=Number(target);p.value=Number(qty);p.setAttribute('aria-label',title);d.append(p);}return d;}
function renderIntent(i) {
  const block=node('article',undefined,'intent');block.dataset.intentId=i.id;
  const [a,b]=roles(i),p=i.preview,unit=p.symbol,stage=strategyStage(i);
  const title=node('div',undefined,'intent-title');title.append(node('h3',`${unit} · ${Object.values(p.legs).map(l=>names[l.connection_id]||l.connection_id||l.account).join(' / ')}`),node('span',stage,'state'+(stage==='Требует внимания'?' attention':'')));block.append(title);
  if(i.strategy_mode!=='DN_FULL_LIMIT_MARKET_V3')block.append(node('p','Архивный режим v1/v2 · исходная последовательность сохранена','hint'));
  const metrics=node('div',undefined,'metrics');metrics.append(metric('Первая нога · набрано / цель',`${i.filled_base[a]} / ${p.quantity_base} ${unit}`,i.filled_base[a],p.quantity_base),metric('Хедж · исполнено / цель',`${i.filled_base[b]} / ${p.quantity_base} ${unit}`,i.filled_base[b],p.quantity_base),metric('Нехеджированный остаток',`${i.residual_base} ${unit}`));block.append(metrics);
  if(unknown(i))block.append(node('p',`Результат заявки неизвестен. Неподтверждённый хедж: ${i.pending_unknown_hedge_base} ${unit}. Возможный остаток: ${(i.possible_residual_base_bounds||[]).join(' … ')} ${unit}. Слепого повтора и автоматического закрытия нет.`,'risk-warning'));
  else if(Number(i.residual_base)!==0)block.append(node('p',`Открыта направленная позиция: ${i.residual_base} ${unit}. ${i.status==='STOPPED_SYNTHETIC'?'Автоматического закрытия нет.':'Первая нога ещё не полностью захеджирована.'}`,'risk-warning'));
  if(i.stop_reason)block.append(node('p','Причина: '+i.stop_reason,'risk-warning'));
  const heading=node('div',undefined,'log-heading');heading.append(node('h3','Журнал исполнения'),node('span','Сохранённые события симуляции · не live-поток'));block.append(heading);
  const log=node('ol',undefined,'timeline');executionLines(i).forEach(e=>{const item=node('li',undefined,e.warning?'warning':'');if(e.time){const t=node('time',new Date(e.time*1000).toLocaleTimeString('ru-RU',{hour12:false}));t.dateTime=new Date(e.time*1000).toISOString();item.append(t);}item.append(node('span',e.text));log.append(item);});block.append(log);
  if(!log.childElementCount)block.append(node('p','Подтверждённых исполнений пока нет.','muted'));
  const technical=detail('Детали операции · '+i.id,i),actions=node('div',undefined,'actions'),resume=node('button','Сверить / точный повтор','secondary'),inspect=node('button','Сохранённый расчёт','secondary');resume.type=inspect.type='button';
  resume.addEventListener('click',()=>task(async()=>{const result=await api('/api/resume',{intent_id:i.id});await refresh();message(result.status==='STOPPED_SYNTHETIC'?'Сверка завершена. Стратегия требует внимания; новая заявка не создавалась.':'Сверка завершена по сохранённому журналу.');}));
  inspect.addEventListener('click',()=>{if(busy)return;technical.append(detail('Сохранённый расчёт',i.preview));});actions.append(resume,inspect);technical.append(actions,detail('Исходный журнал',i.journal));block.append(technical);return block;
}
async function refreshLiveWorker() {
  const target=$('live-worker-status');
  try {
    const state=await api('/api/live-worker-status');
    if(state.mode!=='LOCAL_PRIVATE_DN_STATUS')throw new Error('Unexpected worker projection');
    target.replaceChildren(node('strong',state.worker_process_active?'Локальный worker активен · состояние только для чтения':'Live worker не запущен'),node('p','Наблюдения из сохранённого журнала, не гарантия текущих позиций на бирже.'));
    for(const task of state.tasks)target.append(node('p',`${task.status} · ${task.phase} · первая ${task.primary_filled}, хедж ${task.hedge_filled}, остаток ${task.residual_base}`));
    target.append(detail('Сохранённый статус исполнителя',state));
  } catch (_) {target.replaceChildren(node('p','Статус live-исполнителя недоступен; состояние позиций неизвестно.','risk-warning'));}
}
async function refresh() {const state=await api('/api/state');if(state.mode!=='SYNTHETIC_ONLY')throw new Error('Неожиданный режим сервера');$('strategy-empty').hidden=state.intents.length>0;$('journal').replaceChildren(...state.intents.slice().reverse().map(renderIntent));$('inventory-details').replaceChildren(detail('Локальные позиции симуляции',state.inventory));await refreshLiveWorker();}
function invalidate() {draft=null;submitted=false;$('acknowledge').checked=false;$('review').hidden=true;controls();}
function preset(values) {if(busy)return;Object.entries(values).forEach(([k,v])=>$(k).value=v);invalidate();message('Пара выбрана. Комиссии задаются отдельно; книги ещё не запрошены.');}
async function boot() {
  $('intent_id').value='paper-'+Date.now();
  $('intent-form').addEventListener('invalid',e=>{const d=e.target.closest('details');if(d)d.open=true;},true);
  $('intent-form').addEventListener('input',()=>{invalidate();message('Параметры изменены. Нужен новый расчёт.');});
  $('acknowledge').addEventListener('change',controls);
  $('edit-button').addEventListener('click',()=>{invalidate();message('Измените параметры или обновите расчёт.');});
  $('intent-form').addEventListener('submit',e=>{e.preventDefault();task(async()=>{invalidate();message('Проверяем пару и публичные книги…');const d=await api('/api/preview',{intent_id:$('intent_id').value,params:params()});draft=d;renderPreview(d.preview);$('review').hidden=false;$('advanced').open=false;message('Расчёт готов. Проверьте ноги и подтвердите запуск.');});});
  $('paper-button').addEventListener('click',()=>task(async()=>{if(!draft||!$('acknowledge').checked||ageExpired())throw new Error('Нужен актуальный расчёт и подтверждение');const result=await api('/api/paper',{intent_id:draft.intent_id,params:draft.params,preview_id:draft.preview_id,assume_empty_simulated:true});submitted=true;await refresh();$('review').hidden=true;message(result.status==='STOPPED_SYNTHETIC'?'Стратегия требует внимания. Результат и остаток показаны ниже.':'Симуляция завершена. Исполнения сохранены в журнале.',result.status==='STOPPED_SYNTHETIC');}));
  $('refresh').addEventListener('click',()=>task(async()=>{await refresh();message('Сохранённое состояние обновлено.');}));
  $('hyperliquid-preset').addEventListener('click',()=>preset({primary_venue:'hyperliquid',hedge_venue:'core',market:'ETH',hedge_market:'ETH',quantity:'0.02',max_notional_usd:'100',max_roundtrip_cost_usd:'2'}));
  $('entropy-preset').addEventListener('click',()=>preset({primary_venue:'entropy',hedge_venue:'core',market:'io:NVDA',hedge_market:'NVDA',quantity:'1',max_notional_usd:'500',max_roundtrip_cost_usd:'10'}));
  ['hyperliquid','entropy'].forEach(id=>$(id+'-catalog-button').addEventListener('click',()=>task(async()=>{$(id+'-catalog').replaceChildren(detail('Публичный каталог · точные инструменты',await api('/api/'+id+'-markets')));message('Каталог прочитан. Пара и ордера не создавались.');})));
  setInterval(controls,1000);
  await task(async()=>{token=(await api('/api/session')).csrf_token;
    const connections=await api('/api/hl-connection-status');connections.forEach(c=>$(c.id+'-status').replaceChildren(node('strong',names[c.id]),node('p','Public доступен, Private выключен. Отдельные настройки аккаунта/agent/nonce: '+c.future_account_config_scope),detail('Статус подключения',c)));
    const connectors=await api('/api/connector-status');$('connector-status').replaceChildren(detail('Совместимость коннекторов и зависимости',connectors));connectors.filter(c=>!c.paper_selectable).forEach(c=>{const option=node('option',c.id+' · недоступен');option.value=c.id;option.disabled=true;$('primary_venue').append(option);});
    const foundation=await api('/api/live-foundation-status');
    $('connector-status').append(detail('Live worker не запущен · Signer не активирован',foundation));
    await refresh();message('');
  });
}
if(typeof document!=='undefined')boot();

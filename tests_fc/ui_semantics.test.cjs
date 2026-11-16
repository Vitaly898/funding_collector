'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const context = vm.createContext({console});
vm.runInContext(fs.readFileSync('funding_collector/ui/app.js','utf8').split("\n$('intent_id').value =")[0],context);
function intent(extra={}) {return {status:'RUNNING_SYNTHETIC',strategy_mode:'DN_FULL_LIMIT_MARKET_V3',preview:{quantity_base:'1',symbol:'ETH',legs:{leg_a:{connection_id:'core'},leg_b:{connection_id:'rh'}}},filled_base:{leg_a:'0',leg_b:'0'},residual_base:'0',pending_unknown_hedge_base:'0',journal:[],...extra};}
test('persisted stages: wait, accumulation, primary done, hedge, complete, unknown',()=>{
 const stage=i=>context.strategyStage(i);
 assert.equal(stage(intent()),'Ожидание');
 assert.equal(stage(intent({filled_base:{leg_a:'0.5',leg_b:'0'},residual_base:'0.5'})),'Набираем первую ногу');
 assert.equal(stage(intent({filled_base:{leg_a:'1',leg_b:'0'},residual_base:'1'})),'Первая нога готова');
 assert.equal(stage(intent({filled_base:{leg_a:'1',leg_b:'0'},journal:[{kind:'market_hedge_intent'}]})),'Хеджируем');
 assert.equal(stage(intent({status:'COMPLETED_SYNTHETIC',filled_base:{leg_a:'1',leg_b:'1'}})),'Готово');
 assert.equal(stage(intent({status:'COMPLETED_SYNTHETIC',residual_base:'0.2'})),'Требует внимания');
 assert.equal(stage(intent({status:'STOPPED_SYNTHETIC',pending_unknown_hedge_base:'1'})),'Требует внимания');
});
test('journal distinguishes local intent from submitted order and confirmed fill',()=>{
 const i=intent({journal:[{kind:'limit_intent'},{kind:'fill',venue:'leg_a',quantity:'0.4',vwap:'100'},{kind:'fill',venue:'leg_a',quantity:'0.6',vwap:'101'},{kind:'market_hedge_intent',quantity_base:'1'},{kind:'stop',reason:'market outcome UNKNOWN',unknown_hedge_base:'1'}],pending_unknown_hedge_base:'1'});
 const lines=Array.from(context.executionLines(i),x=>x.text).join('\n');
 assert.match(lines,/Лимитка №1 исполнена: 0.4 ETH/);
 assert.match(lines,/Лимитка №2 исполнена: 0.6 ETH/);
 assert.match(lines,/Первая нога собрана: 1 ETH/);
 assert.match(lines,/Маркетный хедж подготовлен/);
 assert.match(lines,/Результат заявки неизвестен/);
 assert.doesNotMatch(lines,/хедж (отправлен|исполнен)/);
 i.journal.push({kind:'fill',venue:'leg_b',quantity:'1',vwap:'100'});
 assert.match(Array.from(context.executionLines(i),x=>x.text).join('\n'),/Маркетный хедж исполнен: 1 ETH/);
});
test('legacy history is not reinterpreted as full-limit market sequence',()=>{
 const i=intent({strategy_mode:'LEGACY_PER_PARTIAL_V1_V2',journal:[{kind:'fill',venue:'leg_a',quantity:'1',vwap:'100'},{kind:'fill',venue:'leg_b',quantity:'1',vwap:'100'}]});
 const text=Array.from(context.executionLines(i),x=>x.text).join('\n');
 assert.doesNotMatch(text,/Лимитка|Маркетный/);
 assert.match(text,/Исполнение первой ноги/);
});
test('compact launch, closed details, explicit fee assumptions and safe DOM output',()=>{
 const html=fs.readFileSync('funding_collector/ui/index.html','utf8');
 assert.match(html,/Симуляция · реальные ордера отключены/);
 assert.match(html,/id="review"[^>]*hidden/);
 assert.match(html,/id="advanced"/);
 assert.match(html,/id="fee-state"/);
 assert.match(html,/id="strategy-empty"/);
 assert.doesNotMatch(html,/<details[^>]*\sopen[\s>]/);
 assert.doesNotMatch(html,/stop-button/);
 const js=fs.readFileSync('funding_collector/ui/app.js','utf8');
 assert.doesNotMatch(js,/innerHTML|insertAdjacentHTML/);
 assert.match(js,/textContent/);
 assert.match(js,/\/api\/live-foundation-status/);
 assert.match(js,/\/api\/live-worker-status/);
 assert.match(html,/id="live-worker-status"/);
 assert.match(js,/Live worker не запущен/);
 assert.match(js,/Signer не активирован/);
});

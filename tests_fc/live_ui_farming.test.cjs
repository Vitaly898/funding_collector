'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
const app=require('../funding_collector/live_ui/app.js');
const fs=require('node:fs');
test('farming serializes only opt-in bounded policy and rejects unsupported/infinite',()=>{
 const data={farming_enabled:'on',hold_seconds:'60',min_delay_seconds:'1',max_delay_seconds:'5',max_cycles:'2',max_turnover:'25000',max_fees:'1'};
 const venues=['hyperliquid-mainnet-native','lighter-core-mainnet'];
 assert.equal(app.buildFarmingPolicy({},venues),undefined);
 assert.deepEqual(app.buildFarmingPolicy(data,venues),{hold_seconds:60,min_delay_seconds:1,max_delay_seconds:5,max_cycles:2,max_turnover:'25000',max_fees:'1'});
 for(const change of [{max_cycles:'Infinity'},{max_cycles:'11'},{hold_seconds:'59'},{max_fees:'NaN'},{min_delay_seconds:'6'}]) assert.throws(()=>app.buildFarmingPolicy({...data,...change},venues));
 assert.throws(()=>app.buildFarmingPolicy(data,['entropy-mainnet-io',venues[1]]));
 const html=fs.readFileSync('funding_collector/live_ui/index.html','utf8');
 assert.match(html,/name="farming_enabled"/);
 assert.match(html,/Бесконечный режим не поддерживается/);
});
test('bounded farming observations have explicit Russian lifecycle labels',()=>{
 assert.equal(app.statusText('DONE'),'Циклы завершены · обе ноги закрыты');
 assert.equal(app.eventText('both_legs_hedged_hold_started'),'Обе ноги подтверждены; начался отсчёт удержания');
 assert.equal(app.eventText('write_ahead_close_primary'),'Подготовлено reduce-only закрытие первой ноги');
 assert.equal(app.eventText('flat_random_delay_persisted'),'Обе ноги закрыты; сохранена задержка перед следующим циклом');
 assert.equal(app.eventText('farming_requires_attention'),'Циклы остановлены; нужна ручная сверка позиций и заявок');
});

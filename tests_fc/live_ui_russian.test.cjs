'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../funding_collector/live_ui/index.html'),'utf8');
const app=require('../funding_collector/live_ui/app.js');
test('manual strategy has Russian fields and collapsed IOC and raw diagnostics',()=>{
 for(const label of ['Первая биржа','Вторая биржа','Пара на первой бирже','Пара на второй бирже','Направление первой ноги','Объём'])assert.ok(html.includes(label),label);
 const advanced=html.match(/<details id="advanced-execution"[\s\S]*?<\/details>/)?.[0];
 assert.ok(advanced);assert.ok(!advanced.split('>')[0].includes('open'));
 assert.match(advanced,/name="protection_bps"[^>]*required/);
 assert.match(html,/<details id="execution-diagnostics"><summary>Технический журнал<\/summary><pre id="raw-state">/);
 assert.ok(!html.includes('Primary symbol'));
});
test('Russian presentation never exposes unknown internal status or event names',()=>{
 assert.equal(app.statusText('COMPLETED_CONFIRMED'),'Исполнено');
 assert.equal(app.eventText('primary_confirmed_trades'),'Подтверждены сделки на первой бирже');
 assert.equal(app.eventText('internal-new-event'),'Состояние обновлено');
 assert.equal(app.strategyName('browser-12345678-1234-1234-1234-123456789abc'),'browser');
 assert.equal(app.strategyName('dn-12345678-1234-1234-1234-123456789abc'),'Ручная стратегия');
});

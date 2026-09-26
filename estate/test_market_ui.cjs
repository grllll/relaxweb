// Run with NODE_PATH pointing to the bundled node_modules containing Playwright.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || 'python';
const snapshot = JSON.parse(execFileSync(python, ['-B', '-c', `
import json, sqlite3, time
from estate import init_estate, estate_state
conn=sqlite3.connect(':memory:')
conn.execute('CREATE TABLE users(username TEXT PRIMARY KEY,coins REAL NOT NULL)')
conn.execute("INSERT INTO users VALUES ('alice',10000)")
init_estate(conn)
print(json.dumps(estate_state(conn,'alice',int(time.time()))))
`], { cwd: root, encoding: 'utf8' }));

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.CHROME_PATH || chromium.executablePath(), args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 800}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://estate-market.test/**', async route => {
      const file = path.resolve(root, '.' + new URL(route.request().url()).pathname);
      if (!file.startsWith(root + path.sep)) return route.fulfill({status: 403});
      try {
        const body = await fs.readFile(file);
        const contentType = {'.html': 'text/html', '.js': 'text/javascript',
          '.css': 'text/css', '.png': 'image/png', '.json': 'application/json'}[path.extname(file)] || 'application/octet-stream';
        await route.fulfill({body, contentType});
      } catch { await route.fulfill({status: 404}); }
    });
    await page.addInitScript(() => {
      window.sent = [];
      window.WebSocket = class {
        static OPEN = 1;
        constructor() { this.readyState = 1; }
        send(data) { sent.push(JSON.parse(data)); }
        addEventListener() {}
        close() {}
      };
    });
    await page.goto('http://estate-market.test/game.html');
    await page.evaluate(async data => {
      window.core = await import('/assets/js/core.js');
      core.setSignedIn({username: 'alice', nickname: 'Alice', coins: 10000});
      core.state.hallPage = 'estate'; core.renderGameView();
      core.handleServerMessage({type: 'estate_state', ...data});
      window.marketUi = (await import('/assets/js/estate/ui.js')).createEstateUI(document.querySelector('.estate-root'));
    }, snapshot);
    // 入口在顶栏，不再藏在抽奖马戏团里
    await page.getByRole('button', {name: '📈 股市'}).click();
    await page.waitForFunction(() => sent.some(message => message.type === 'estate_market_get'));
    assert.match(await page.locator('.estate-sheet-title').innerText(), /股市/);
    const firstRequest = await page.evaluate(() => sent.find(message => message.type === 'estate_market_get'));
    assert.equal(firstRequest.symbol, 'XTIDE');
    const request = await page.evaluate(() => sent.find(message => message.type === 'estate_market_get'));
    const market = {symbol: 'XTIDE', name: '星潮模拟指数', blurb: '基准档', price: 1000,
      anchor: 1000, quote_minute: 123,
      symbols: [{symbol: 'XTIDE', name: '星潮模拟指数', price: 1000, blurb: '基准档'},
        {symbol: 'XCROP', name: '庄园农业板', price: 620, blurb: '稳健'},
        {symbol: 'XORE', name: '深矿资源板', price: 1480, blurb: '刺激'}],
      available: true, fee_rate: 0.0025, maker_fee_rate: 0.0005, shares: 0, coins: 10000,
      capacity_left: 2000, tradable_buy: 2000, tradable_sell: 2000, trend_paused: false,
      split_count: 2, last_split_minute: 33333333,
      cost_basis: 0, market_value: 0, realized_pnl: 0,
      book: {bids: [{price: 999, quantity: 1}, {price: 998, quantity: 2}],
        asks: [{price: 1001, quantity: 1}, {price: 1002, quantity: 2}]},
      flow_left: {buy: 20, sell: 20},
      positions: [{username: 'bob', shares: 12, market_value: 12000, cost_basis: 9000,
        unrealized_pnl: 3000, realized_pnl: 50}],
      orders: [{id: 7, side: 'buy', price: 990, quantity: 5, filled: 0, remaining: 5,
        expires_minute: 123}],
      fills: [{time: 120, username: 'bob', side: 'sell', price: 995, quantity: 1, amount: 995}],
      my_fills: [{time: 60, username: 'alice', side: 'buy', price: 998, quantity: 0.5, amount: 499}],
      history: [{time: 60, price: 995}, {time: 120, price: 1000}],
      volume: {minute: {shares: 12.5, amount: 12500}, day: {shares: 340.25, amount: 341000}},
      candles: {
        minute: [{time: 60, open: 990, high: 995, low: 990, close: 995, volume: 8, amount: 7960},
          {time: 120, open: 995, high: 1002, low: 995, close: 1000, volume: 12.5, amount: 12500}],
        hour: [{time: 0, open: 990, high: 1002, low: 990, close: 1000, volume: 20.5, amount: 20460}],
        day: [{time: 0, open: 990, high: 1002, low: 990, close: 1000, volume: 340.25, amount: 341000}],
      }};
    await page.evaluate(({request, market}) => core.handleServerMessage({
      type: 'estate_market_state', request_id: request.request_id, market,
    }), {request, market});
    assert.match(await page.locator('.estate-market > .estate-sheet-note').first().innerText(), /手续费 吃单 0.25% \/ 挂单 0.05%/);
    const volumeLine = await page.locator('.estate-market-volume').innerText();
    assert.match(volumeLine, /本分钟成交 12\.500 份 · 12,500\.00 金币/);
    assert.match(volumeLine, /今日成交 340\.250 份 · 341,000\.00 金币/);
    assert.equal(await page.locator('.estate-market-chart svg rect[fill-opacity]').count(), 2);
    assert.equal(await page.locator('.estate-symbol-tab').count(), 3);
    assert.equal(await page.locator('.estate-symbol-tab[aria-selected="true"]').innerText(),
      '星潮模拟指数 1,000.00');
    // 切标的会带上 symbol 重新拉快照
    await page.getByRole('tab', {name: /深矿资源板/}).click();
    await page.waitForFunction(() => sent.filter(m => m.type === 'estate_market_get').length === 2);
    const secondRequest = await page.evaluate(() => sent.filter(m => m.type === 'estate_market_get')[1]);
    assert.equal(secondRequest.symbol, 'XORE');
    const ore = {...market, symbol: 'XORE', name: '深矿资源板', price: 1480, anchor: 1400,
      shares: 0, cost_basis: 0, market_value: 0, positions: [], orders: [], fills: [],
      my_fills: []};
    await page.evaluate(({secondRequest, ore}) => core.handleServerMessage({
      type: 'estate_market_state', request_id: secondRequest.request_id, market: ore,
    }), {secondRequest, ore});
    await page.waitForFunction(() => document.querySelector('.estate-symbol-tab[aria-selected="true"]')
      ?.textContent.startsWith('深矿资源板'));
    // 切回基准档继续后面的下单流程
    await page.getByRole('tab', {name: /星潮模拟指数/}).click();
    await page.waitForFunction(() => sent.filter(m => m.type === 'estate_market_get').length === 3);
    const backRequest = await page.evaluate(() => sent.filter(m => m.type === 'estate_market_get')[2]);
    await page.evaluate(({backRequest, market}) => core.handleServerMessage({
      type: 'estate_market_state', request_id: backRequest.request_id, market,
    }), {backRequest, market});
    await page.waitForFunction(() => document.querySelector('.estate-symbol-tab[aria-selected="true"]')
      ?.textContent.startsWith('星潮模拟指数'));
    assert.match(await page.locator('.estate-market-holdings').innerText(), /做市商剩余额度：2,000,000 金币/);
    assert.match(await page.locator('.estate-market').innerText(), /已拆股 2 次（最近一次/);
    assert.equal(await page.locator('.estate-book-row').count(), 4);
    assert.match(await page.locator('.estate-market-orders').innerText(), /#7 买入 990 元 · 剩余 5.000 份/);
    assert.match(await page.locator('.estate-market-fills').innerText(), /bob 卖出 1.000 份 @ 995/);
    assert.match(await page.locator('.estate-market-positions').innerText(), /全服持仓（1 人持有）[\s\S]*bob[\s\S]*12\.000 份 · 市值 12,000\.00 金币/);
    assert.equal(await page.getByRole('img', {name: '分钟K线图'}).count(), 1);
    await page.getByRole('button', {name: '小时K线'}).click();
    assert.equal(await page.getByRole('img', {name: '小时K线图'}).count(), 1);
    await page.getByRole('button', {name: '日K线'}).click();
    assert.equal(await page.getByRole('img', {name: '日K线图'}).count(), 1);
    // 点盘口把价格填进下单框，带价格提交就是限价委托。
    await page.locator('.estate-book-row').first().click();
    assert.equal(await page.getByRole('spinbutton', {name: '委托价格'}).inputValue(), '1002');
    await page.getByRole('spinbutton', {name: '交易份额'}).fill('0.125');
    assert.match(await page.locator('.estate-market-estimate').innerText(), /125/);
    await page.getByRole('button', {name: '买入'}).click();
    const order = await page.evaluate(() => sent.find(message => message.type === 'estate_market_order'));
    assert.deepEqual([order.side, order.price, order.quantity], ['buy', '1002', '0.125']);
    await page.evaluate(({snapshot, order, market}) => core.handleServerMessage({
      type: 'estate_state', ...snapshot, request_id: order.request_id,
      result: {action: 'market_order', side: 'buy', price: 1002, quantity: 0.125, filled: 0.125,
        market: {...market, shares: 0.125, cost_basis: 125.63, market_value: 125}},
    }), {snapshot, order, market});
    await page.waitForFunction(() => document.querySelector('.estate-market-holdings')?.textContent.includes('0.125'));
    assert.match(await page.locator('.estate-market-message').innerText(), /已提交委托：买入 0.125 份，立即成交 0.125 份/);
    // 撤单走 estate_market_cancel，并且流水可以切换成只看自己。
    await page.getByRole('button', {name: '撤单'}).click();
    const cancelled = await page.evaluate(() => sent.find(message => message.type === 'estate_market_cancel'));
    assert.equal(cancelled.order_id, 7);
    await page.evaluate(({snapshot, cancelled, market}) => core.handleServerMessage({
      type: 'estate_state', ...snapshot, request_id: cancelled.request_id,
      result: {action: 'market_cancel', order_id: 7, market: {...market, orders: [], shares: 0.125}},
    }), {snapshot, cancelled, market});
    await page.getByRole('button', {name: '只看我的'}).click();
    assert.match(await page.locator('.estate-market-fills').innerText(), /alice 买入 0.500 份 @ 998/);
    assert.match(await page.locator('.estate-market-fills').innerText(), /看全服/);
    // 不带价格提交就是市价单。
    await page.getByRole('spinbutton', {name: '委托价格'}).fill('');
    await page.getByRole('button', {name: '卖出'}).click();
    const trade = await page.evaluate(() => sent.find(message => message.type === 'estate_market_trade'));
    assert.deepEqual([trade.side, trade.quantity], ['sell', '0.125']);
    await page.evaluate(({snapshot, trade, market}) => core.handleServerMessage({
      type: 'estate_state', ...snapshot, request_id: trade.request_id,
      result: {action: 'market_trade', side: 'sell', quantity: 0.125, price: 1000,
        average_price: 999, amount: 124.38,
        market: {...market, shares: 0, cost_basis: 0, market_value: 0}},
    }), {snapshot, trade, market});
    await page.waitForFunction(() => document.querySelector('.estate-market-holdings')?.textContent.includes('持有份额：0.000'));
    assert.match(await page.locator('.estate-market-message').innerText(), /已按 999.00 点卖出 0.125 份/);
    await page.evaluate(async () => {
      const {estateStore} = await import('/assets/js/estate/state.js');
      estateStore.snapshot.profile.pet_level = 4;
      estateStore.snapshot.profile.penguin_level = 1;
      estateStore.snapshot.profile.active_pet = 'stinky_penguin';
      marketUi.interact({kind: 'general_store'});
    });
    assert.equal(await page.getByRole('button', {name: '设为出场宠物'}).count(), 1);
    await page.getByRole('button', {name: '设为出场宠物'}).click();
    assert.equal((await page.evaluate(() => sent.at(-1))).pet, 'doudou');
    await page.evaluate(() => marketUi.interact({kind: 'lottery'}));
    await page.getByRole('button', {name: '查看抽奖记录'}).click();
    await page.waitForFunction(() => sent.some(message => message.type === 'estate_lottery_history'));
    const historyRequest = await page.evaluate(() => sent.find(message => message.type === 'estate_lottery_history'));
    await page.evaluate(requestId => core.handleServerMessage({type: 'estate_lottery_history',
      request_id: requestId, history: [{created_at: 2000000000, award: 'coins_250', coins_awarded: 250}]}),
    historyRequest.request_id);
    assert.match(await page.locator('.estate-lottery-history').innerText(), /250 金币/);
    assert.deepEqual(errors, []);
    console.log('PASS topbar market entry, symbol switch, book, limit order, cancel and tape');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

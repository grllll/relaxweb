"use strict";

import { alertDialog } from "../dialog.js";
import { estateRequest } from "./protocol.js";

const format = (value, digits = 2) => Number(value || 0).toLocaleString("zh-CN", {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
});
const PERIODS = [["minute", "分钟"], ["hour", "小时"], ["day", "日"]];
let selectedPeriod = "minute";
// 下单框的草稿：每次提交后面板会整块重建，草稿让价格与份额不至于被清空。
let draftPrice = "";
let draftQuantity = "";

function marketChart(candles, period) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 720 244"); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${PERIODS.find(([key]) => key === period)[1]}K线图`);
  const ns = "http://www.w3.org/2000/svg";
  const low = Math.min(...candles.map((item) => Number(item.low)));
  const high = Math.max(...candles.map((item) => Number(item.high)));
  const padding = Math.max((high - low) * 0.08, high * 0.001);
  const bottom = low - padding; const range = high - low + padding * 2;
  const y = (value) => 168 - (value - bottom) / range * 146;
  // 底部 176–206 留给成交量柱，柱高按窗口内最大成交量归一。
  const volumes = candles.map((item) => Number(item.volume) || 0);
  const peak = Math.max(...volumes, 0);
  const add = (tag, attrs) => {
    const node = document.createElementNS(ns, tag);
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
    svg.append(node); return node;
  };
  for (const value of [high, (high + low) / 2, low]) {
    const ordinate = y(value);
    add("line", {x1: 54, x2: 706, y1: ordinate, y2: ordinate, stroke: "#dccbaa", "stroke-dasharray": "3 4"});
    const label = add("text", {x: 47, y: ordinate + 4, "text-anchor": "end", fill: "#7d674c", "font-size": 11});
    label.textContent = format(value);
  }
  const step = 646 / candles.length;
  const bodyWidth = Math.max(2, Math.min(12, step * 0.68));
  candles.forEach((item, index) => {
    const x = 58 + step * (index + 0.5);
    const rising = item.close >= item.open;
    const color = rising ? "#b95245" : "#358266";
    add("line", {x1: x, x2: x, y1: y(item.high), y2: y(item.low), stroke: color, "stroke-width": 1.5});
    const top = Math.min(y(item.open), y(item.close));
    const body = add("rect", {x: x - bodyWidth / 2, y: top, width: bodyWidth,
      height: Math.max(2, Math.abs(y(item.open) - y(item.close))), fill: color});
    const title = document.createElementNS(ns, "title");
    title.textContent = `${new Date(item.time * 1000).toLocaleString("zh-CN")}  开 ${format(item.open)}  高 ${format(item.high)}  低 ${format(item.low)}  收 ${format(item.close)}`;
    body.append(title);
    const volume = Number(item.volume) || 0;
    if (peak > 0 && volume > 0) {
      const height = Math.max(1, volume / peak * 30);
      const bar = add("rect", {x: x - bodyWidth / 2, y: 206 - height, width: bodyWidth,
        height, fill: color, "fill-opacity": 0.55});
      const hint = document.createElementNS(ns, "title");
      hint.textContent = `${new Date(item.time * 1000).toLocaleString("zh-CN")}  成交 ${format(volume, 3)} 份 · ${format(item.amount)} 金币`;
      bar.append(hint);
    }
  });
  add("line", {x1: 54, x2: 706, y1: 206, y2: 206, stroke: "#cbb691"});
  for (const index of new Set([0, Math.floor((candles.length - 1) / 2), candles.length - 1])) {
    const item = candles[index];
    const label = add("text", {x: 58 + step * (index + 0.5), y: 232,
      "text-anchor": index === 0 ? "start" : index === candles.length - 1 ? "end" : "middle",
      fill: "#7d674c", "font-size": 11});
    label.textContent = new Date(item.time * 1000).toLocaleString("zh-CN", period === "day"
      ? {month: "numeric", day: "numeric"} : {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
  }
  return svg;
}

export function renderMarketGame(target, market, onTrade, notice = "", onSelect = null) {
  const wrap = document.createElement("div"); wrap.className = "estate-market";
  if (!market) {
    const loading = document.createElement("p"); loading.className = "estate-sheet-note";
    loading.textContent = "正在获取模拟指数…"; wrap.append(loading); target.append(wrap); return;
  }
  if (onSelect) target.append(symbolBar(market, onSelect));
  const title = document.createElement("div"); title.className = "estate-market-quote";
  const name = document.createElement("strong"); name.textContent = market.name;
  const price = document.createElement("b"); price.textContent = `${format(market.price)} 点`;
  title.append(name, price); wrap.append(title);
  const status = document.createElement("p"); status.className = "estate-sheet-note";
  const trend = market.trend_paused ? "宏观趋势暂缓（本窗口印钞额度已用尽）" : "长期趋势约每周 +5%";
  status.textContent = market.available
    ? `游戏内模拟行情 · ${trend}、短期最大波动 ±60% · 每 10 秒撮合 · 手续费 吃单 ${(market.fee_rate * 100).toFixed(2)}% / 挂单 ${((market.maker_fee_rate ?? market.fee_rate) * 100).toFixed(2)}%`
    : "行情暂不可用；可查看已有持仓。";
  wrap.append(status);
  if (market.split_count > 0) {
    const split = document.createElement("p"); split.className = "estate-sheet-note";
    const when = market.last_split_minute > 0
      ? new Date(market.last_split_minute * 60000).toLocaleDateString("zh-CN") : "";
    split.textContent = `已拆股 ${format(market.split_count, 0)} 次（最近一次${when ? ` ${when}` : ""}）：`
      + "份额 ×2、价格 ÷2，持仓成本与金币金额不变，历史 K 线已按拆股折算。";
    wrap.append(split);
  }
  const volume = document.createElement("p"); volume.className = "estate-sheet-note estate-market-volume";
  const minuteVolume = market.volume?.minute || {};
  const dayVolume = market.volume?.day || {};
  volume.textContent = `本分钟成交 ${format(minuteVolume.shares ?? 0, 3)} 份 · ${format(minuteVolume.amount ?? 0)} 金币`
    + ` ｜ 今日成交 ${format(dayVolume.shares ?? 0, 3)} 份 · ${format(dayVolume.amount ?? 0)} 金币`;
  wrap.append(volume);
  const chartControls = document.createElement("div"); chartControls.className = "estate-market-periods";
  const chart = document.createElement("div"); chart.className = "estate-market-chart";
  const drawChart = () => {
    const candles = market.candles?.[selectedPeriod] || [];
    chart.replaceChildren();
    if (candles.length) chart.append(marketChart(candles, selectedPeriod));
    else chart.textContent = "暂无这一周期的采样记录。";
    for (const control of chartControls.children) {
      control.setAttribute("aria-pressed", String(control.dataset.period === selectedPeriod));
    }
  };
  for (const [period, label] of PERIODS) {
    const control = document.createElement("button"); control.type = "button";
    control.className = "estate-button"; control.dataset.period = period;
    control.textContent = `${label}K线`;
    control.addEventListener("click", () => { selectedPeriod = period; drawChart(); });
    chartControls.append(control);
  }
  wrap.append(chartControls, chart);
  drawChart();
  const chartNote = document.createElement("p"); chartNote.className = "estate-market-disclosure";
  chartNote.textContent = "K 线由服务端每 10 秒的报价汇总成每分钟开高低收，下方柱状为成交量；红色上涨、绿色下跌。无采样的时段不会补造数据。";
  wrap.append(chartNote);
  wrap.append(bookSection(market, (price) => {
    draftPrice = String(price);
    priceInput.value = draftPrice;
    updateEstimate();
  }));
  const priceInput = document.createElement("input"); priceInput.type = "number";
  priceInput.min = "1"; priceInput.step = "1"; priceInput.placeholder = "价格（元），留空为市价";
  priceInput.setAttribute("aria-label", "委托价格");
  priceInput.value = draftPrice;
  const input = document.createElement("input"); input.type = "number";
  input.min = "0.001"; input.step = "0.001"; input.placeholder = "份额，例如 0.5";
  input.setAttribute("aria-label", "交易份额");
  input.value = draftQuantity;
  const buy = document.createElement("button"); buy.type = "button";
  buy.className = "estate-button estate-button-gold"; buy.textContent = "买入";
  const sell = document.createElement("button"); sell.type = "button";
  sell.className = "estate-button"; sell.textContent = "卖出";
  buy.disabled = !market.available; sell.disabled = !market.available;
  const message = document.createElement("p"); message.className = "estate-market-message";
  message.setAttribute("role", "status");
  message.textContent = notice;
  const estimate = document.createElement("p"); estimate.className = "estate-market-disclosure estate-market-estimate";
  const updateEstimate = () => {
    const quantity = Number(input.value);
    const price = Number(priceInput.value) || market.price;
    if (Number.isFinite(quantity) && quantity > 0) {
      estimate.textContent = `估算买入支出 ${format(quantity * price * (1 + market.fee_rate))} 金币；卖出收入 ${format(quantity * price * (1 - market.fee_rate))} 金币。实际金额以成交时服务端报价为准。`;
    } else {
      estimate.textContent = market.price
        ? `现价 ${format(market.price)} 点；本分钟自然流剩余 买 ${format(market.flow_left?.buy ?? 0, 3)} / 卖 ${format(market.flow_left?.sell ?? 0, 3)} 份，超出会提示改用委托。`
        : "请输入份额查看预计支出与收入；支持 0.001 份起交易。";
    }
  };
  priceInput.addEventListener("input", () => { draftPrice = priceInput.value; updateEstimate(); });
  input.addEventListener("input", () => { draftQuantity = input.value; updateEstimate(); });
  const submit = async (side) => {
    const quantity = input.value.trim();
    const price = priceInput.value.trim();
    if (!/^\d+(?:\.\d{1,3})?$/.test(quantity) || Number(quantity) <= 0) {
      message.textContent = "请输入最多三位小数的正数份额。"; return;
    }
    if (price && !/^\d+$/.test(price)) { message.textContent = "委托价格必须是不小于 1 元的整数。"; return; }
    buy.disabled = true; sell.disabled = true;
    message.textContent = price ? "正在提交委托…" : "正在提交交易…";
    try {
      const result = price
        ? await estateRequest("estate_market_order", { side, price, quantity }, { timeoutMs: 15000 })
        : await estateRequest("estate_market_trade", { side, quantity }, { timeoutMs: 15000 });
      onTrade(result);
    } catch (error) {
      message.textContent = error.message || "交易失败，请稍后重试。";
      buy.disabled = !market.available; sell.disabled = !market.available;
    }
  };
  buy.addEventListener("click", () => submit("buy"));
  sell.addEventListener("click", () => submit("sell"));
  const form = document.createElement("div"); form.className = "estate-market-order";
  form.append(priceInput, input, buy, sell);
  wrap.append(form, estimate, message);
  wrap.append(orderSection(market, onTrade));
  wrap.append(fillSection(market));
  const holdings = document.createElement("div"); holdings.className = "estate-market-holdings";
  for (const [label, value] of [
    ["持有份额", format(market.shares, 3)], ["当前市值", `${format(market.market_value)} 金币`],
    ["持仓成本", `${format(market.cost_basis)} 金币`],
    ["浮动盈亏", `${format(market.market_value - market.cost_basis)} 金币`],
    ["已实现盈亏", `${format(market.realized_pnl)} 金币`],
    // 额度按金币计量：价格变动或拆股后这个数字不变，份数会变。
    ["做市商剩余额度", `${format(market.capacity_left * market.price, 0)} 金币`],
  ]) {
    const cell = document.createElement("span");
    cell.textContent = `${label}：${value}`; holdings.append(cell);
  }
  wrap.append(holdings);
  wrap.append(positionSection(market));
  const disclosure = document.createElement("p"); disclosure.className = "estate-market-disclosure";
  disclosure.textContent = "此指数仅使用游戏金币交易，并非真实股票或加密资产；不支持借款、做空或提现。委托单 24 小时未成交会自动撤销。";
  wrap.append(disclosure); target.append(wrap);
}

/** 标的切换条：每只标的的漂移与波动不同，点一下就换盘。 */
function symbolBar(market, onSelect) {
  const wrap = document.createElement("div"); wrap.className = "estate-market-symbols";
  wrap.setAttribute("role", "tablist");
  wrap.setAttribute("aria-label", "标的");
  for (const item of market.symbols || []) {
    const tab = document.createElement("button"); tab.type = "button";
    tab.className = "estate-button estate-symbol-tab";
    tab.setAttribute("role", "tab");
    const current = item.symbol === market.symbol;
    tab.setAttribute("aria-selected", String(current));
    tab.title = item.blurb || "";
    tab.textContent = `${item.name} ${format(item.price, 2)}`;
    tab.addEventListener("click", () => { if (!current) onSelect(item.symbol); });
    wrap.append(tab);
  }
  return wrap;
}

/** 盘口：上下各 5 档，点价格直接填入下单框。 */
function bookSection(market, onPick) {
  const book = market.book || {bids: [], asks: []};
  const wrap = document.createElement("div"); wrap.className = "estate-market-book";
  const rows = [...book.asks].reverse().map((level) => [level, "ask"])
    .concat(book.bids.map((level) => [level, "bid"]));
  if (!rows.length) { wrap.textContent = "暂无盘口。"; return wrap; }
  for (const [level, kind] of rows) {
    const row = document.createElement("button"); row.type = "button";
    row.className = `estate-book-row estate-book-${kind}`;
    const name = document.createElement("span");
    name.textContent = `${kind === "ask" ? "卖" : "买"} ${format(level.price, 0)} 元`;
    const size = document.createElement("span"); size.textContent = `${format(level.quantity, 0)} 份`;
    row.append(name, size);
    row.addEventListener("click", () => onPick(level.price));
    wrap.append(row);
  }
  return wrap;
}

/** 我的委托：剩余份额与撤单。 */
function orderSection(market, onTrade) {
  const wrap = document.createElement("div"); wrap.className = "estate-market-orders";
  const title = document.createElement("p"); title.className = "estate-sheet-note";
  title.textContent = `我的委托（${(market.orders || []).length} 张未成交）`;
  wrap.append(title);
  for (const order of market.orders || []) {
    const row = document.createElement("div"); row.className = "estate-order-row";
    const text = document.createElement("span");
    text.textContent = `#${order.id} ${order.side === "buy" ? "买入" : "卖出"} ${format(order.price, 0)} 元 · 剩余 ${format(order.remaining, 3)} 份`;
    const cancel = document.createElement("button"); cancel.type = "button";
    cancel.className = "estate-button"; cancel.textContent = "撤单";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try { onTrade(await estateRequest("estate_market_cancel", { order_id: order.id }, { timeoutMs: 15000 })); }
      catch (error) { cancel.disabled = false; alertDialog(error.message || "撤销失败"); }
    });
    row.append(text, cancel); wrap.append(row);
  }
  return wrap;
}

/** 全服持仓：谁拿着多少，完全公开。 */
function positionSection(market) {
  const wrap = document.createElement("div"); wrap.className = "estate-market-positions";
  const rows = market.positions || [];
  const title = document.createElement("p"); title.className = "estate-sheet-note";
  title.textContent = `全服持仓（${rows.length} 人持有）`;
  wrap.append(title);
  if (!rows.length) { const empty = document.createElement("div"); empty.textContent = "现在还没有人持仓。"; wrap.append(empty); return wrap; }
  for (const row of rows) {
    const line = document.createElement("div"); line.className = "estate-position-row";
    const who = document.createElement("span"); who.className = "estate-position-who";
    who.textContent = row.username;
    const main = document.createElement("span");
    main.textContent = `${format(row.shares, 3)} 份 · 市值 ${format(row.market_value)} 金币`;
    const pnl = document.createElement("span"); pnl.className = "estate-position-pnl";
    const unrealized = row.unrealized_pnl || 0;
    pnl.textContent = `浮盈 ${unrealized >= 0 ? "+" : ""}${format(unrealized)} · 已实现 ${row.realized_pnl >= 0 ? "+" : ""}${format(row.realized_pnl)}`;
    line.append(who, main, pnl); wrap.append(line);
  }
  return wrap;
}

/** 成交流水：默认全服明细，可切换只看自己的。 */
function fillSection(market) {
  const wrap = document.createElement("div"); wrap.className = "estate-market-fills";
  let mine = false;
  const toggle = document.createElement("button"); toggle.type = "button";
  toggle.className = "estate-button"; toggle.setAttribute("aria-pressed", "false");
  toggle.textContent = "只看我的";
  const list = document.createElement("div");
  const draw = () => {
    const rows = (mine ? market.my_fills : market.fills) || [];
    list.replaceChildren();
    if (!rows.length) { list.textContent = "暂无成交记录。"; return; }
    for (const fill of rows) {
      const row = document.createElement("div"); row.className = "estate-fill-row";
      row.textContent = `${new Date(fill.time * 1000).toLocaleString("zh-CN")} · ${fill.username}`
        + ` ${fill.side === "buy" ? "买入" : "卖出"} ${format(fill.quantity, 3)} 份 @ ${format(fill.price)}`
        + ` · ${format(fill.amount)} 金币`;
      list.append(row);
    }
  };
  toggle.addEventListener("click", () => {
    mine = !mine;
    toggle.setAttribute("aria-pressed", String(mine));
    toggle.textContent = mine ? "看全服" : "只看我的";
    draw();
  });
  draw();
  wrap.append(toggle, list);
  return wrap;
}

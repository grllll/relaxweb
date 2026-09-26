"""Generate a self-contained spectator replay and a complete Markdown transcript."""
import copy
import html
import json
from pathlib import Path

from match import HERE, LABELS, SEATS, STAGES, pretty, load
from policy import RULES

RUN = HERE / "live-match"
TOOL_USE = {
    "standard": "bash + job_output",
    "ptc": "run_code → SDK bash / job_output",
    "minimal": "持久 bash",
    "cordis": "bash + job_output",
}
HIGHLIGHTS = ["顶对下注，全员弃牌", "河牌同花反超三条", "三条击败超对与顶对", "三条击败顶对"]


def amount(x):
    return f"{x:g}"


def action_name(event):
    a, s = event["decision"], event["state"]
    if a["action"] == "raise":
        return ("下注 " if s["current_bet"] == 0 else "加注到 ") + amount(a["raise_to"])
    return {"check": "过牌", "fold": "弃牌", "call": "跟注"}[a["action"]]


def stack_text(stacks):
    return " / ".join(f"{LABELS[p]} {amount(stacks[p])}" for p in SEATS)


def card_html(cards):
    if not cards:
        return '<span class="muted">尚未发出公共牌</span>'
    return " ".join(f'<span class="card {"red" if s in (1, 2) else "black"}">{pretty([(r,s)])}</span>' for r, s in cards)


def table(headers, rows):
    head = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(x))}</td>" for x in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def main():
    events = [json.loads(line) for line in (RUN / "events.jsonl").read_text().splitlines()]
    summary = load(RUN / "summary.json")
    proof = load(RUN / "runtime-proof.json")
    receipts = {p: load(RUN / p / "receipt.json") for p in SEATS}
    for p in SEATS:
        header = proof[p]["physical_header"]
        assert header["agentPreset"] == p
        assert header["id"] == receipts[p]["session_id"] == summary["players"][p]["session_id"]
        assert header["origin"] == "subagent" and header["delegationDepth"] == 1
        assert receipts[p]["strategy_sha256"] == summary["strategy_sha256"]
    assert sum(x["actions"] for x in receipts.values()) == 63
    md = ["# 四种真实 preset · 同策略德扑完整牌谱", "",
          "**已完成：4 个真实子 agent，4 手牌，63 次行动，筹码总量始终为 800。**", "",
          "这是离线 Mock 功能演示：由四个子 agent 各自用原生工具启动玩家客户端，客户端调用同一份确定性策略程序逐动作提交决策。不是大模型逐动作自由决策，也不是随机公平实力测评。人工预设牌堆用于覆盖不同牌局情境；没有真实资金。", "",
          "## 配置与身份验证", "",
          "- 座位顺序：标准 → PTC → 极简 → 创造；庄家依次轮转。",
          "- 初始筹码：每人 200；小盲/大盲：1/2；不补码；连续 4 手。",
          "- 每次请求仅包含该玩家自己的底牌和当前公共信息；回放是公开全部底牌的旁观视角。邮箱采用协作隔离，不是文件系统安全隔离。",
          "- 使用项目现有 HoldemRoom 引擎；其 Mock 发牌省略烧牌步骤。预设但未发出的转牌/河牌不会计入已发生的牌局。",
          "- 临时 preset 启动工具已移除；没有修改四个 preset，也未重启 GUI。", "",
          "| 玩家 / 实际 preset | 独立子会话 ID | 实际执行工具 | 行动数 |",
          "|---|---|---|---:|"]
    for p in SEATS:
        md.append(f"| {LABELS[p]} / `{p}` | `{receipts[p]['session_id']}` | {TOOL_USE[p]} | {receipts[p]['actions']} |")
    md += ["", f"四个会话头均记录了对应的 `agentPreset` 和 `origin: subagent`。可核对[运行时身份证据]({RUN / 'runtime-proof.json'})。", "",
           f"共同策略 SHA-256：`{summary['strategy_sha256']}`", "", "## 完全相同的策略", "", RULES, "",
           "## 最终筹码", "", "| 玩家 | 初始 | 最终 | 净变化 |", "|---|---:|---:|---:|"]
    for p in SEATS:
        md.append(f"| {LABELS[p]} | 200 | {summary['stacks'][p]:g} | {summary['net'][p]:+g} |")
    md += ["", "以下底池数值为引擎中的累计投入。若最后一笔下注无人跟注，结算会说明退回额，不把自己的退注算作净赢利。", ""]
    sections = []
    snapshots = []
    starts = {e["hand"]: e for e in events if e["type"] == "hand_start"}
    ends = {e["hand"]: e for e in events if e["type"] == "hand_end"}
    for hand in range(1, 5):
        start, end = starts[hand], ends[hand]
        result = end["result"]
        winner = "/".join(LABELS[p] for p in result["payouts"])
        intro = f"庄家：{LABELS[start['dealer']]}；小盲：{LABELS[start['small_blind']]} 1；大盲：{LABELS[start['big_blind']]} 2。"
        md += [f"## 第 {hand} 手 · {HIGHLIGHTS[hand-1]}", "", intro, "",
               "开局筹码：" + stack_text(start["stacks"]), "", "| 玩家 | 底牌 |", "|---|---|"]
        for p in SEATS:
            md.append(f"| {LABELS[p]} | {pretty(start['holes'][p])} |")
        md += ["", "### 翻牌前", "", "盲注投入后，底池 3。", "",
               "| # | 玩家 | 动作 | 本次投入 | 底池 | 该玩家付款后筹码 | 策略理由 |", "|---:|---|---|---:|---:|---:|---|"]
        fragment = [f'<section id="hand-{hand}"><div class="section-label">HAND {hand:02}</div><h2>{HIGHLIGHTS[hand-1]}</h2>',
                    f'<p>{intro}</p><p class="muted">开局：{stack_text(start["stacks"])}</p>',
                    '<div class="hole-grid">' + ''.join(f'<div class="hole"><b>{LABELS[p]}</b><div>{card_html(start["holes"][p])}</div></div>' for p in SEATS) + '</div>',
                    '<h3>翻牌前 <small>盲注入池后 3</small></h3>']
        rows = []
        stage = "preflop"
        stacks = dict(start["stacks"])
        stacks[start["small_blind"]] -= 1
        stacks[start["big_blind"]] -= 2
        snap = {"hand": hand, "stage": "翻牌前", "title": intro, "holes": start["holes"], "board": [],
                "stacks": stacks, "folded": [], "actor": None, "pot": 3}
        snapshots.append(copy.deepcopy(snap))
        for event in events:
            if event.get("hand") != hand:
                continue
            if event["type"] == "street":
                fragment.append(table(["#", "玩家", "动作", "投入", "底池", "余码¹", "策略理由"], rows))
                rows = []
                stage = event["stage"]
                board = pretty(event["board"])
                md += ["", f"### {STAGES[stage]}：{board}", "", f"本街开始底池 {event['pot']:g}。", "",
                       "| # | 玩家 | 动作 | 本次投入 | 底池 | 该玩家付款后筹码 | 策略理由 |", "|---:|---|---|---:|---:|---:|---|"]
                fragment.append(f'<h3>{STAGES[stage]} <small>起始底池 {event["pot"]:g}</small></h3><div class="board">{card_html(event["board"])}</div>')
                snap.update(stage=STAGES[stage], board=event["board"], actor=None, title=f"发出{STAGES[stage]}：{board}")
                snapshots.append(copy.deepcopy(snap))
            elif event["type"] == "action":
                s, a = event["state"], event["decision"]
                row = [event["seq"], LABELS[s["player"]], action_name(event), amount(event["paid"]),
                       amount(event["pot_after"]), amount(event["stack_after_payment"]), a["reason"]]
                rows.append(row)
                md.append("| " + " | ".join(map(str, row)) + " |")
                snap.update(stacks=dict(s["stacks"]), folded=list(s["folded"]), actor=s["player"],
                            pot=event["pot_after"], title=f"#{event['seq']:02} {LABELS[s['player']]} {action_name(event)} · {a['reason']}")
                snap["stacks"][s["player"]] = event["stack_after_payment"]
                if a["action"] == "fold":
                    snap["folded"].append(s["player"])
                snapshots.append(copy.deepcopy(snap))
        fragment.append(table(["#", "玩家", "动作", "投入", "底池", "余码¹", "策略理由"], rows))
        contributions = sorted(((p["committed"], p["username"]) for p in result["hands"]), reverse=True)
        refund = contributions[0][0] - contributions[1][0]
        notes = f"{winner} 收到 {result['pot']:g}，"
        if refund:
            notes += f"其中无人跟注的 {refund:g} 退回；实际争夺底池 {result['pot']-refund:g}。"
        else:
            notes += "无未跟注退注。"
        md += ["", "### 摊牌与结算", "", notes, "", "| 玩家 | 本手累计投入 | 牌型/状态 | 返还及分池 | 本手净变化 | 结余 |", "|---|---:|---|---:|---:|---:|"]
        settlement_rows = []
        for p in SEATS:
            row = next(x for x in result["hands"] if x["username"] == p)
            status = "弃牌" if row["folded"] else (row["hand_name"] or "对手全弃，无需摊牌")
            values = [LABELS[p], amount(row["committed"]), status, amount(result["payouts"].get(p, 0)),
                      f"{end['net'][p]:+g}", amount(end["stacks"][p])]
            settlement_rows.append(values)
            md.append("| " + " | ".join(values) + " |")
        if hand == 1:
            md += ["", "本手在翻牌圈结束；转牌、河牌没有发出。展示胜者底牌仅供旁观复核，并非要求其摊牌。"]
        fragment += ['<div class="settlement"><h3>摊牌与结算</h3>', f'<p>{notes}</p>',
                     table(["玩家", "累计投入", "牌型/状态", "返还及分池", "净变化", "结余"], settlement_rows), '</div></section>']
        sections.append("\n".join(fragment))
        snap.update(stacks=end["stacks"], pot=0, actor=None, title=f"第 {hand} 手结算：{notes}")
        snapshots.append(copy.deepcopy(snap))
        md.append("")
    md += ["## 校验与复现", "", "- 四位玩家分别提交了 16 / 21 / 15 / 11 个决策，合计 63。",
           "- 63/63 决策与共同策略逐字段一致，且均通过引擎合法动作检查。",
           "- 逐动作验证筹码守恒 800；每手投入总和等于分池总和。",
           "- 独立牌型计算器与项目引擎完成 6,000 组固定种子交叉验证。",
           "- 每手完整 52 张牌无重复；程序化复现结果与真实四客户端对局一致。", "",
           f"- [原始事件 JSONL]({RUN / 'events.jsonl'}) · [结果 JSON]({RUN / 'summary.json'}) · [preset 身份证据]({RUN / 'runtime-proof.json'})",
           f"- [共享策略]({HERE / 'policy.py'}) · [Mock 裁判与客户端]({HERE / 'match.py'})", "",
           "本地复现（不再创建子 agent，只核对策略和发牌的确定性）：", "", "```bash",
           "python3 artifacts/preset-poker/match.py preflight /tmp/preset-poker-new-run", "```", "",
           "必须使用一个尚无牌谱的新目录，避免覆盖原始对局。", ""]
    (HERE / "完整牌谱.md").write_text("\n".join(md), encoding="utf-8")
    final_rows = [[LABELS[p], p, 200, amount(summary["stacks"][p]), f"{summary['net'][p]:+g}", receipts[p]["actions"]] for p in SEATS]
    identity_rows = [[LABELS[p], receipts[p]["session_id"], TOOL_USE[p]] for p in SEATS]
    template = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>四种真实 preset · 德扑完整回放</title>
<style>
:root{color-scheme:dark;--bg:#101b19;--panel:#182722;--ink:#ebf1e9;--muted:#a8b9ae;--line:#32463b;--gold:#e1c68c}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.75 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:1200px;margin:auto;padding:50px 28px 80px}h1{font-size:clamp(28px,4vw,46px);line-height:1.3;letter-spacing:-1px;margin:12px 0}h2{font-size:26px;margin:4px 0 12px}h3{font-size:18px;margin-top:28px}small,.muted{color:var(--muted)}small{font-size:13px;font-weight:400;margin-left:8px}.eyebrow,.section-label{color:var(--gold);font-size:12px;letter-spacing:3px}.lead{max-width:930px;color:var(--muted)}.chips{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}.pill{border:1px solid var(--line);border-radius:30px;padding:6px 15px;color:var(--gold)}nav{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}a{color:var(--gold);text-decoration:none}a:hover{text-decoration:underline}section,.panel{border:1px solid var(--line);background:var(--panel);border-radius:18px;padding:24px;margin:24px 0}section{scroll-margin-top:16px}.note{padding:14px 18px;border-left:3px solid var(--gold);background:#263128;color:#e0d9c2;margin:20px 0}.hole-grid,.players{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.hole,.player{padding:14px;background:#111e19;border:1px solid var(--line);border-radius:12px}.hole b{display:block;margin-bottom:8px}.card{display:inline-block;background:#f1efe7;border-radius:6px;min-width:37px;text-align:center;font-size:19px;line-height:1.8;font-weight:650;padding:1px 5px;box-shadow:0 2px 1px #0004}.red{color:#b9363e}.black{color:#182620}.board{margin:12px 0 20px}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px}th{text-align:left;color:var(--muted);font-weight:500;white-space:nowrap}td,th{padding:11px 10px;border-bottom:1px solid var(--line)}td:nth-child(-n+6){white-space:nowrap}tbody tr:hover{background:#ffffff04}td:last-child{min-width:60px}.settlement{padding-top:4px;margin-top:14px}.settlement h3{color:var(--gold)}pre{white-space:pre-wrap;font:13px/1.9 ui-monospace,monospace;overflow-wrap:anywhere;color:#c9d8cd}.replay{border:1px solid #697448;background:radial-gradient(ellipse at center,#28513b,#12271c);border-radius:20px;padding:24px}.replay-head{display:flex;justify-content:space-between;gap:20px;color:var(--gold)}#replay-board{text-align:center;padding:18px 0}#replay-board .card{font-size:24px;min-width:45px}.player.active{border-color:var(--gold);box-shadow:0 0 0 1px var(--gold)}.player.folded{opacity:.5}.player .stack{float:right;color:var(--gold)}.player .cards{margin-top:10px}.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:16px 0}button{font:inherit;background:#25382b;color:var(--ink);border:1px solid #587252;border-radius:7px;padding:6px 13px;cursor:pointer}button:hover{background:#36533e}input[type=range]{flex:1;min-width:180px;accent-color:var(--gold)}#replay-title{min-height:54px;color:#e4e5cd;margin-top:15px}details summary{cursor:pointer;color:var(--gold)}footer{color:var(--muted);margin-top:30px;font-size:13px}.proof td:nth-child(2){font:12px ui-monospace,monospace}@media(max-width:650px){main{padding:24px 14px}section,.panel,.replay{padding:16px}.hole-grid,.players{grid-template-columns:repeat(2,minmax(0,1fr))}.table-wrap{margin:0 -6px}td,th{padding:9px 7px}}
</style></head><body><main>
<div class="eyebrow">FOUR PRESETS / ONE POLICY / COMPLETE HAND HISTORY</div><h1>同一套策略，四位真实玩家。</h1>
<p class="lead">标准 · PTC · 极简 · 创造，分别由四个真实 preset 子 agent 启动独立客户端。共 4 手牌、63 次行动，下面保留每次下注、理由与结算。</p>
<div class="chips"><span class="pill">四个真实子会话</span><span class="pill">同一策略 SHA-256</span><span class="pill">每人 200 · 盲注 1/2</span><span class="pill">总筹码 800 守恒</span></div>
<div class="note"><b>这是透明的 Mock 演示，不是实力排名。</b>使用人工预设牌堆和同一个确定性策略程序。四位 agent 各自运行客户端，程序逐动作执行策略；不是大模型逐动作自由决策。回放公开全部底牌，玩家请求只包含自己的底牌与当前公共信息。</div>
<nav><a href="#replay">逐步回放</a><a href="#hand-1">第 1 手</a><a href="#hand-2">第 2 手</a><a href="#hand-3">第 3 手</a><a href="#hand-4">第 4 手</a><a href="#identity">真实 preset 证据</a><a href="#rules">统一策略</a></nav>
<div class="panel"><h2>最终筹码</h2>__FINAL_TABLE__<p class="muted">输赢来自牌局与位置；不能据此判定某个 preset 更强。</p></div>
<section id="replay"><div class="section-label">STEP BY STEP</div><h2>逐步回放 · 全牌旁观视角</h2><p class="muted">每一帧代表已发生的发牌、行动或结算。付款后筹码尚未包含当手分池，结算帧才入账。</p>
<div class="replay"><div class="replay-head"><span id="replay-stage">加载回放</span><span id="replay-pot"></span></div><div id="replay-board"></div><div class="players" id="replay-players"></div><div id="replay-title"></div></div>
<div class="controls"><button id="prev" aria-label="上一步">←</button><button id="play">自动播放</button><button id="next" aria-label="下一步">→</button><input type="range" id="slider" min="0" max="0" value="0" aria-label="回放进度"><span id="step"></span></div>
<noscript>此预览不执行 JavaScript；下方静态牌谱仍完整包含全部 63 次行动。可在浏览器打开本文件使用回放控件。</noscript></section>
__HAND_SECTIONS__
<section id="identity"><div class="section-label">VERIFIED RUNTIME</div><h2>确实是四种 preset，不是四个标签</h2><p>四份持久化会话头分别记录对应的 <code>agentPreset</code>、<code>origin: subagent</code> 与同一个父会话。下面工具名称来自各子 agent 的真实执行回报。</p><div class="proof">__IDENTITY_TABLE__</div><p><a href="live-match/runtime-proof.json">查看提取的会话头证据</a> · <a href="live-match/events.jsonl">原始事件 JSONL</a> · <a href="live-match/summary.json">机器可读结果</a> · <a href="完整牌谱.md">完整文字牌谱</a></p><p class="muted">临时启动配置已恢复原状；未修改四个 preset，未重启 GUI。仅提取会话元数据，不导出模型内部推理。</p></section>
<section id="rules"><div class="section-label">IDENTICAL DETERMINISTIC POLICY</div><h2>完全相同的策略</h2><pre>__RULES__</pre><details><summary>策略指纹与校验说明</summary><pre>SHA-256: __HASH__
63/63 决策与共同策略一致；四个客户端 PID 和子会话 ID 各不相同。
每手 52 张牌无重复；逐动作总筹码为 800；投入与分池守恒。
独立牌型计算器与现有引擎完成 6,000 组固定种子交叉验证。
客户端邮箱为协作隔离，不是文件系统安全隔离；无真实账号或资金操作。
Mock 复用的引擎省略烧牌；提前结束的牌局不发剩余公共牌。</pre></details></section>
<footer>¹「余码」指该玩家本次付款后的剩余筹码，不含尚未发生的分池。第 1 手累计投入 26，其中 8 为无人跟注退注，实际争夺底池 18，标准净赢 12。<br>此页面完全离线，无外部依赖；静态牌谱始终可读。__SNAP_COUNT__ 帧，含全部 63 次行动。</footer>
</main><script id="replay-data" type="application/json">__SNAPSHOTS__</script><script>
const frames=JSON.parse(document.getElementById('replay-data').textContent),names={standard:'标准',ptc:'PTC',minimal:'极简',cordis:'创造'},seats=Object.keys(names),$=id=>document.getElementById(id);
let index=0,timer=null;const cards=cs=>cs.length?cs.map(([r,s])=>`<span class="card ${s===1||s===2?'red':'black'}">${'23456789TJQKA'[r-2]}${'♠♥♦♣'[s]}</span>`).join(' '):'<span class="muted">尚未发出公共牌</span>';
function render(){const f=frames[index];$('replay-stage').textContent=`第 ${f.hand} 手 · ${f.stage}`;$('replay-pot').textContent=`底池 ${f.pot}`;$('replay-board').innerHTML=cards(f.board);$('replay-title').textContent=f.title;$('replay-players').innerHTML=seats.map(p=>`<div class="player ${f.actor===p?'active':''} ${f.folded.includes(p)?'folded':''}"><b>${names[p]}</b><span class="stack">${f.stacks[p]}</span><div class="cards">${cards(f.holes[p])}</div>${f.folded.includes(p)?'<small>已弃牌</small>':''}</div>`).join('');$('slider').value=index;$('step').textContent=`${index+1} / ${frames.length}`;}
function stop(){if(timer)clearInterval(timer);timer=null;$('play').textContent='自动播放';}
$('slider').max=frames.length-1;$('slider').oninput=()=>{stop();index=+$('slider').value;render();};$('prev').onclick=()=>{stop();index=Math.max(0,index-1);render();};$('next').onclick=()=>{stop();index=Math.min(frames.length-1,index+1);render();};$('play').onclick=()=>{if(timer){stop();return;}if(index===frames.length-1)index=0;$('play').textContent='暂停';render();timer=setInterval(()=>{index++;render();if(index===frames.length-1)stop();},1100);};render();
</script></body></html>'''
    replacements = {"__FINAL_TABLE__": table(["玩家", "真实 preset", "初始", "最终", "净变化", "行动数"], final_rows),
                    "__HAND_SECTIONS__": "\n".join(sections), "__IDENTITY_TABLE__": table(["玩家", "独立子会话 ID", "实际执行工具"], identity_rows),
                    "__RULES__": html.escape(RULES), "__HASH__": summary["strategy_sha256"],
                    "__SNAP_COUNT__": str(len(snapshots)), "__SNAPSHOTS__": json.dumps(snapshots, ensure_ascii=False).replace("<", "\\u003c")}
    for key, value in replacements.items():
        template = template.replace(key, value)
    (HERE / "完整对局回放.html").write_text(template, encoding="utf-8")
    print(f"Generated full HTML replay ({len(snapshots)} frames) and Markdown transcript ({summary['action_count']} actions).")


if __name__ == "__main__":
    main()

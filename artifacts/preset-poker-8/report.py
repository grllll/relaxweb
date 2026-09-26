"""Standalone eight-player spectator replay and every-action Markdown transcript."""
import copy
import html
import json
from match import HERE, POLICY, LABELS, SEATS, PRESETS, PRESET_FOR, STAGES, RULES, HANDS, SEED, TOTAL, pretty, load, digest

RUN = HERE / "live-match"
TOOL_USE = {"standard": "bash + job_output", "ptc": "run_code → bash / job_output", "minimal": "持久 bash", "cordis": "bash + job_output"}


def table(headers, rows):
    head = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>"+"".join(f"<td>{html.escape(str(x))}</td>" for x in row)+"</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def mdtable(headers, rows):
    return ["| "+" | ".join(map(str, headers))+" |", "|"+"---|"*len(headers)] + ["| "+" | ".join(map(str, row))+" |" for row in rows]


def card_html(cards):
    return " ".join(f'<span class="card {"red" if s in (1,2) else "black"}">{pretty([(r,s)])}</span>' for r,s in cards) or '<span class="muted">尚未发出公共牌</span>'


def action_name(e):
    a,s=e["decision"],e["state"]
    return ("下注 " if s["current_bet"]==0 else "加注到 ")+f"{a['raise_to']:g}" if a["action"]=="raise" else {"fold":"弃牌","call":"跟注","check":"过牌"}[a["action"]]


def stack_text(stacks):
    return " / ".join(f"{LABELS[p]} {stacks[p]:g}" for p in SEATS)


def main():
    events=[json.loads(x) for x in (RUN/"events.jsonl").read_text().splitlines()]
    summary,proof=load(RUN/"summary.json"),load(RUN/"runtime-proof.json")
    receipts={p:load(RUN/p/"receipt.json") for p in SEATS}
    count=summary["action_count"]
    assert sum(r["actions"] for r in receipts.values())==count
    for p in SEATS:
        h=proof[p]["physical_header"]
        assert h["agentPreset"]==PRESET_FOR[p] and h["origin"]=="subagent" and h["delegationDepth"]==1
        assert h["id"]==receipts[p]["session_id"]==summary["players"][p]["session_id"]
        assert receipts[p]["strategy_sha256"]==summary["strategy_sha256"]==digest()
    assert len({r["session_id"] for r in receipts.values()})==8
    ranked=sorted(SEATS,key=lambda p:summary["stacks"][p],reverse=True)
    finals=[[LABELS[p],PRESET_FOR[p],200,f"{summary['stacks'][p]:g}",f"{summary['net'][p]:+g}",receipts[p]["actions"]] for p in ranked]
    groups=[[preset,2,400,f"{sum(summary['stacks'][p] for p in SEATS if PRESET_FOR[p]==preset):g}",f"{sum(summary['net'][p] for p in SEATS if PRESET_FOR[p]==preset):+g}"] for preset in PRESETS]
    identities=[[LABELS[p],PRESET_FOR[p],receipts[p]["session_id"],TOOL_USE[PRESET_FOR[p]],receipts[p]["actions"]] for p in SEATS]
    disclaimer="八个真实子 agent 各自用原生工具启动玩家客户端；客户端逐动作调用同一份确定性策略。不是大模型逐动作自由决策。固定种子伪随机洗牌，无真实资金；仅八手，不能据此比较 preset 实力。"
    config=["每个 preset 恰好 2 个独立子会话：standard / ptc / minimal / cordis。创造 preset 的真实 ID 是 cordis。",
            "座位顺序："+" → ".join(LABELS[p] for p in SEATS)+"；庄家轮转一周，每人一次。",
            "初始筹码每人 200；小盲/大盲 1/2；连续 8 手，不补码，无人淘汰。",
            f"牌堆种子从 {SEED} 至 {SEED+HANDS-1}；种子在首次预检前写入，未根据结果挑选或重洗。每手 52 张无重复牌。",
            "策略源文件与上一场逐字节相同；仅本场规则说明中的人数与手数改为八。",
            "行动请求仅含本人的底牌和当时公共信息；回放为公开全部底牌的旁观视角。邮箱是协作隔离，不是文件系统安全边界。",
            "使用项目现有 HoldemRoom 引擎，Mock 发牌省略烧牌。未发出的公共牌不计入牌谱。",
            "上一场四人结果保留；临时 preset 启动配置已恢复，未修改 preset、未重启 GUI。"]
    md=["# 八人 × 四种真实 preset · 同策略德扑完整牌谱", "",f"**8 个真实子 agent，8 手牌，{count} 次行动，筹码总量 {TOTAL}。**","",disclaimer,"","## 配置","",*["- "+x for x in config],"","## 最终筹码","",*mdtable(["玩家","实际 preset","初始","最终","净变化","行动数"],finals),"","### 每个 preset 两人合计（非实力排名）","",*mdtable(["preset","人数","初始合计","最终合计","净变化"],groups),"","## 真实身份与回执","",*mdtable(["玩家","实际 preset","独立子会话 ID","实际工具","行动数"],identities),"",f"共同策略 SHA-256：`{digest()}`","","## 完全相同的策略","",RULES,""]
    sections,frames,hand_rows=[],[],[]
    for hand in range(1,HANDS+1):
        relevant=[e for e in events if e.get("hand")==hand]
        start=next(e for e in relevant if e["type"]=="hand_start")
        end=next(e for e in relevant if e["type"]=="hand_end")
        result=end["result"]
        alive=[p for p in result["hands"] if not p["folded"]]
        winners=" / ".join(LABELS[p] for p in result["payouts"])
        showdown=len(alive)>1
        title=winners+" · "+("摊牌获胜" if showdown else "对手全弃")
        intro=f"庄家 {LABELS[start['dealer']]}；小盲 {LABELS[start['small_blind']]} 1；大盲 {LABELS[start['big_blind']]} 2。"
        fragment=[f'<section id="hand-{hand}"><div class="eyebrow">HAND {hand:02}</div><h2>{title}</h2><p>{intro}</p><p class="muted">开局：{stack_text(start["stacks"])}</p>',
                  '<div class="hole-grid">'+''.join(f'<div class="hole"><b>{LABELS[p]}</b><div>{card_html(start["holes"][p])}</div></div>' for p in SEATS)+'</div>', '<h3>翻牌前 · 盲注入池后 3</h3>']
        md += [f"## 第 {hand} 手 · {title}","",intro,"","开局筹码："+stack_text(start["stacks"]),"",*mdtable(["玩家","底牌"],[[LABELS[p],pretty(start["holes"][p])] for p in SEATS]),"","### 翻牌前 · 底池 3",""]
        heads=["#","玩家","动作","本次投入","底池","付款后筹码¹","策略理由"]
        md+=mdtable(heads,[])
        stacks=dict(start["stacks"])
        stacks[start["small_blind"]]-=1
        stacks[start["big_blind"]]-=2
        snap={"hand":hand,"stage":"翻牌前","title":intro,"holes":start["holes"],"board":[],"stacks":stacks,"folded":[],"actor":None,"pot":3,"dealer":start["dealer"],"small_blind":start["small_blind"],"big_blind":start["big_blind"]}
        frames.append(copy.deepcopy(snap))
        rows=[]
        for e in relevant:
            if e["type"]=="street":
                fragment.append(table(heads,rows)); rows=[]
                board=pretty(e["board"])
                fragment.append(f'<h3>{STAGES[e["stage"]]} · 起始底池 {e["pot"]:g}</h3><div class="board">{card_html(e["board"])}</div>')
                md += ["",f"### {STAGES[e['stage']]}：{board}","",f"本街起始底池 {e['pot']:g}。","",*mdtable(heads,[])]
                snap.update(stage=STAGES[e["stage"]],board=e["board"],actor=None,title=f"发出{STAGES[e['stage']]}：{board}")
                frames.append(copy.deepcopy(snap))
            elif e["type"]=="action":
                s,a=e["state"],e["decision"]
                row=[e["seq"],LABELS[s["player"]],action_name(e),f"{e['paid']:g}",f"{e['pot_after']:g}",f"{e['stack_after_payment']:g}",a["reason"]]
                rows.append(row); md.append("| "+" | ".join(map(str,row))+" |")
                snap.update(stacks=dict(s["stacks"]),folded=list(s["folded"]),actor=s["player"],pot=e["pot_after"],title=f"#{e['seq']:03} {LABELS[s['player']]} {action_name(e)} · {a['reason']}")
                snap["stacks"][s["player"]]=e["stack_after_payment"]
                if a["action"]=="fold":snap["folded"].append(s["player"])
                frames.append(copy.deepcopy(snap))
        fragment.append(table(heads,rows))
        committed=sorted([p["committed"] for p in result["hands"]],reverse=True)
        refund=committed[0]-committed[1]
        notes=f"{winners} 收到 {result['pot']:g}；实际争夺底池 {result['pot']-refund:g}；未跟注退回 {refund:g}。"
        if not showdown:notes+=" 对手全弃，无需摊牌；展示底牌仅供旁观复核。"
        if len(snap["board"])<5:notes+=" 本手提前结束，其余公共牌未发出。"
        settlement=[]
        for p in SEATS:
            r=next(x for x in result["hands"] if x["username"]==p)
            status="弃牌" if r["folded"] else ((r["hand_name"] or "—") if showdown else "对手全弃，无需摊牌")
            settlement.append([LABELS[p],f"{r['committed']:g}",status,f"{result['payouts'].get(p,0):g}",f"{end['net'][p]:+g}",f"{end['stacks'][p]:g}"])
        sh=["玩家","本手累计投入","牌型/状态","返还及分池","本手净变化","结余"]
        md += ["","### 摊牌与结算","",notes,"",*mdtable(sh,settlement),""]
        fragment += ['<div class="settlement"><h3>摊牌与结算</h3><p>'+notes+'</p>',table(sh,settlement),'</div></section>']
        sections.append("\n".join(fragment))
        snap.update(stacks=end["stacks"],pot=0,actor=None,stage="结算",title=f"第 {hand} 手结算：{notes}")
        frames.append(copy.deepcopy(snap))
        hand_rows.append([hand,winners,pretty(snap["board"]),f"{result['pot']-refund:g}",f"{refund:g}"," / ".join(f"{LABELS[p]} {end['net'][p]:+g}" for p in result["payouts"])])
    verify=[f"{count}/{count} 动作与同一策略逐字段一致，且全部合法；每次请求、响应、回执都匹配对应子会话。",
            "8 个独立真实 subagent 会话头：四种 agentPreset 各 2 个；每人恰好一次庄家。",
            "逐动作验证总筹码 1600；每手投入等于分池；无人淘汰。",
            "独立牌型函数与引擎交叉验证 6,000 组；本地复现与真实八客户端牌谱逐事件一致。",
            "¹ 付款后筹码为发奖前余额，结算余额另列；加注到 N 指本条街累计下注目标，不是额外投入 N。"]
    md += ["## 校验与复现","",*["- "+x for x in verify],"",f"- [原始事件]({RUN/'events.jsonl'}) · [结果]({RUN/'summary.json'}) · [真实 preset 身份证据]({RUN/'runtime-proof.json'})",f"- [上一场原封不动的共享策略]({POLICY}) · [八人裁判与客户端]({HERE/'match.py'})","","本地复现不会创建新 agent，必须使用新目录：","","```bash","python3 artifacts/preset-poker-8/match.py preflight /tmp/preset-poker-eight-new-run","python3 -m unittest discover -s artifacts/preset-poker-8 -p 'test_*.py' -v","```",""]
    (HERE/"完整牌谱.md").write_text("\n".join(md),encoding="utf-8")
    payload={"frames":frames,"names":LABELS,"seats":SEATS,"actionCount":count}
    template='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>八人同策略德扑 · 完整回放</title><style>
:root{color-scheme:dark;--bg:#0b1614;--panel:#122420;--line:#2b433b;--ink:#eef2e7;--muted:#a7bbb1;--gold:#e7c878}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 80% 0,#204539,var(--bg) 55%);color:var(--ink);font-family:system-ui,-apple-system,"PingFang SC",sans-serif;line-height:1.65}main{max-width:1280px;margin:auto;padding:44px 30px}h1{font-size:clamp(30px,4vw,48px);line-height:1.2;margin:12px 0 20px}h2{font-size:26px;margin:8px 0 18px}h3{margin:26px 0 14px;font-size:19px}p{margin:10px 0}.eyebrow{color:var(--gold);font-size:12px;letter-spacing:.16em;font-weight:750}.muted,small{color:var(--muted)}.stats{display:flex;flex-wrap:wrap;gap:10px;margin:22px 0}.badge{border:1px solid var(--line);border-radius:40px;padding:7px 15px;background:#172d26}.gold{color:var(--gold)}section{border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:26px;margin:26px 0;min-width:0}nav{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0}a{color:var(--gold);text-decoration:none}nav a{padding:7px 14px;border:1px solid var(--line);border-radius:10px}.table-wrap{overflow-x:auto;width:100%;margin:15px 0;border-radius:10px;border:1px solid var(--line)}table{border-collapse:collapse;width:100%;font-size:14px;text-align:left}th,td{padding:11px 12px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:600;background:#172d26}td:last-child{white-space:normal;min-width:110px}tr:last-child td{border-bottom:0}.players,.hole-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:20px 0}.player,.hole{border:1px solid var(--line);border-radius:12px;padding:14px;background:#10201b;min-width:0}.player.active{border-color:var(--gold);box-shadow:0 0 0 1px var(--gold)}.player.folded{opacity:.48}.player-head{display:flex;justify-content:space-between;gap:6px;align-items:center}.role{color:var(--gold);font-size:11px}.stack{font-size:26px;font-weight:750;margin:5px 0}.card{display:inline-block;background:#f3f0e8;border-radius:5px;padding:5px 8px;margin:4px 2px 4px 0;font-weight:750;font-size:22px;line-height:1.4;box-shadow:0 2px 0 #0004}.card.red{color:#b73f49}.card.black{color:#14242a}.board{min-height:64px}.board .card{font-size:27px;padding:8px 12px}.replay-head{display:flex;justify-content:space-between;align-items:center;gap:12px}#replay-pot{font-size:22px;font-weight:750;color:var(--gold)}#replay-title{min-height:55px;border-left:3px solid var(--gold);padding:12px 14px;background:#193028;border-radius:0 8px 8px 0}.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:18px 0}button,select{font:inherit;background:#28473b;color:var(--ink);border:1px solid #4b6959;border-radius:8px;padding:8px 16px;cursor:pointer}button:disabled{opacity:.35;cursor:default}button:hover:not(:disabled){border-color:var(--gold)}input[type=range]{width:100%;accent-color:var(--gold)}.settlement{margin-top:25px;padding:1px 18px 10px;background:#1b332a;border-radius:12px}.rules{white-space:pre-wrap;color:var(--muted);font:inherit;font-size:14px;overflow-wrap:anywhere}.hash{overflow-wrap:anywhere;font-size:12px}footer{margin:40px 0;color:var(--muted);font-size:13px}summary{cursor:pointer;color:var(--gold)}@media(max-width:650px){main{padding:24px 14px}section{padding:16px;border-radius:14px}.players,.hole-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.player,.hole{padding:10px}.board .card{font-size:20px;padding:7px}.replay-head{align-items:flex-start}#replay-pot{font-size:19px}h2{font-size:22px}button{padding:8px 12px}}
</style></head><body><main><header><div class="eyebrow">MOCK HOLD’EM / 8 REAL SUBAGENTS / IDENTICAL POLICY</div><h1>八人同桌，一套策略。<br><span class="gold">四种 preset，各两位。</span></h1><p class="muted">__DISCLAIMER__</p><div class="stats"><span class="badge">8 个独立子会话</span><span class="badge">8 手牌 · 每人一次庄家</span><span class="badge">__ACTIONS__ 次行动 · __FRAMES__ 帧</span><span class="badge">盲注 1/2 · 筹码 1600</span></div></header>
<nav><a href="#replay">逐步回放</a><a href="#results">最终结果</a>__NAV__<a href="#proof">真实身份</a></nav>
<section id="replay"><div class="eyebrow">STEP-BY-STEP REPLAY / 旁观者全底牌视角</div><div class="replay-head"><h2 id="replay-stage"></h2><div id="replay-pot"></div></div><div id="replay-board" class="board"></div><div id="replay-players" class="players"></div><p id="replay-title"></p><div class="controls"><button id="prev">上一步</button><button id="play">自动播放</button><button id="next">下一步</button><label>跳至 <select id="hand-select" aria-label="选择手牌">__OPTIONS__</select></label><span id="step" class="muted"></span></div><input id="slider" type="range" min="0" max="__MAX__" value="0" aria-label="对局回放进度"><p class="muted">D 庄家 / SB 小盲 / BB 大盲；金框表示刚行动的玩家。余额为当前步骤值，最后一帧才加入该手结算。下方保留全部文字牌谱，禁用脚本也可阅读。</p></section>
<section id="results"><div class="eyebrow">FINAL STACKS</div><h2>八人最终结果</h2>__FINAL__<details><summary>每种 preset 两人合计 · 不代表实力排名</summary>__GROUPS__</details><h3>八手概览</h3>__OVERVIEW__<p class="muted">争夺底池不含无人跟注的退注；收到筹码 ≠ 净赢利。</p></section>
__HANDS__
<section id="proof"><div class="eyebrow">PROVENANCE &amp; REPRODUCIBILITY</div><h2>真实 preset，不只是不同昵称</h2>__IDENTITIES__<p class="muted">实际 session 头验证了八个独立 subagent 会话和各自的 agentPreset；每种恰好两位。</p><details><summary>完整设置与共享策略</summary><ul>__CONFIG__</ul><pre class="rules">__RULES__</pre><p class="hash">共享策略 SHA-256：__HASH__</p></details><ul>__CHECKS__</ul></section><footer>离线 Mock 演示 · 无真实资金 · 固定种子 __SEED__ · 所有动作及理由可复核。<br>本场胜负受发牌与座位影响；策略相同不意味着玩家结果相同。</footer></main>
<script id="replay-data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('replay-data').textContent),frames=data.frames,names=data.names,seats=data.seats;
let index=0,timer=null;const el=id=>document.getElementById(id);
const cards=cs=>cs.length?cs.map(([r,s])=>`<span class="card ${s===1||s===2?'red':'black'}">${'23456789TJQKA'[r-2]}${'♠♥♦♣'[s]}</span>`).join(' '):'<span class="muted">尚未发出公共牌</span>';
function render(){const f=frames[index];el('replay-stage').textContent=`第 ${f.hand} 手 · ${f.stage}`;el('replay-pot').textContent=`底池 ${f.pot}`;el('replay-board').innerHTML=cards(f.board);el('replay-title').textContent=f.title;el('replay-players').innerHTML=seats.map(p=>`<div class="player ${f.actor===p?'active':''} ${f.folded.includes(p)?'folded':''}"><div class="player-head"><b>${names[p]}</b><span class="role">${p===f.dealer?'D':p===f.small_blind?'SB':p===f.big_blind?'BB':''}</span></div><div class="stack">${f.stacks[p]}</div>${cards(f.holes[p]||[])}<div class="muted">${f.folded.includes(p)?'已弃牌':f.stage==='结算'?'已结算':'在局'}</div></div>`).join('');el('step').textContent=`${index+1} / ${frames.length}`;el('slider').value=index;el('prev').disabled=index===0;el('next').disabled=index===frames.length-1;el('hand-select').value=f.hand;}
function stop(){clearInterval(timer);timer=null;el('play').textContent='自动播放';}
el('prev').onclick=()=>{stop();index=Math.max(0,index-1);render();};el('next').onclick=()=>{stop();index=Math.min(frames.length-1,index+1);render();};el('slider').oninput=e=>{stop();index=+e.target.value;render();};el('hand-select').onchange=e=>{stop();index=frames.findIndex(f=>f.hand===+e.target.value);render();};el('play').onclick=()=>{if(timer){stop();return;}if(index===frames.length-1){index=0;render();}el('play').textContent='暂停';timer=setInterval(()=>{index++;render();if(index===frames.length-1)stop();},1000);};render();
</script></body></html>'''
    replacements={"__DISCLAIMER__":disclaimer,"__ACTIONS__":str(count),"__FRAMES__":str(len(frames)),"__MAX__":str(len(frames)-1),
                  "__NAV__":"".join(f'<a href="#hand-{n}">第 {n} 手</a>' for n in range(1,9)),"__OPTIONS__":"".join(f'<option value="{n}">第 {n} 手</option>' for n in range(1,9)),
                  "__FINAL__":table(["玩家","真实 preset","初始","最终","净变化","行动数"],finals),"__GROUPS__":table(["preset","人数","初始合计","最终合计","净变化"],groups),
                  "__OVERVIEW__":table(["手牌","胜者","实际公共牌","争夺底池","退注","胜者净变化"],hand_rows),"__HANDS__":"\n".join(sections),
                  "__IDENTITIES__":table(["玩家","实际 preset","独立子会话 ID","实际工具","行动数"],identities),"__CONFIG__":"".join('<li>'+html.escape(x)+'</li>' for x in config),
                  "__RULES__":html.escape(RULES),"__HASH__":digest(),"__CHECKS__":"".join('<li>'+html.escape(x)+'</li>' for x in verify),"__SEED__":str(SEED),
                  "__DATA__":json.dumps(payload,ensure_ascii=False).replace('<','\\u003c')}
    for key,value in replacements.items():template=template.replace(key,value)
    (HERE/"完整对局回放.html").write_text(template,encoding="utf-8")
    print(f"Generated {HANDS} hands, {count} actions, {len(frames)} replay frames, eight verified identities.")


if __name__=="__main__":main()

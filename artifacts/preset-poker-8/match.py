"""Eight actual agent clients, unchanged policy, seeded Mock Hold'em (no real accounts).
Previous four-player artifacts remain untouched. No web server is started.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
POLICY = HERE.parent / "preset-poker/policy.py"
sys.path.insert(0, str(ROOT))
sys.path.append(str(POLICY.parent))
from games import holdem
from policy import VERSION, RULES as ORIGINAL_RULES, best, decide

PRESETS = ["standard", "ptc", "minimal", "cordis"]
SEATS = [f"{preset}-{n}" for n in (1, 2) for preset in PRESETS]
PRESET_FOR = {p: p.rsplit("-", 1)[0] for p in SEATS}
LABELS = {p: dict(zip(PRESETS, ["标准", "PTC", "极简", "创造"]))[PRESET_FOR[p]] + " " + p[-1] for p in SEATS}
STAGES = {"preflop": "翻牌前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
HANDS, INITIAL, TOTAL, SEED = 8, 200, 1600, 2026092108
RULES = ORIGINAL_RULES.replace("四人共享", "八人共享").replace("连续打 4 手", "连续打 8 手")


def digest():
    return hashlib.sha256(POLICY.read_bytes()).hexdigest()


def pretty(cs):
    return " ".join("23456789TJQKA"[r-2] + "♠♥♦♣"[s] for r, s in cs) or "—"


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def deck_for(hand):
    # Seed committed before preflight; no outcome-based reshuffle/seed selection.
    deck = [(r, s) for r in range(2, 15) for s in range(4)]
    random.Random(SEED + hand).shuffle(deck)
    assert len(set(deck)) == len(deck) == 52
    return deck


async def noop(*args, **kwargs):
    pass


def new_room():
    room = holdem.HoldemRoom(room_id="preset-poker-eight-mock", name="八人同策略 Mock", owner=SEATS[0], buy_in=INITIAL, blind=1)
    room.broadcast_payload = room.broadcast_views = room.on_rooms_changed = noop
    room.schedule = room.cancel_timer = lambda *args, **kwargs: None
    for player in SEATS:
        room.add_member(player, INITIAL)
    return room


def state_for(room, seq):
    g = room.game
    name = g["to_act"]
    return {"seq": seq, "hand": g["hand_no"], "player": name, "stage": g["stage"],
            "hole": g["holes"][name], "board": list(g["board"]), "dealer": g["dealer"],
            "pot": sum(g["committed"].values()), "current_bet": g["current_bet"],
            "to_call": round(g["current_bet"]-g["street_committed"][name], 2),
            "street_committed": dict(g["street_committed"]),
            "stacks": {p: room.members[p]["stack"] for p in SEATS},
            "folded": sorted(g["folded"]), "allin": sorted(g["allin"]), "legal": room.legal_actions(name),
            "strategy_sha256": digest()}


def check_legal(state, action):
    kind, opts = action["action"], state["legal"]
    assert kind in {"check", "call", "fold", "raise"}
    if kind == "raise":
        target = action["raise_to"]
        assert opts["can_raise"] and 0 < target <= opts["raise_max"]
        assert target >= opts["raise_min"] or target == opts["raise_max"]
    else:
        assert opts[kind], f"Illegal {kind}"
    assert action == decide(state), "Client deviated from unchanged shared strategy"


async def play(get_action, emit):
    room = new_room()
    seq = 0
    for hand in range(HANDS):
        before = {p: room.members[p]["stack"] for p in SEATS}
        with patch.object(holdem, "new_deck", lambda: deck_for(hand)):
            await (room.start() if hand == 0 else room.start_hand())
        g = room.game
        assert g is not None, "Insufficient players to continue"
        emit({"type": "hand_start", "hand": hand+1, "dealer": g["dealer"],
              "small_blind": g["order"][0], "big_blind": g["order"][1],
              "holes": g["holes"], "stacks": before, "seed": SEED+hand,
              "deck_sha256": hashlib.sha256(json.dumps(deck_for(hand)).encode()).hexdigest()})
        last_stage = "preflop"
        while room.game["stage"] != "showdown":
            g = room.game
            if g["stage"] != last_stage:
                last_stage = g["stage"]
                emit({"type": "street", "hand": hand+1, "stage": last_stage,
                      "board": list(g["board"]), "pot": sum(g["committed"].values())})
            seq += 1
            assert seq < 2000, "Action loop did not converge"
            state = json.loads(json.dumps(state_for(room, seq)))
            assert "holes" not in state and "deck" not in state
            action = await get_action(state)
            check_legal(state, action)
            player, old = state["player"], g["committed"][state["player"]]
            await room.perform_action(player, action["action"], action)
            assert g["last_action"] and g["last_action"]["username"] == player
            committed = next(x["committed"] for x in g["result"]["hands"] if x["username"] == player) if g["result"] else g["committed"][player]
            paid = round(committed-old, 2)
            emit({"type": "action", "hand": hand+1, "seq": seq, "state": state, "decision": action,
                  "paid": paid, "pot_after": round(state["pot"]+paid, 2),
                  "stack_after_payment": round(state["stacks"][player]-paid, 2)})
            assert round(sum(room.members[p]["stack"] for p in SEATS)+sum(g["committed"].values()), 2) == TOTAL
        result = room.game["result"]
        for row in result["hands"]:
            if not row["folded"] and len(room.game["board"]) == 5:
                hole = [(c["r"], c["s"]) for c in row["cards"]]
                score = best(hole+room.game["board"])
                assert score == holdem.best7(hole+room.game["board"])
                row["score"] = list(score)
        assert sum(p["committed"] for p in result["hands"]) == sum(result["payouts"].values()) == result["pot"]
        emit({"type": "hand_end", "hand": hand+1, "result": result,
              "stacks": {p: room.members[p]["stack"] for p in SEATS},
              "net": {p: room.members[p]["stack"]-before[p] for p in SEATS}})
    final = {p: room.members[p]["stack"] for p in SEATS}
    room.close()
    return {"stacks": final, "net": {p: final[p]-INITIAL for p in SEATS}, "action_count": seq,
            "hand_count": HANDS, "seed": SEED, "seats": SEATS, "presets": PRESET_FOR}


async def wait_file(path, seconds=900):
    end = time.monotonic()+seconds
    while not path.exists():
        if time.monotonic() >= end:
            raise TimeoutError(f"Timed out waiting for {path}")
        await asyncio.sleep(.05)
    return load(path)


async def dealer(run_dir, local=False):
    run_dir.mkdir(parents=True, exist_ok=True)
    assert not (run_dir/"events.jsonl").exists(), "Use a fresh directory; do not overwrite a previous match"
    ready = {}
    def emit(event):
        with (run_dir/"events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False)+"\n")
        if event["type"] == "hand_start":
            print(f"\n第 {event['hand']} 手，庄家 {LABELS[event['dealer']]}", flush=True)
        elif event["type"] == "street":
            print(f"{STAGES[event['stage']]}：{pretty(event['board'])}，底池 {event['pot']:g}", flush=True)
        elif event["type"] == "action":
            s,a=event["state"],event["decision"]
            print(f"#{event['seq']:03} {LABELS[s['player']]} {a['action']} {a.get('raise_to','')}；投入 {event['paid']:g}，池 {event['pot_after']:g}；{a['reason']}", flush=True)
        elif event["type"] == "hand_end":
            print(f"结算：{event['result']['payouts']}；剩余 {event['stacks']}", flush=True)
    try:
        if not local:
            print("Waiting for eight real-preset agent clients…", flush=True)
            entries=await asyncio.gather(*(wait_file(run_dir/p/"ready.json") for p in SEATS))
            ready=dict(zip(SEATS,entries))
            assert len({e["pid"] for e in entries}) == len({e["session_id"] for e in entries}) == 8
            for p,e in ready.items():
                assert e["player"] == p and e["strategy_sha256"] == digest()
                assert e["preset"] == PRESET_FOR[p]
            print("All eight unique agent sessions and client processes are ready.", flush=True)
        emit({"type": "match_start", "mode": "local-preflight" if local else "eight-real-agent-clients",
              "strategy_version": VERSION, "strategy_sha256": digest(), "rules": RULES, "players": ready,
              "initial_stack": INITIAL, "total_chips": TOTAL, "small_blind": 1, "big_blind": 2,
              "seed": SEED, "presets": PRESET_FOR,
              "disclaimer": "固定种子伪随机洗牌的 Mock 演示；策略程序自动执行，非逐动作大模型自由决策；八手不足以比较 preset 实力。"})
        async def action(state):
            if local:
                return decide(state)
            box, n = run_dir/state["player"], f"{state['seq']:04d}"
            atomic(box/f"request-{n}.json", state)
            answer=await wait_file(box/f"response-{n}.json", seconds=90)
            assert answer["seq"] == state["seq"] and answer["player"] == state["player"]
            assert answer["session_id"] == ready[state["player"]]["session_id"]
            assert answer["strategy_sha256"] == digest()
            return answer["decision"]
        summary=await play(action, emit)
        summary.update({"mode": "local-preflight" if local else "eight-real-agent-clients", "players": ready,
                        "strategy_sha256": digest(), "checks": ["52张无重复牌", "合法动作", "逐动作策略一致", "1600筹码守恒", "投入与分池守恒", "独立牌型校验"]})
        emit({"type": "match_end", **summary})
        atomic(run_dir/"summary.json", summary)
        for p in SEATS:
            atomic(run_dir/p/"done.json", {"ok": True, **summary})
        print("\nFINAL "+json.dumps(summary, ensure_ascii=False), flush=True)
    except BaseException as exc:
        for p in SEATS:
            atomic(run_dir/p/"done.json", {"ok": False, "error": str(exc)})
        raise


def client(run_dir, player):
    box=run_dir/player
    box.mkdir(parents=True, exist_ok=True)
    identity={"player": player, "preset": PRESET_FOR[player], "pid": os.getpid(),
              "session_id": os.environ.get("DSH_SESSION_ID"), "strategy_version": VERSION, "strategy_sha256": digest()}
    assert identity["session_id"], "Must be launched by an actual DSH agent"
    assert not (box/"ready.json").exists(), "Duplicate player client"
    atomic(box/"ready.json", identity)
    print("READY "+json.dumps(identity, ensure_ascii=False), flush=True)
    seen=set()
    end=time.monotonic()+1200
    while time.monotonic()<end:
        for request in sorted(box.glob("request-*.json")):
            if request.name in seen:
                continue
            state=load(request)
            assert state["player"] == player and state["strategy_sha256"] == digest()
            answer=decide(state)
            atomic(box/request.name.replace("request-", "response-"),
                   {"player": player, "seq": state["seq"], "decision": answer, "strategy_sha256": digest(), "session_id": identity["session_id"]})
            seen.add(request.name)
            print(f"HAND {state['hand']} / #{state['seq']} / {state['stage']} / hole={pretty(state['hole'])} board={pretty(state['board'])} / "+json.dumps(answer,ensure_ascii=False), flush=True)
        if (box/"done.json").exists():
            result=load(box/"done.json")
            assert result["ok"], result.get("error")
            receipt={**identity, "actions": len(seen), "final_stack": result["stacks"][player], "net": result["net"][player], "ok": True}
            atomic(box/"receipt.json", receipt)
            print("RECEIPT "+json.dumps(receipt, ensure_ascii=False), flush=True)
            return
        time.sleep(.05)
    raise TimeoutError("Dealer did not finish")


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("mode", choices=["preflight", "dealer", "client"])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("player", nargs="?", choices=SEATS)
    args=parser.parse_args()
    if args.mode == "client":
        assert args.player
        client(args.run_dir.resolve(),args.player)
    else:
        asyncio.run(dealer(args.run_dir.resolve(),local=args.mode=="preflight"))

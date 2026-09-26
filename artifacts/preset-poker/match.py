"""Four real agent-launched clients, one deterministic policy, the real HoldemRoom engine.
No web server, real account, database, network, or real-money side effects.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from games import holdem
from policy import VERSION, RULES, best, decide

SEATS = ["standard", "ptc", "minimal", "cordis"]
LABELS = dict(zip(SEATS, ["标准", "PTC", "极简", "创造"]))
STAGES = {"preflop": "翻牌前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
FIXTURES = [
    {"holes": ["As Kd", "Qh Qc", "9s 8s", "7d 2c"], "board": "Kh 9d 4s 2h Jc"},
    {"holes": ["Qs Jd", "Ah Kh", "8c 8d", "Ac Td"], "board": "Kc 8h 2h 4s 3h"},
    {"holes": ["Ad Qc", "Kd Ks", "7h 7s", "Ac Js"], "board": "Qd 7c 2s 4h 9d"},
    {"holes": ["9h 9c", "Ad Kd", "Qs Js", "5h 5c"], "board": "As 5d 2c Td 8s"},
]


def digest():
    return hashlib.sha256((HERE / "policy.py").read_bytes()).hexdigest()


def cards(text):
    return [("23456789TJQKA".index(token[0]) + 2, "shdc".index(token[1])) for token in text.split()]


def pretty(cs):
    return " ".join("23456789TJQKA"[r - 2] + "♠♥♦♣"[s] for r, s in cs) or "—"


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def deck_for(hand):
    fixture = FIXTURES[hand]
    holes = dict(zip(SEATS, map(cards, fixture["holes"])))
    order = SEATS[hand + 1:] + SEATS[:hand + 1]
    sequence = [card for name in order for card in holes[name]] + cards(fixture["board"])
    assert len(set(sequence)) == 13, "duplicate scripted card"
    rest = [(rank, suit) for rank in range(2, 15) for suit in range(4) if (rank, suit) not in sequence]
    result = list(reversed(sequence + rest))
    assert len(result) == len(set(result)) == 52
    return result


async def noop(*args, **kwargs):
    pass


def new_room():
    room = holdem.HoldemRoom(room_id="preset-poker-mock", name="四 preset 同策略演示", owner=SEATS[0], buy_in=200, blind=1)
    room.broadcast_payload = noop
    room.broadcast_views = noop
    room.on_rooms_changed = noop
    room.schedule = lambda *args, **kwargs: None
    room.cancel_timer = lambda *args, **kwargs: None
    for name in SEATS:
        room.add_member(name, 200)
    return room


def state_for(room, seq):
    g = room.game
    name = g["to_act"]
    return {
        "seq": seq, "hand": g["hand_no"], "player": name, "stage": g["stage"],
        "hole": g["holes"][name], "board": list(g["board"]), "dealer": g["dealer"],
        "pot": sum(g["committed"].values()), "current_bet": g["current_bet"],
        "to_call": round(g["current_bet"] - g["street_committed"][name], 2),
        "street_committed": dict(g["street_committed"]),
        "stacks": {p: room.members[p]["stack"] for p in SEATS},
        "folded": sorted(g["folded"]), "legal": room.legal_actions(name),
        "strategy_sha256": digest(),
    }


def check_legal(state, action):
    kind = action["action"]
    opts = state["legal"]
    assert kind in {"check", "call", "fold", "raise"}
    if kind in {"check", "call", "fold"}:
        assert opts[kind], f"illegal {kind}"
    else:
        target = action["raise_to"]
        assert opts["can_raise"] and 0 < target <= opts["raise_max"]
        assert target >= opts["raise_min"] or target == opts["raise_max"]
    assert action == decide(state), "client deviated from shared policy"


async def play(get_action, emit):
    room = new_room()
    seq = 0
    for hand in range(4):
        before_stacks = {p: room.members[p]["stack"] for p in SEATS}
        with patch.object(holdem, "new_deck", lambda: deck_for(hand)):
            await (room.start() if hand == 0 else room.start_hand())
        g = room.game
        emit({"type": "hand_start", "hand": hand + 1, "dealer": g["dealer"],
              "small_blind": g["order"][0], "big_blind": g["order"][1],
              "holes": g["holes"], "stacks": before_stacks, "deck_sha256": hashlib.sha256(
                  json.dumps(deck_for(hand)).encode()).hexdigest()})
        last_stage = "preflop"
        while room.game["stage"] != "showdown":
            g = room.game
            if g["stage"] != last_stage:
                last_stage = g["stage"]
                emit({"type": "street", "hand": hand + 1, "stage": last_stage,
                      "board": list(g["board"]), "pot": sum(g["committed"].values())})
            seq += 1
            assert seq < 300, "action loop did not converge"
            state = state_for(room, seq)
            # Serialization also proves that the client gets no opponent holes or future board.
            state = json.loads(json.dumps(state))
            assert "holes" not in state and "deck" not in state
            action = await get_action(state)
            check_legal(state, action)
            player = state["player"]
            old_committed = g["committed"][player]
            await room.perform_action(player, action["action"], action)
            assert g["last_action"] and g["last_action"]["username"] == player
            if g["result"]:
                committed = next(p["committed"] for p in g["result"]["hands"] if p["username"] == player)
            else:
                committed = g["committed"][player]
            paid = round(committed - old_committed, 2)
            post_stack = round(state["stacks"][player] - paid, 2)
            emit({"type": "action", "hand": hand + 1, "seq": seq, "state": state,
                  "decision": action, "paid": paid, "pot_after": round(state["pot"] + paid, 2),
                  "stack_after_payment": post_stack})
            chips = sum(room.members[p]["stack"] for p in SEATS) + sum(g["committed"].values())
            assert round(chips, 2) == 800, f"chip conservation violated: {chips}"
        result = room.game["result"]
        for row in result["hands"]:
            if not row["folded"] and len(room.game["board"]) == 5:
                hole = [(c["r"], c["s"]) for c in row["cards"]]
                score = best(hole + room.game["board"])
                assert score == holdem.best7(hole + room.game["board"])
                row["score"] = list(score)
        assert sum(p["committed"] for p in result["hands"]) == result["pot"]
        assert sum(result["payouts"].values()) == result["pot"]
        emit({"type": "hand_end", "hand": hand + 1, "result": result,
              "stacks": {p: room.members[p]["stack"] for p in SEATS},
              "net": {p: room.members[p]["stack"] - before_stacks[p] for p in SEATS}})
    final = {p: room.members[p]["stack"] for p in SEATS}
    room.close()
    return {"stacks": final, "net": {p: final[p] - 200 for p in SEATS}, "action_count": seq}


async def wait_file(path, seconds=300):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for {path.name}")
        await asyncio.sleep(0.05)
    return load(path)


async def dealer(run_dir, local=False):
    run_dir.mkdir(parents=True, exist_ok=True)
    assert not (run_dir / "events.jsonl").exists(), "Use a fresh run directory; never overwrite a completed match"
    events = []
    ready = {}

    def emit(event):
        events.append(event)
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if event["type"] == "hand_start":
            print(f"\n第 {event['hand']} 手，庄家 {LABELS[event['dealer']]}", flush=True)
        elif event["type"] == "street":
            print(f"{STAGES[event['stage']]}：{pretty(event['board'])}，底池 {event['pot']:g}", flush=True)
        elif event["type"] == "action":
            s, a = event["state"], event["decision"]
            amount = f" 到 {a['raise_to']:g}" if "raise_to" in a else ""
            print(f"#{event['seq']:02} {LABELS[s['player']]} {a['action']}{amount}；"
                  f"投入 {event['paid']:g}，池 {event['pot_after']:g}；{a['reason']}", flush=True)
        elif event["type"] == "hand_end":
            print(f"结算：{event['result']['payouts']}；剩余 {event['stacks']}", flush=True)

    try:
        if not local:
            print("Waiting for four real-preset agent clients…", flush=True)
            entries = await asyncio.gather(*(wait_file(run_dir / p / "ready.json") for p in SEATS))
            ready = dict(zip(SEATS, entries))
            assert len({e["pid"] for e in entries}) == 4
            assert len({e["session_id"] for e in entries}) == 4
            for p, entry in ready.items():
                assert entry["player"] == p and entry["strategy_sha256"] == digest()
            print("All four separate agent sessions and client processes are ready.", flush=True)
        emit({"type": "match_start", "mode": "local-preflight" if local else "four-real-agent-clients",
              "strategy_version": VERSION, "strategy_sha256": digest(), "rules": RULES,
              "players": ready, "initial_stack": 200, "small_blind": 1, "big_blind": 2,
              "disclaimer": "人工预设牌堆的功能演示，不是随机实力测评；策略程序自动执行，非逐动作大模型自由决策。"})

        async def action(state):
            if local:
                return decide(state)
            box = run_dir / state["player"]
            n = f"{state['seq']:04d}"
            atomic(box / f"request-{n}.json", state)
            response = await wait_file(box / f"response-{n}.json", seconds=60)
            assert response["seq"] == state["seq"] and response["player"] == state["player"]
            assert response["strategy_sha256"] == digest()
            return response["decision"]

        summary = await play(action, emit)
        summary.update({"mode": "local-preflight" if local else "four-real-agent-clients", "players": ready,
                        "strategy_sha256": digest(), "checks": ["52 张无重复牌", "正确行动顺序与合法动作",
                        "逐动作策略一致", "逐动作总筹码 800", "投入与分池守恒", "两套牌型计算一致"]})
        emit({"type": "match_end", **summary})
        atomic(run_dir / "summary.json", summary)
        for p in SEATS:
            atomic(run_dir / p / "done.json", {"ok": True, **summary})
        print("\nFINAL " + json.dumps(summary, ensure_ascii=False), flush=True)
    except BaseException as exc:
        for p in SEATS:
            atomic(run_dir / p / "done.json", {"ok": False, "error": str(exc)})
        raise


def client(run_dir, player):
    box = run_dir / player
    box.mkdir(parents=True, exist_ok=True)
    identity = {"player": player, "pid": os.getpid(), "session_id": os.environ.get("DSH_SESSION_ID"),
                "strategy_version": VERSION, "strategy_sha256": digest()}
    assert identity["session_id"], "Client must be launched by an actual DSH agent"
    assert not (box / "ready.json").exists(), "Duplicate player client"
    atomic(box / "ready.json", identity)
    print("READY " + json.dumps(identity, ensure_ascii=False), flush=True)
    seen = set()
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        for request in sorted(box.glob("request-*.json")):
            if request.name in seen:
                continue
            state = load(request)
            assert state["player"] == player and state["strategy_sha256"] == digest()
            answer = decide(state)
            response = {"player": player, "seq": state["seq"], "decision": answer,
                        "strategy_sha256": digest(), "session_id": identity["session_id"]}
            atomic(box / request.name.replace("request-", "response-"), response)
            seen.add(request.name)
            print(f"HAND {state['hand']} / #{state['seq']} / {state['stage']} / "
                  f"hole={pretty(state['hole'])} board={pretty(state['board'])} / "
                  + json.dumps(answer, ensure_ascii=False), flush=True)
        if (box / "done.json").exists():
            result = load(box / "done.json")
            assert result["ok"], result.get("error")
            receipt = {**identity, "actions": len(seen), "final_stack": result["stacks"][player],
                       "net": result["net"][player], "ok": True}
            atomic(box / "receipt.json", receipt)
            print("RECEIPT " + json.dumps(receipt, ensure_ascii=False), flush=True)
            return
        time.sleep(0.05)
    raise TimeoutError("dealer did not finish")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["dealer", "client", "preflight"])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("player", nargs="?", choices=SEATS)
    args = parser.parse_args()
    if args.mode == "client":
        assert args.player
        client(args.run_dir.resolve(), args.player)
    else:
        asyncio.run(dealer(args.run_dir.resolve(), local=args.mode == "preflight"))


if __name__ == "__main__":
    main()

"""Offline audit of actual eight-agent journal, shared policy and replay output."""
import asyncio
from collections import Counter
import hashlib
import json
import random
import re
import unittest
from match import HERE, SEATS, PRESETS, PRESET_FOR, HANDS, TOTAL, SEED, digest, load, decide, best, check_legal, deck_for, play
from games.holdem import best7

RUN=HERE/"live-match"


class EightPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events=[json.loads(x) for x in (RUN/"events.jsonl").read_text().splitlines()]
        cls.actions=[e for e in cls.events if e["type"]=="action"]
        cls.summary=load(RUN/"summary.json")
        cls.starts={e["hand"]:e for e in cls.events if e["type"]=="hand_start"}
        cls.ends=[e for e in cls.events if e["type"]=="hand_end"]

    def test_eight_real_headers_exactly_two_per_preset(self):
        proof=load(RUN/"runtime-proof.json")
        headers=[proof[p]["physical_header"] for p in SEATS]
        self.assertEqual(Counter(h["agentPreset"] for h in headers),dict.fromkeys(PRESETS,2))
        self.assertEqual(len({h["id"] for h in headers}),8)
        self.assertEqual(len({h["parentSession"] for h in headers}),1)
        self.assertEqual(len({p["pid"] for p in self.summary["players"].values()}),8)
        for p,h in zip(SEATS,headers):
            self.assertEqual(h["agentPreset"],PRESET_FOR[p])
            self.assertEqual(h["id"],self.summary["players"][p]["session_id"])
            self.assertEqual(h["origin"],"subagent")
            self.assertEqual(h["delegationDepth"],1)

    def test_each_player_deals_once_with_no_eliminations(self):
        self.assertEqual([e["dealer"] for e in self.starts.values()],SEATS)
        self.assertEqual(len(self.ends),HANDS)
        for e in self.starts.values():
            self.assertEqual(set(e["holes"]),set(SEATS))
            self.assertTrue(all(v>0 for v in e["stacks"].values()))

    def test_every_action_matches_its_private_mailbox_and_policy(self):
        self.assertEqual(len(self.actions),122)
        self.assertEqual([e["seq"] for e in self.actions],list(range(1,123)))
        for e in self.actions:
            s=e["state"]; p=s["player"]; n=f"{e['seq']:04d}"
            self.assertEqual(load(RUN/p/f"request-{n}.json"),s)
            r=load(RUN/p/f"response-{n}.json")
            self.assertEqual(r["decision"],e["decision"])
            self.assertEqual(r["decision"],decide(s))
            self.assertEqual(r["seq"],e["seq"])
            self.assertEqual(r["player"],p)
            self.assertEqual(r["session_id"],self.summary["players"][p]["session_id"])
            self.assertEqual(r["strategy_sha256"],digest())
            check_legal(s,e["decision"])

    def test_receipts_and_unchanged_policy_fingerprint(self):
        self.assertEqual(digest(),"4b45820bae8c5571f3ac5c8080e096e682c253df0ae8aefb45587ee1e18492d3")
        self.assertEqual(load(HERE.parent/"preset-poker/live-match/summary.json")["strategy_sha256"],digest())
        counts=Counter(e["state"]["player"] for e in self.actions)
        self.assertEqual(counts,dict(zip(SEATS,[15,11,14,21,15,15,19,12])))
        for p in SEATS:
            r=load(RUN/p/"receipt.json")
            self.assertTrue(r["ok"])
            self.assertEqual(r["actions"],counts[p])
            self.assertEqual(r["final_stack"],self.summary["stacks"][p])
            self.assertEqual(r["net"],self.summary["net"][p])
            self.assertEqual(r["session_id"],self.summary["players"][p]["session_id"])
            self.assertEqual(r["strategy_sha256"],digest())

    def test_private_state_excludes_other_holes_and_future_board(self):
        for e in self.actions:
            s=e["state"]
            self.assertEqual(s["hole"],self.starts[e["hand"]]["holes"][s["player"]])
            self.assertNotIn("holes",s)
            self.assertNotIn("deck",s)
            self.assertEqual(len(s["board"]),{"preflop":0,"flop":3,"turn":4,"river":5}[s["stage"]])

    def test_chip_conservation_and_all_settlements(self):
        for e in self.actions:
            s=e["state"]
            self.assertEqual(sum(s["stacks"].values())+s["pot"],TOTAL)
            self.assertEqual(sum(s["stacks"].values())-e["paid"]+e["pot_after"],TOTAL)
        for e in self.ends:
            r=e["result"]
            self.assertEqual(sum(e["stacks"].values()),TOTAL)
            self.assertEqual(sum(x["committed"] for x in r["hands"]),r["pot"])
            self.assertEqual(sum(r["payouts"].values()),r["pot"])
            self.assertEqual(sum(e["net"].values()),0)

    def test_eight_seeded_decks_and_actual_deal_order(self):
        for hand,start in self.starts.items():
            deck=deck_for(hand-1)
            self.assertEqual(len(deck),52)
            self.assertEqual(len(set(deck)),52)
            self.assertEqual(start["seed"],SEED+hand-1)
            self.assertEqual(start["deck_sha256"],hashlib.sha256(json.dumps(deck).encode()).hexdigest())
            order=SEATS[hand:]+SEATS[:hand]
            for p in order:
                self.assertEqual(start["holes"][p],[list(deck.pop()),list(deck.pop())])
            board=self.ends[hand-1]["result"]["board"]
            self.assertEqual(board,[dict(zip(("r","s"),deck.pop())) for _ in board])

    def test_6000_independent_rank_evaluations(self):
        rng=random.Random(20260921)
        deck=[(r,s) for r in range(2,15) for s in range(4)]
        for n in (5,6,7):
            for _ in range(2000):
                cs=rng.sample(deck,n)
                self.assertEqual(best(cs),best7(cs))

    def test_local_reproduction_matches_every_event_not_only_results(self):
        reproduced=[]
        async def action(state):return decide(state)
        result=asyncio.run(play(action,reproduced.append))
        for k,v in result.items():self.assertEqual(v,self.summary[k])
        self.assertEqual(json.loads(json.dumps(reproduced)),[e for e in self.events if e["type"] not in {"match_start","match_end"}])
        self.assertEqual(result["stacks"],dict(zip(SEATS,[439,195,205,108,113,69,268,203])))

    def test_policy_rejects_divergent_action(self):
        with self.assertRaises(AssertionError):check_legal(self.actions[0]["state"],{"action":"fold","reason":"deviation"})

    def test_replay_frames_cover_all_events_without_future_streets(self):
        text=(HERE/"完整对局回放.html").read_text()
        match=re.search(r'<script id="replay-data" type="application/json">(.*?)</script>',text,re.S)
        payload=json.loads(match.group(1))
        frames=payload["frames"]
        self.assertEqual(len(frames),155)
        self.assertEqual(payload["actionCount"],len(self.actions))
        self.assertEqual(payload["seats"],SEATS)
        self.assertEqual(frames[-1]["stacks"],self.summary["stacks"])
        for f in frames:self.assertEqual(sum(f["stacks"].values())+f["pot"],TOTAL)
        for hand in range(1,9):
            group=[f for f in frames if f["hand"]==hand]
            self.assertEqual(group[0]["board"],[])
            self.assertEqual(group[-1]["board"],[[c["r"],c["s"]] for c in self.ends[hand-1]["result"]["board"]])
        self.assertTrue(all(f["board"]==[] for f in frames if f["hand"]==4))
        self.assertNotRegex(text,r"__[A-Z_]+__")


if __name__=="__main__":unittest.main(verbosity=2)

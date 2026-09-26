"""Offline regression and forensic checks for this demonstration, with no new agents."""
import asyncio
from collections import Counter
import json
from pathlib import Path
import random
import unittest

from match import HERE, SEATS, check_legal, deck_for, digest, load, play
from games.holdem import best7
from policy import best, decide


class PokerDemoTests(unittest.TestCase):
    def setUp(self):
        self.events = [json.loads(line) for line in (HERE / "live-match/events.jsonl").read_text().splitlines()]
        self.actions = [e for e in self.events if e["type"] == "action"]

    def test_all_four_real_presets_have_distinct_child_sessions(self):
        proof = load(HERE / "live-match/runtime-proof.json")
        headers = [proof[p]["physical_header"] for p in SEATS]
        self.assertEqual(len({h["id"] for h in headers}), 4)
        self.assertEqual(len({h["parentSession"] for h in headers}), 1)
        for preset, header in zip(SEATS, headers):
            self.assertEqual(header["agentPreset"], preset)
            self.assertEqual(header["origin"], "subagent")
            self.assertEqual(header["delegationDepth"], 1)

    def test_every_real_action_has_a_matching_client_response(self):
        self.assertEqual(len(self.actions), 63)
        for event in self.actions:
            state, action = event["state"], event["decision"]
            player = state["player"]
            number = f"{event['seq']:04d}"
            request = load(HERE / "live-match" / player / f"request-{number}.json")
            response = load(HERE / "live-match" / player / f"response-{number}.json")
            self.assertEqual(request, state)
            self.assertEqual(response["decision"], action)
            self.assertEqual(action, decide(state))
            self.assertEqual(response["strategy_sha256"], digest())
            check_legal(state, action)

    def test_private_state_has_no_other_holes_or_future_board(self):
        lengths = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
        starts = {e["hand"]: e for e in self.events if e["type"] == "hand_start"}
        for event in self.actions:
            state = event["state"]
            self.assertEqual(state["hole"], starts[event["hand"]]["holes"][state["player"]])
            self.assertNotIn("holes", state)
            self.assertNotIn("deck", state)
            self.assertEqual(len(state["board"]), lengths[state["stage"]])

    def test_identical_policy_fingerprints_and_receipt_totals(self):
        counts = Counter(e["state"]["player"] for e in self.actions)
        for player in SEATS:
            receipt = load(HERE / "live-match" / player / "receipt.json")
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["actions"], counts[player])
            self.assertEqual(receipt["strategy_sha256"], digest())
        self.assertEqual(counts, dict(zip(SEATS, [16, 21, 15, 11])))

    def test_every_action_conserves_chips_and_every_payout_balances(self):
        for event in self.actions:
            state = event["state"]
            self.assertEqual(sum(state["stacks"].values()) + state["pot"], 800)
            self.assertEqual(sum(state["stacks"].values()) - event["paid"] + event["pot_after"], 800)
        ends = [e for e in self.events if e["type"] == "hand_end"]
        self.assertEqual(len(ends), 4)
        for event in ends:
            self.assertEqual(sum(event["stacks"].values()), 800)
            result = event["result"]
            self.assertEqual(sum(p["committed"] for p in result["hands"]), sum(result["payouts"].values()))
            self.assertEqual(sum(event["net"].values()), 0)

    def test_four_fixed_decks_contain_52_unique_cards(self):
        for hand in range(4):
            self.assertEqual(len(deck_for(hand)), 52)
            self.assertEqual(len(set(deck_for(hand))), 52)

    def test_seeded_6000_hand_ranks_match_existing_engine(self):
        rng = random.Random(20260920)
        deck = [(rank, suit) for rank in range(2, 15) for suit in range(4)]
        for count in (5, 6, 7):
            for _ in range(2000):
                hand = rng.sample(deck, count)
                self.assertEqual(best(hand), best7(hand))

    def test_full_local_replay_exactly_matches_real_clients(self):
        replay_events = []
        async def action(state):
            return decide(state)
        replay = asyncio.run(play(action, replay_events.append))
        actual = load(HERE / "live-match/summary.json")
        self.assertEqual(replay["stacks"], actual["stacks"])
        self.assertEqual(replay["action_count"], 63)
        self.assertEqual(replay["stacks"], dict(zip(SEATS, [98, 80, 320, 302])))
        reproduced = [e for e in replay_events if e["type"] == "action"]
        self.assertEqual(reproduced, self.actions)

    def test_deviating_action_is_rejected(self):
        state = self.actions[1]["state"]
        with self.assertRaises(AssertionError):
            check_legal(state, {"action": "fold", "reason": "deviation"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

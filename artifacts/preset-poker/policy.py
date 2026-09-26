"""Shared, deterministic poker policy v1; no opponent cards, RNG or preset branching."""
from collections import Counter
from itertools import combinations

VERSION = "same-policy-v1"
RULES = """四人共享 same-policy-v1，禁止诈唬、读对手底牌或修改策略。
初始筹码每人 200，盲注 1/2，连续打 4 手，庄家按座位轮转。
翻牌前：QQ+ 或 AK，在当前下注低于 6 时加注到 6；否则最多跟注差额 12。
其余对子、任意 Ax、两张均至少 T 的牌、低张至少 5 的同花连张，最多跟注差额 6。
其余牌免费则过牌，否则弃牌。
翻牌/转牌/河牌基础下注 S 分别为 8/16/24。
两对及以上：无人下注时下注 S；面对下注且当前总下注低于 2S 时，合法则加注到 2S；否则最多跟注差额 2S。
顶对/超对（底牌参与该对子）：无人下注时下注 S，否则最多跟注差额 S。
其他底牌参与的一对：免费过牌，否则最多跟注差额 S/2。
翻牌或转牌有四张同花或一张补成顺子的听牌：免费过牌，否则最多跟注差额 S/2。
其余免费过牌，否则弃牌。下注受合法最小加注和筹码上限约束；筹码不足以跟注时以全下跟注表示。
规则中的阈值均指本次需补的差额，不是整条街累计投入。无随机决策，无针对 preset 的分支。"""


def score5(cards):
    ranks = sorted((c[0] for c in cards), reverse=True)
    counts = Counter(ranks)
    unique = sorted(counts, reverse=True)
    flush = len({c[1] for c in cards}) == 1
    straight = 0
    if len(unique) == 5:
        if unique[0] - unique[-1] == 4:
            straight = unique[0]
        elif unique == [14, 5, 4, 3, 2]:
            straight = 5
    groups = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    shape = [n for _, n in groups]
    ties = [r for r, _ in groups]
    if flush and straight:
        return (8, straight)
    if shape == [4, 1]:
        return (7, *ties)
    if shape == [3, 2]:
        return (6, *ties)
    if flush:
        return (5, *ranks)
    if straight:
        return (4, straight)
    if shape == [3, 1, 1]:
        return (3, *ties)
    if shape == [2, 2, 1]:
        return (2, *ties)
    if shape == [2, 1, 1, 1]:
        return (1, *ties)
    return (0, *ranks)


def best(cards):
    return max(score5(c) for c in combinations(cards, 5))


def draw(cards):
    flush_draw = max(Counter(c[1] for c in cards).values()) == 4
    ranks = {c[0] for c in cards}
    if 14 in ranks:
        ranks.add(1)
    straight_draw = any(len(ranks & set(range(lo, lo + 5))) == 4 for lo in range(1, 11))
    return flush_draw or straight_draw


def decide(state):
    hole, board = state["hole"], state["board"]
    options = state["legal"]
    owed = state["to_call"]
    bet = state["current_bet"]

    def passive(limit, reason):
        if owed <= 0:
            return {"action": "check", "reason": reason + "；免费过牌"}
        if owed <= limit:
            if options["call"]:
                return {"action": "call", "reason": reason + f"；补 {owed:g} ≤ 阈值 {limit:g}"}
            return {"action": "raise", "raise_to": options["raise_max"],
                    "reason": reason + "；筹码不足，全下跟注"}
        return {"action": "fold", "reason": reason + f"；需补 {owed:g} > 阈值 {limit:g}"}

    def aggressive(target, fallback, reason):
        target = max(target, options["raise_min"])
        if options["can_raise"] and target > bet and options["raise_max"] > bet:
            return {"action": "raise", "raise_to": min(target, options["raise_max"]),
                    "reason": reason}
        return passive(fallback, reason + "；不能加注")

    if state["stage"] == "preflop":
        ranks = sorted(c[0] for c in hole)
        pair = ranks[0] == ranks[1]
        premium = (pair and ranks[0] >= 12) or ranks == [13, 14]
        playable = (pair or ranks[1] == 14 or ranks[0] >= 10
                    or (hole[0][1] == hole[1][1] and ranks[1] - ranks[0] == 1 and ranks[0] >= 5))
        if premium:
            if bet < 6:
                return aggressive(6, 12, "翻前强牌 QQ+/AK，加注到 6")
            return passive(12, "翻前强牌 QQ+/AK")
        return passive(6 if playable else 0, "翻前入池范围" if playable else "翻前范围外")

    size = {"flop": 8, "turn": 16, "river": 24}[state["stage"]]
    score = best(hole + board)
    if score[0] >= 2:
        if bet < 2 * size:
            return aggressive(size if bet == 0 else 2 * size, 2 * size, "两对及以上，价值下注/加注")
        return passive(2 * size, "两对及以上")
    own_pair = score[0] == 1 and any(c[0] == score[1] for c in hole)
    top_pair = own_pair and score[1] >= max(c[0] for c in board)
    if top_pair:
        if bet == 0:
            return aggressive(size, size, "顶对/超对，价值下注")
        return passive(size, "顶对/超对")
    if own_pair:
        return passive(size / 2, "其他底牌参与的一对")
    if state["stage"] != "river" and draw(hole + board):
        return passive(size / 2, "同花/顺子听牌")
    return passive(0, "无足够成牌或听牌")

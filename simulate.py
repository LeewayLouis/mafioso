"""Mafioso 机器人对局模拟

用法：
    python simulate.py              # 跑 5000 局，打印统计
    python simulate.py 20000        # 跑 20000 局
    python simulate.py --show 42    # 用上帝视角，逐条打印第 42 号种子的那一局

机器人只会"随机地做合法的事"，不会推理和欺骗，
所以胜率只反映规则结构上的倾向，不代表真人对局。
它最大的用处是：把规则跑上几千遍，找出报错、死局和明显失衡的地方。
"""
from __future__ import annotations

import random
import sys
from collections import Counter

from engine import (
    IllegalAction, close_dig_stage, close_main_stage, close_shoot_stage, close_vote_stage,
    stage_players, start_day, start_night, submit, _undug,
)
from models import (
    Action, ActionKind as K, DayStage, GameState, NightStage, Phase, Role, Team, new_game,
)

# ---------------------------------------------------------------
# 机器人的性格参数（可以调，看看对胜率有什么影响）
# ---------------------------------------------------------------
P_DIG = 0.5          # 掘墓人每晚（有尸体时）去挖的概率
P_ITEM = 0.15        # 好人每晚使用一件道具的概率
P_SHOOT = 0.25       # 猎人每天开枪的概率
P_ABSTAIN = 0.15     # 好人投票时弃权的概率
WOLVES_VOTE_TOGETHER = True   # 狼白天是否统一投同一个好人


# ---------------------------------------------------------------
# 机器人：夜晚
# ---------------------------------------------------------------
def _alive(game: GameState) -> list[int]:
    return [p.pid for p in game.alive_players()]


def _alive_good(game: GameState) -> list[int]:
    return [p.pid for p in game.alive_players() if p.team == Team.GOOD]


def bot_dig_stage(game: GameState, rng: random.Random) -> None:
    for pid in stage_players(game):
        acts = []
        p = game.players[pid]
        if rng.random() < P_DIG:
            acts.append(Action(pid, K.DIG, rng.choice(_undug(game))))
            if p.has_antidote and rng.random() < 0.3:
                acts.append(Action(pid, K.USE_ANTIDOTE))   # 保证挖掘不受毒影响
        submit(game, pid, acts)


def bot_good_night(game: GameState, pid: int, rng: random.Random) -> list[Action]:
    p, n, night = game.players[pid], game.round, game.night
    acts: list[Action] = []
    already_used = pid in night.decoy or pid in night.antidote
    if not already_used and rng.random() < P_ITEM:
        items = [k for k, ok in ((K.USE_DECOY, p.has_decoy), (K.USE_ANTIDOTE, p.has_antidote)) if ok]
        if items:
            acts.append(Action(pid, rng.choice(items)))
    others = [q for q in _alive(game) if q != pid]
    if p.role == Role.GUARD and p.injured_on != n - 1:
        acts.append(Action(pid, K.PROTECT, rng.choice(_alive(game))))
    elif p.role == Role.DETECTIVE and others:
        acts.append(Action(pid, K.TRACK, rng.choice(others)))
    elif p.role == Role.WITCH:
        acts.append(Action(pid, K.CHECK, rng.choice(_alive(game))))
    return acts


def bot_wolf_plan(game: GameState, rng: random.Random) -> dict[int, list[Action]]:
    """狼群一起讨论，整体决定今晚做什么。"""
    plan: dict[int, list[Action]] = {}
    wolves = [p for p in game.alive_players() if p.team == Team.WOLF]
    goods = _alive_good(game)
    head = next((w for w in wolves if w.role == Role.WOLF_HEAD), None)
    tails = [w for w in wolves if w.role == Role.WOLF_TAIL]

    biter = head
    if game.vacant_head is not None and tails:       # 狼首空缺：选一个狼尾接任
        biter = rng.choice(tails)
        tails = [t for t in tails if t is not biter]
        plan[biter.pid] = [Action(biter.pid, K.DIG, game.vacant_head)]

    bite_target = rng.choice(goods) if goods and biter else None
    if biter and bite_target is not None:
        plan.setdefault(biter.pid, []).append(Action(biter.pid, K.BITE, bite_target))

    pool = [g for g in goods if g != bite_target]
    rng.shuffle(pool)
    for t in tails:
        plan[t.pid] = [Action(t.pid, K.POISON, pool.pop())] if pool else []
    return plan


def bot_night(game: GameState, rng: random.Random) -> list:
    events = []
    start_night(game)
    if game.night.stage == NightStage.DIG:
        bot_dig_stage(game, rng)
        events += close_dig_stage(game)
    wolf_plan = bot_wolf_plan(game, rng)
    for pid in sorted(stage_players(game)):
        if game.players[pid].team == Team.GOOD:
            submit(game, pid, bot_good_night(game, pid, rng))
        else:
            submit(game, pid, wolf_plan.get(pid, []))
    events += close_main_stage(game)
    return events


# ---------------------------------------------------------------
# 机器人：白天
# ---------------------------------------------------------------
def bot_day(game: GameState, rng: random.Random) -> list:
    events = []
    start_day(game)
    for pid in stage_players(game):                   # 有子弹的猎人
        others = [q for q in _alive(game) if q != pid]
        if others and rng.random() < P_SHOOT:
            submit(game, pid, [Action(pid, K.SHOOT, rng.choice(others))])
        else:
            submit(game, pid, [])
    events += close_shoot_stage(game)
    if game.phase == Phase.ENDED:
        return events

    goods = _alive_good(game)
    wolf_target = rng.choice(goods) if goods else None
    for pid in stage_players(game):
        p = game.players[pid]
        if p.team == Team.WOLF:
            target = wolf_target if WOLVES_VOTE_TOGETHER else rng.choice(goods)
        elif rng.random() < P_ABSTAIN:
            target = None
        else:
            target = rng.choice([q for q in _alive(game) if q != pid])
        submit(game, pid, [Action(pid, K.VOTE, target)])
    events += close_vote_stage(game)
    return events


# ---------------------------------------------------------------
# 跑一局
# ---------------------------------------------------------------
def play_game(seed: int) -> GameState:
    game = new_game(seed=seed)
    rng = random.Random(seed * 7919 + 1)              # 机器人自己的随机数，和引擎分开
    for _ in range(100):                              # 防止死循环
        if game.phase == Phase.ENDED:
            return game
        if game.phase == Phase.NIGHT:
            bot_night(game, rng)
        else:
            bot_day(game, rng)
    raise RuntimeError(f"种子 {seed}：100 个阶段后游戏还没结束，可能是死循环")


# ---------------------------------------------------------------
# 统计
# ---------------------------------------------------------------
def run(n_games: int) -> None:
    winners: Counter = Counter()
    reasons: Counter = Counter()
    end_day: Counter = Counter()
    errors: list[tuple[int, str]] = []

    for seed in range(n_games):
        try:
            g = play_game(seed)
        except (IllegalAction, RuntimeError, Exception) as e:   # 任何报错都记下种子
            errors.append((seed, f"{type(e).__name__}: {e}"))
            continue
        winners[g.winner] += 1
        reasons[g.win_reason] += 1
        end_day[g.round] += 1

    done = n_games - len(errors)
    print(f"\n共 {n_games} 局，正常结束 {done} 局，报错 {len(errors)} 局\n")
    if done:
        print("胜率")
        for team, name in ((Team.GOOD, "好人"), (Team.WOLF, "狼人")):
            print(f"  {name}  {winners[team] / done:6.1%}  ({winners[team]} 局)")
        print("\n结局")
        for reason, c in reasons.most_common():
            print(f"  {c / done:6.1%}  {reason}")
        avg = sum(d * c for d, c in end_day.items()) / done
        print(f"\n平均在第 {avg:.1f} 天结束")
        print("  " + "  ".join(f"第{d}天:{end_day[d] / done:.0%}" for d in sorted(end_day)))
    if errors:
        print("\n报错的局（用 --show 种子号 重现）")
        for seed, msg in errors[:10]:
            print(f"  种子 {seed}: {msg}")


# ---------------------------------------------------------------
# 上帝视角回放一局
# ---------------------------------------------------------------
def show(seed: int) -> None:
    g = new_game(seed=seed)
    name = {p.pid: f"{p.pid}号({p.role.value})" for p in g.players}
    print(f"种子 {seed} 的身份：" + "  ".join(name[p.pid] for p in g.players))
    g = play_game(seed)

    def who(pid):
        return name[pid]

    last_round, last_phase = None, None
    for e in g.log:
        if (e.round, e.phase) != (last_round, last_phase):
            label = "夜" if e.phase == Phase.NIGHT else "天"
            print(f"\n—— 第 {e.round} {label} ——")
            last_round, last_phase = e.round, e.phase
        d = e.detail
        if e.kind == "dig_result":
            print(f"  [私] {who(e.to)} 挖开了 {who(d['corpse'])} 的坟墓，变成了{d['new_role']}")
        elif e.kind == "head_succeeded" and e.to == d["pid"]:
            print(f"  [狼] {who(d['pid'])} 接任狼首")
        elif e.kind == "bite_result" and e is _first(g.log, e):
            print(f"  [狼] 撕咬 {who(d['target'])}：{'成功' if d['success'] else '失败'}")
        elif e.kind == "track_result":
            print(f"  [私] 侦探追踪 {who(d['target'])}：叶子{'掉了' if d['leaf_fell'] else '还在'}")
        elif e.kind == "check_result":
            print(f"  [私] 女巫检测 {who(d['target'])}：{'中毒' if d['poisoned'] else '没中毒'}")
        elif e.kind == "dawn":
            deaths = "、".join(who(x) for x in d["deaths"]) or "无人"
            print(f"  天亮了，昨夜死亡：{deaths}")
        elif e.kind == "shot":
            if d.get("suicide"):
                print(f"  {who(d['shooter'])} 开枪，但他中了毒，枪口转向了自己")
            else:
                print(f"  {who(d['shooter'])} 向 {who(d['target'])} 开枪，流出{d['blood']}色的血")
        elif e.kind == "vote_result":
            counts = "，".join(f"{who(k)} {v}票" for k, v in sorted(d["counts"].items()))
            out = who(d["sacrificed"]) if d["sacrificed"] is not None else "无人（平票或弃权）"
            print(f"  投票：{counts or '全员弃权'}；被献祭：{out}")
        elif e.kind == "game_over":
            print(f"\n  游戏结束：{'好人' if d['winner'] == 'GOOD' else '狼人'}获胜 —— {d['reason']}")
    print(f"  累计尸体：{g.death_count}")


def _first(log, ev):
    """同一条狼群消息会发给每头狼，回放时只打印一次。"""
    for e in log:
        if e.kind == ev.kind and e.round == ev.round and e.detail == ev.detail:
            return e
    return ev


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--show"]:
        show(int(args[1]) if len(args) > 1 else 0)
    else:
        run(int(args[0]) if args else 5000)

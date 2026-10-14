"""Mafioso 规则引擎：规则逻辑（对应规则文档 v3）

目前完成：夜晚、白天（猎人开枪、匿名投票献祭、第十天上限）

夜晚的进行方式：每个人被关进自己的小营帐，各自提交
    start_night(game)
    ── 掘墓阶段（墓地里有没挖开的尸体时才有，约 10 秒）──
    submit(game, 掘墓人座位, [挖掘, 可选的道具])
    close_dig_stage(game)               # 掘墓立刻结算；挖到狼就加入狼群
    ── 主阶段（约 1 分钟）：所有人同时行动 ──
    submit(game, 座位号, [行动...])      # 好人在各自营帐；狼在公共房间一起讨论
    close_main_stage(game)              # 统一结算，天亮

提交时立刻检查合不合法，不合法会报错，玩家可以马上改；
同一阶段内可以重新提交，覆盖之前的选择；没提交的人视为什么都不做。
写测试和机器人对局时，可以用 resolve_night 一次性交完整夜的行动。

白天的进行方式（讨论的 5 分钟由服务器计时，引擎不管）：
    start_day(game)
    ── 开枪阶段：讨论结束后、投票之前 ──
    submit(game, 猎人座位, [Action(猎人, SHOOT, 目标)])   # 不开枪就交空列表
    close_shoot_stage(game)             # 开枪立刻公开结算
    ── 投票阶段：所有活着的人匿名投票 ──
    submit(game, 座位号, [Action(座位号, VOTE, 目标)])     # 目标为 None 或不交表示弃权
    close_vote_stage(game)              # 公布每人得票数，献祭，进入下一夜
同样可以用 resolve_day 一次性交完整个白天的行动。
"""
from __future__ import annotations

import copy
from collections import defaultdict
from typing import Optional

from models import (
    Action, ActionKind as K, DayStage, DayState, DeathCause, Event, GameState, MAX_DAYS,
    NightStage, NightState, Phase, Player, Role, SACRIFICE_TARGET, Team,
)


class IllegalAction(Exception):
    """玩家提交了一个规则不允许的行动。"""


ITEM_KINDS = {K.USE_DECOY, K.USE_ANTIDOTE}
ROLE_KINDS = {                          # 主阶段里各个好人角色能做的事（道具人人都能用）
    Role.GUARD: {K.PROTECT},
    Role.DETECTIVE: {K.TRACK},
    Role.WITCH: {K.CHECK},
}
GOOD_KINDS = ITEM_KINDS | {K.PROTECT, K.TRACK, K.CHECK}
WOLF_KINDS = {K.BITE, K.POISON, K.DIG}


# ---------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------
def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise IllegalAction(msg)


def _emit(game: GameState, events: list[Event], kind: str,
          to: Optional[int] = None, **detail) -> None:
    """记一条事件。to=None 表示公开，否则只发给 to 这个座位。"""
    ev = Event(round=game.round, phase=game.phase, kind=kind,
               public=(to is None), to=to, detail=detail)
    events.append(ev)
    game.log.append(ev)


def _emit_to_wolves(game: GameState, events: list[Event], kind: str, **detail) -> None:
    for p in game.alive_players():
        if p.team == Team.WOLF:
            _emit(game, events, kind, to=p.pid, **detail)


def _end(game: GameState, events: list[Event], winner: Team, reason: str) -> None:
    game.winner = winner
    game.win_reason = reason
    _emit(game, events, "game_over", winner=winner.name, reason=reason)  # 记在当前的夜/天里
    game.phase = Phase.ENDED
    game.night = None
    game.day = None


def kill(game: GameState, pid: int, cause: DeathCause) -> None:
    """让一个玩家死亡并下葬。不论怎么死、什么身份，都计入尸体数。"""
    p = game.players[pid]
    if not p.alive:
        return
    p.alive = False
    game.death_count += 1
    p.death_order = game.death_count
    p.death_cause = cause
    game.graveyard.append(pid)


def _undug(game: GameState) -> list[int]:
    return [pid for pid in game.graveyard if pid not in game.dug]


def _inherit(game: GameState, digger: Player, corpse: Player) -> None:
    """digger 被 corpse 的灵魂入侵，继承它的角色和消耗后的状态。
    掘墓人掘墓和狼尾接任，用的是同一个机制。"""
    was_good = digger.team == Team.GOOD
    digger.role = corpse.role
    digger.has_bullet = corpse.has_bullet      # 猎人没开过枪，继承后得到那一枪
    digger.injured_on = corpse.injured_on
    if was_good and digger.team == Team.WOLF:
        digger.has_decoy = False               # 道具是好人专属，转入狼阵营后失去
        digger.has_antidote = False
    game.dug.add(corpse.pid)
    if game.vacant_head == corpse.pid:
        game.vacant_head = None


def _apply_items(game: GameState, acts: list[Action]) -> None:
    """替身、解药生效。解药会解除昨夜的毒，并让今晚的毒无效。"""
    n, night = game.round, game.night
    for a in acts:
        p = game.players[a.actor]
        if a.kind == K.USE_DECOY:
            p.has_decoy, p.decoy_used_on = False, n
            night.decoy.add(p.pid)
        elif a.kind == K.USE_ANTIDOTE:
            p.has_antidote, p.antidote_used_on = False, n
            night.antidote.add(p.pid)
            if p.poisoned_on == n - 1:
                p.poisoned_on = None


def _still_poisoned(game: GameState, p: Player) -> bool:
    """昨夜中的毒，今晚准备阶段还在生效吗。"""
    return p.poisoned_on == game.round - 1


# ---------------------------------------------------------------
# 夜晚：开始、谁要行动、谁还没交
# ---------------------------------------------------------------
def start_night(game: GameState) -> None:
    _check(game.phase == Phase.NIGHT, "现在不是夜晚")
    _check(game.night is None, "这一夜已经开始了")
    # 掘墓人还在场（活着、还没挖过）并且墓地里有尸体，才有掘墓阶段，所有人最多等 10 秒。
    # 掘墓人死了或者已经挖过，这 10 秒就消失了，这是有意保留的信息，作为一个博弈点。
    gravedigger_present = any(p.role == Role.GRAVEDIGGER for p in game.alive_players())
    stage = NightStage.DIG if (gravedigger_present and _undug(game)) else NightStage.MAIN
    game.night = NightState(stage=stage)


def stage_players(game: GameState) -> set[int]:
    """当前阶段需要行动的玩家。"""
    alive = game.alive_players()
    if game.phase == Phase.DAY:
        _check(game.day is not None, "白天还没开始")
        if game.day.stage == DayStage.SHOOT:
            return {p.pid for p in alive if p.role == Role.HUNTER and p.has_bullet}
        return {p.pid for p in alive}
    _check(game.night is not None, "夜晚还没开始")
    if game.night.stage == NightStage.DIG:
        return {p.pid for p in alive if p.role == Role.GRAVEDIGGER}
    return {p.pid for p in alive}


def waiting_for(game: GameState) -> set[int]:
    """当前阶段还没提交的玩家（只给服务器用，不要显示给玩家看）。"""
    if game.phase == Phase.DAY:
        day = game.day
        subs = day.shots if day.stage == DayStage.SHOOT else day.votes
    else:
        night = game.night
        subs = night.dig_subs if night.stage == NightStage.DIG else night.subs
    return stage_players(game) - set(subs)


# ---------------------------------------------------------------
# 夜晚：提交
# ---------------------------------------------------------------
def submit(game: GameState, pid: int, actions: list[Action]) -> None:
    """一个玩家提交自己这一阶段的全部行动（可以是空列表，表示什么都不做）。
    检查不通过会报错，而且不会改动游戏状态。"""
    if game.phase == Phase.DAY:
        _submit_day(game, pid, actions)
        return
    night = game.night
    _check(game.phase == Phase.NIGHT and night is not None, "夜晚还没开始")
    _check(pid in stage_players(game), f"座位 {pid} 现在不能行动")
    _check(all(a.actor == pid for a in actions), "只能提交自己的行动")
    kinds = [a.kind for a in actions]
    _check(len(set(kinds)) == len(kinds), "同一种行动只能提交一次")
    for a in actions:
        if a.target is not None:
            _check(0 <= a.target < len(game.players), f"目标座位 {a.target} 不存在")

    p = game.players[pid]
    if night.stage == NightStage.DIG:
        _validate_dig(game, p, actions)
        night.dig_subs[pid] = list(actions)
    elif p.team == Team.GOOD:
        _validate_good(game, p, actions)
        night.subs[pid] = list(actions)
    else:
        _validate_wolf(game, p, actions)
        night.subs[pid] = list(actions)


def _validate_items(game: GameState, p: Player, actions: list[Action]) -> None:
    night = game.night
    used_tonight = (p.pid in night.decoy) or (p.pid in night.antidote)
    items = [a for a in actions if a.kind in ITEM_KINDS]
    if not game.rules.items_same_night:
        _check(len(items) <= 1 and not (items and used_tonight),
               "同一晚不能同时用替身和解药")
    for a in items:
        if a.kind == K.USE_DECOY:
            _check(p.has_decoy, "替身已经用过了")
        else:
            _check(p.has_antidote, "解药已经用过了")


def _validate_dig(game: GameState, p: Player, actions: list[Action]) -> None:
    _validate_items(game, p, actions)
    for a in actions:
        _check(a.kind == K.DIG or a.kind in ITEM_KINDS, "掘墓阶段只能挖掘或使用道具")
        if a.kind == K.DIG:
            _check(a.target in _undug(game), "只能挖墓地里还没被挖开的尸体")


def _validate_good(game: GameState, p: Player, actions: list[Action]) -> None:
    n, P = game.round, game.players
    allowed = ITEM_KINDS | ROLE_KINDS.get(p.role, set())
    _validate_items(game, p, actions)
    for a in actions:
        if a.kind == K.DIG:
            _check(False, "掘墓只能在掘墓阶段进行")
        _check(a.kind in allowed, f"{p.role.value}不能做 {a.kind.name}")
        if a.kind == K.PROTECT:
            _check(p.injured_on != n - 1, "守卫受了伤，今晚必须留在营帐里休养")
            _check(a.target is not None and P[a.target].alive, "保护目标必须活着")
        elif a.kind == K.TRACK:
            _check(a.target is not None and P[a.target].alive and a.target != p.pid,
                   "只能追踪另一个活着的人")
        elif a.kind == K.CHECK:
            _check(a.target is not None and P[a.target].alive, "只能检测活着的人")


def _validate_wolf(game: GameState, p: Player, actions: list[Action]) -> None:
    """狼群在公共房间一起讨论，所以会和其他狼已经提交的选择对照检查。"""
    P = game.players
    kinds = {a.kind for a in actions}
    others = [a for q, acts in game.night.subs.items()
              if q != p.pid and P[q].team == Team.WOLF for a in acts]

    allowed = {K.BITE} if p.role == Role.WOLF_HEAD else {K.POISON, K.DIG, K.BITE}
    for a in actions:
        _check(a.kind in allowed, f"{p.role.value}不能做 {a.kind.name}")
        if a.kind in (K.BITE, K.POISON):
            _check(a.target is not None and P[a.target].alive
                   and P[a.target].team == Team.GOOD, "只能对活着的好人下手")

    if K.DIG in kinds:
        dig = next(a for a in actions if a.kind == K.DIG)
        _check(game.vacant_head is not None and dig.target == game.vacant_head,
               "现在没有等待接任的狼首尸体")
        _check(K.POISON not in kinds, "接任狼首后只能撕咬，不能下毒")
        _check(not any(o.kind == K.DIG for o in others), "已经有别的狼尾接任了")
    if K.BITE in kinds and p.role == Role.WOLF_TAIL:
        _check(K.DIG in kinds, "狼尾只有接任狼首后才能撕咬")

    bites = [a.target for a in actions + others if a.kind == K.BITE]
    poisons = [a.target for a in actions + others if a.kind == K.POISON]
    _check(len(bites) <= 1, "今晚只能撕咬一个人")
    _check(len(set(poisons)) == len(poisons), "两个狼尾不能毒同一个人")
    _check(not (set(bites) & set(poisons)), "下毒目标不能和撕咬目标相同")


# ---------------------------------------------------------------
# 夜晚：掘墓阶段结束 → 掘墓立刻结算
# ---------------------------------------------------------------
def close_dig_stage(game: GameState) -> list[Event]:
    night = game.night
    _check(night is not None and night.stage == NightStage.DIG, "现在不是掘墓阶段")
    events: list[Event] = []
    acts = [a for lst in night.dig_subs.values() for a in lst]
    _apply_items(game, acts)

    for a in acts:
        if a.kind == K.DIG:
            digger = game.players[a.actor]
            # 中毒时认不清死者顺序，随机挖到一具（今晚吃了解药就没事）
            target = (game.rng.choice(_undug(game)) if _still_poisoned(game, digger)
                      else a.target)
            _inherit(game, digger, game.players[target])
            night.left.add(digger.pid)
            _emit(game, events, "dig_result", to=digger.pid, corpse=target,
                  new_role=digger.role.value, has_bullet=digger.has_bullet)
            if digger.team == Team.WOLF:
                _emit_to_wolves(game, events, "wolf_joined", pid=digger.pid,
                                role=digger.role.value)

    night.stage = NightStage.MAIN
    return events


# ---------------------------------------------------------------
# 夜晚：主阶段结束 → 统一结算，天亮
# ---------------------------------------------------------------
def close_main_stage(game: GameState) -> list[Event]:
    night = game.night
    _check(night is not None and night.stage == NightStage.MAIN, "现在不是主阶段")
    n, P = game.round, game.players
    events: list[Event] = []
    good_acts = [a for pid, lst in night.subs.items() if P[pid].team == Team.GOOD for a in lst]
    wolf_acts = [a for pid, lst in night.subs.items() if P[pid].team == Team.WOLF for a in lst]

    # 1. 道具，然后确定谁仍在中毒
    _apply_items(game, good_acts)
    prep_poisoned = {p.pid for p in game.alive_players() if _still_poisoned(game, p)}
    died: list[int] = []

    # 守卫今晚去了哪里
    guard: Optional[Player] = None
    guard_target: Optional[int] = None
    for a in good_acts:
        if a.kind == K.PROTECT:
            guard, guard_target = P[a.actor], a.target
            if a.target != a.actor:
                night.left.add(a.actor)

    # 1.5 可选规则：守卫闯进了狼的营帐
    if (game.rules.guard_encounter and guard is not None and guard_target != guard.pid
            and P[guard_target].team == Team.WOLF):
        wolf = P[guard_target]
        frightened: Optional[Player] = None
        if guard.pid in prep_poisoned:
            if wolf.role == Role.WOLF_HEAD:
                kill(game, guard.pid, DeathCause.BITTEN)   # 中毒的守卫撞上狼首：守卫死亡
                died.append(guard.pid)
                frightened = wolf                          # 狼首今晚也无法撕咬
            else:
                guard.injured_on = n                       # 狼尾趁机溜走，守卫受伤
        else:
            guard.injured_on = n                           # 两败俱伤：守卫明晚不能用技能
            frightened = wolf                              # 狼被吓到，今晚缺席狼族会议
        if guard.alive:
            _emit(game, events, "guard_encounter", to=guard.pid, target=wolf.pid)
        if frightened is not None:
            _emit_to_wolves(game, events, "wolf_frightened", pid=frightened.pid)
            wolf_acts = [a for a in wolf_acts if a.actor != frightened.pid]
        guard_target = None                                # 守卫没有保护到任何人

    # 2. 狼尾接任
    for a in wolf_acts:
        if a.kind == K.DIG:
            _inherit(game, P[a.actor], P[a.target])
            _emit_to_wolves(game, events, "head_succeeded", pid=a.actor)
    if game.vacant_head is not None:
        _end(game, events, Team.GOOD, "狼首被献祭后无人接任，祭坛得到紫血")
        return events
    if game.round > MAX_DAYS:
        _end(game, events, Team.WOLF, f"第 {MAX_DAYS} 天结束仍未见紫血，大祭司复活")
        return events
    if game.death_count >= SACRIFICE_TARGET:
        _end(game, events, Team.WOLF, f"献祭满 {SACRIFICE_TARGET} 具尸体，大祭司复活")
        return events

    # 3. 狼都去了公共房间，离开了营帐（被吓跑的狼也跑出了营帐）
    for p in game.alive_players():
        if p.team == Team.WOLF:
            night.left.add(p.pid)

    # 4. 撕咬
    bite_target = next((a.target for a in wolf_acts if a.kind == K.BITE), None)
    if bite_target is not None:
        if guard is not None and guard.alive and guard_target == bite_target:
            # 守卫去挡刀：不管被咬的人有没有用替身，守卫都会受伤
            if guard.pid in night.decoy:
                pass                           # 守卫自己用了替身
            elif guard.pid in prep_poisoned:
                kill(game, guard.pid, DeathCause.BITTEN)   # 中毒虚弱，挡刀时死亡
                died.append(guard.pid)
            else:
                guard.injured_on = n
        elif bite_target in night.decoy:
            pass                               # 替身挡下
        else:
            kill(game, bite_target, DeathCause.BITTEN)
            died.append(bite_target)
        _emit_to_wolves(game, events, "bite_result", target=bite_target,
                        success=bite_target in died)

    # 5. 下毒（今晚吃了解药的人免疫）
    for a in wolf_acts:
        if a.kind == K.POISON and a.target not in night.antidote and P[a.target].alive:
            P[a.target].poisoned_on = n

    # 6. 天亮：私下告知侦探、女巫结果，公布死亡
    game.left_tent[n] = night.left
    for a in good_acts:
        if a.kind == K.TRACK:
            fell = a.target in night.left or a.actor in prep_poisoned
            _emit(game, events, "track_result", to=a.actor, target=a.target, leaf_fell=fell)
        elif a.kind == K.CHECK:
            flip = a.actor in prep_poisoned                  # 女巫中毒：结论全部相反
            really = P[a.target].poisoned_on == n
            detail = {"poisoned": really != flip}
            if game.rules.witch_sees_wolf:
                detail["is_wolf"] = (P[a.target].team == Team.WOLF) != flip
            _emit(game, events, "check_result", to=a.actor, target=a.target, **detail)
    _emit(game, events, "dawn", deaths=died)

    if game.death_count >= SACRIFICE_TARGET:
        _end(game, events, Team.WOLF, f"献祭满 {SACRIFICE_TARGET} 具尸体，大祭司复活")
        return events
    game.night = None
    game.phase = Phase.DAY
    return events


# ---------------------------------------------------------------
# 白天
# ---------------------------------------------------------------
def start_day(game: GameState) -> None:
    _check(game.phase == Phase.DAY, "现在不是白天")
    _check(game.day is None, "这个白天已经开始了")
    game.day = DayState()


def _submit_day(game: GameState, pid: int, actions: list[Action]) -> None:
    day, P = game.day, game.players
    _check(day is not None, "白天还没开始")
    _check(pid in stage_players(game), f"座位 {pid} 现在不能行动")
    _check(all(a.actor == pid for a in actions), "只能提交自己的行动")
    _check(len(actions) <= 1, "这个阶段只能提交一个行动")
    a = actions[0] if actions else None

    if day.stage == DayStage.SHOOT:
        if a is not None:
            _check(a.kind == K.SHOOT, "现在只能开枪")
            _check(a.target is not None and 0 <= a.target < len(P) and P[a.target].alive
                   and a.target != pid, "只能向另一个活着的人开枪")
        day.shots[pid] = a.target if a else None
    else:
        if a is not None:
            _check(a.kind == K.VOTE, "现在只能投票")
            if a.target is not None:
                _check(0 <= a.target < len(P) and P[a.target].alive, "只能投给活着的人")
        day.votes[pid] = a.target if a else None


def close_shoot_stage(game: GameState) -> list[Event]:
    """开枪阶段结束：开枪是公开的，一开枪全场都知道他是猎人。"""
    day = game.day
    _check(game.phase == Phase.DAY and day is not None and day.stage == DayStage.SHOOT,
           "现在不是开枪阶段")
    P = game.players
    events: list[Event] = []

    for shooter_pid, target in day.shots.items():
        if target is None:
            continue
        hunter = P[shooter_pid]
        hunter.has_bullet = False
        if hunter.poisoned_on == game.round:
            # 中了昨夜的毒（昨夜没吃解药）：开枪变成自杀
            kill(game, hunter.pid, DeathCause.SUICIDE)
            _emit(game, events, "shot", shooter=hunter.pid, target=target, suicide=True)
        elif P[target].role == Role.WOLF_HEAD:
            _emit(game, events, "shot", shooter=hunter.pid, target=target, blood="紫")
            _end(game, events, Team.GOOD, "猎人击中狼首，紫血洒上祭坛，大祭司无法复活")
            return events
        elif P[target].role == Role.WOLF_TAIL:
            kill(game, target, DeathCause.SHOT)
            _emit(game, events, "shot", shooter=hunter.pid, target=target, blood="蓝")
        else:
            _emit(game, events, "shot", shooter=hunter.pid, target=target, blood="红")

        if game.death_count >= SACRIFICE_TARGET:
            _end(game, events, Team.WOLF, f"献祭满 {SACRIFICE_TARGET} 具尸体，大祭司复活")
            return events

    day.stage = DayStage.VOTE
    return events


def close_vote_stage(game: GameState) -> list[Event]:
    """投票阶段结束：公布每人得票数（谁投给谁不公开），得票唯一最高者被献祭。"""
    day = game.day
    _check(game.phase == Phase.DAY and day is not None and day.stage == DayStage.VOTE,
           "现在不是投票阶段")
    P = game.players
    events: list[Event] = []

    counts: dict[int, int] = {}
    for target in day.votes.values():
        if target is not None:
            counts[target] = counts.get(target, 0) + 1
    top = max(counts.values(), default=0)
    leaders = [pid for pid, c in counts.items() if c == top]
    sacrificed = leaders[0] if top > 0 and len(leaders) == 1 else None
    if game.rules.majority_vote and top * 2 <= len(game.alive_players()):
        sacrificed = None                              # 没有超过半数，不献祭
    _emit(game, events, "vote_result", counts=counts, sacrificed=sacrificed)

    if sacrificed is not None:
        kill(game, sacrificed, DeathCause.SACRIFICED)       # 身份不公开
        if P[sacrificed].role == Role.WOLF_HEAD:
            # 狼首被献祭：就算已经没人能接任，也要等到这一夜过完才揭晓结果
            game.vacant_head = sacrificed
        if game.death_count >= SACRIFICE_TARGET and game.vacant_head is None:
            _end(game, events, Team.WOLF, f"献祭满 {SACRIFICE_TARGET} 具尸体，大祭司复活")
            return events

    # 第十天结束：狼首空缺时也要先过完这一夜，看有没有人接任
    if game.round >= MAX_DAYS and game.vacant_head is None:
        _end(game, events, Team.WOLF, f"第 {MAX_DAYS} 天结束仍未见紫血，大祭司复活")
        return events

    game.day = None
    game.round += 1
    game.phase = Phase.NIGHT
    return events


def resolve_day(game: GameState, actions: list[Action]) -> list[Event]:
    """把整个白天的行动（开枪、投票）一次交进来，自动分阶段提交并结算。
    只要有一个行动不合法，整个白天都不结算，游戏状态保持原样，然后报错。"""
    backup = copy.deepcopy(game)
    try:
        if game.day is None:
            start_day(game)
        shots = [a for a in actions if a.kind == K.SHOOT]
        votes = [a for a in actions if a.kind == K.VOTE]
        _check(len(shots) + len(votes) == len(actions), "白天只能开枪或投票")
        events: list[Event] = []
        for a in shots:
            submit(game, a.actor, [a])
        events += close_shoot_stage(game)
        if game.phase == Phase.ENDED:
            return events
        for a in votes:
            submit(game, a.actor, [a])
        events += close_vote_stage(game)
        return events
    except IllegalAction:
        game.__dict__.update(backup.__dict__)
        raise


# ---------------------------------------------------------------
# 一次性跑完整夜（写测试、机器人对局用）
# ---------------------------------------------------------------
def resolve_night(game: GameState, actions: list[Action]) -> list[Event]:
    """把整夜所有人的行动一次交进来，自动分阶段提交并结算。
    只要有一个行动不合法，整夜都不结算，游戏状态保持原样，然后报错。"""
    backup = copy.deepcopy(game)
    try:
        if game.night is None:
            start_night(game)
        pending: dict[int, list[Action]] = defaultdict(list)
        for a in actions:
            pending[a.actor].append(a)
        events: list[Event] = []

        if game.night.stage == NightStage.DIG:
            for pid in stage_players(game):          # 掘墓人的挖掘和道具在这一阶段交
                mine = [a for a in pending[pid] if a.kind == K.DIG or a.kind in ITEM_KINDS]
                pending[pid] = [a for a in pending[pid] if a not in mine]
                submit(game, pid, mine)
            events += close_dig_stage(game)

        for pid in sorted(stage_players(game)):
            submit(game, pid, pending.pop(pid, []))
        leftover = [a for lst in pending.values() for a in lst]
        _check(not leftover, f"这些行动现在不能做：{leftover}")
        events += close_main_stage(game)
        return events
    except IllegalAction:
        game.__dict__.update(backup.__dict__)   # 回滚到结算之前
        raise

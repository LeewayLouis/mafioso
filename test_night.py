"""夜晚结算的测试。每个测试对应规则文档 v3 里的一条规则。

运行方法（两种都行）：
    python test_night.py
    pytest test_night.py      （如果装了 pytest）
"""
from engine import (
    IllegalAction, close_dig_stage, close_main_stage, kill, resolve_night,
    start_night, submit, waiting_for,
)
from models import (
    Action, ActionKind as K, DeathCause, NightStage, Phase, Role, STANDARD_ROLES, Team,
    new_game,
)

# 固定座位，方便写测试：
HEAD, TAIL1, TAIL2, WITCH, GUARD, DET, GD, HUNTER = range(8)


def setup():
    return new_game(seed=0, roles=STANDARD_ROLES[:])


def next_night(game):
    """白天结算还没写，测试里先手动进入下一夜。"""
    game.round += 1
    game.phase = Phase.NIGHT


def results(events, kind, to=None):
    return [e.detail for e in events if e.kind == kind and (to is None or e.to == to)]


def raises(fn):
    try:
        fn()
    except IllegalAction:
        return True
    return False


# ---------------- 撕咬与守卫 ----------------
def test_plain_bite_kills():
    g = setup()
    ev = resolve_night(g, [Action(HEAD, K.BITE, WITCH)])
    assert not g.players[WITCH].alive
    assert g.death_count == 1 and g.graveyard == [WITCH]
    assert results(ev, "dawn")[0]["deaths"] == [WITCH]


def test_guard_protects_other_and_gets_injured():
    g = setup()
    resolve_night(g, [Action(GUARD, K.PROTECT, WITCH), Action(HEAD, K.BITE, WITCH)])
    assert g.players[WITCH].alive and g.players[GUARD].alive
    assert g.players[GUARD].injured_on == 1


def test_guard_self_protect_survives_injured():
    g = setup()
    resolve_night(g, [Action(GUARD, K.PROTECT, GUARD), Action(HEAD, K.BITE, GUARD)])
    assert g.players[GUARD].alive and g.players[GUARD].injured_on == 1


def test_injured_guard_cannot_protect_and_dies_if_bitten():
    g = setup()
    resolve_night(g, [Action(GUARD, K.PROTECT, GUARD), Action(HEAD, K.BITE, GUARD)])
    next_night(g)
    assert raises(lambda: resolve_night(g, [Action(GUARD, K.PROTECT, WITCH)]))
    resolve_night(g, [Action(HEAD, K.BITE, GUARD)])
    assert not g.players[GUARD].alive


def test_poisoned_guard_dies_when_absorbing_bite():
    g = setup()
    resolve_night(g, [Action(TAIL1, K.POISON, GUARD)])
    next_night(g)
    resolve_night(g, [Action(GUARD, K.PROTECT, WITCH), Action(HEAD, K.BITE, WITCH)])
    assert g.players[WITCH].alive          # 目标仍然受到保护
    assert not g.players[GUARD].alive      # 守卫自己死了


def test_guard_still_injured_when_victim_used_decoy():
    g = setup()
    resolve_night(g, [Action(WITCH, K.USE_DECOY), Action(GUARD, K.PROTECT, WITCH),
                      Action(HEAD, K.BITE, WITCH)])
    assert g.players[WITCH].alive
    assert g.players[GUARD].injured_on == 1


# ---------------- 替身与解药 ----------------
def test_decoy_blocks_bite_once():
    g = setup()
    resolve_night(g, [Action(WITCH, K.USE_DECOY), Action(HEAD, K.BITE, WITCH)])
    assert g.players[WITCH].alive and not g.players[WITCH].has_decoy
    next_night(g)
    assert raises(lambda: resolve_night(g, [Action(WITCH, K.USE_DECOY)]))


def test_decoy_and_antidote_not_same_night():
    g = setup()
    assert raises(lambda: resolve_night(
        g, [Action(WITCH, K.USE_DECOY), Action(WITCH, K.USE_ANTIDOTE)]))


def test_wolves_have_no_items():
    g = setup()
    assert raises(lambda: resolve_night(g, [Action(HEAD, K.USE_DECOY)]))


def test_antidote_blocks_poison_tonight():
    g = setup()
    resolve_night(g, [Action(DET, K.USE_ANTIDOTE), Action(TAIL1, K.POISON, DET)])
    assert g.players[DET].poisoned_on is None


# ---------------- 侦探 ----------------
def test_detective_leaf_falls_for_wolf_stays_for_witch():
    g = setup()
    ev = resolve_night(g, [Action(DET, K.TRACK, TAIL1)])
    assert results(ev, "track_result", DET)[0]["leaf_fell"] is True
    next_night(g)
    ev = resolve_night(g, [Action(DET, K.TRACK, WITCH)])
    assert results(ev, "track_result", DET)[0]["leaf_fell"] is False


def test_guard_self_protect_does_not_leave_tent():
    g = setup()
    ev = resolve_night(g, [Action(GUARD, K.PROTECT, GUARD), Action(DET, K.TRACK, GUARD)])
    assert results(ev, "track_result", DET)[0]["leaf_fell"] is False


def test_guard_protecting_other_leaves_tent():
    g = setup()
    ev = resolve_night(g, [Action(GUARD, K.PROTECT, WITCH), Action(DET, K.TRACK, GUARD)])
    assert results(ev, "track_result", DET)[0]["leaf_fell"] is True


def test_poisoned_detective_leaf_always_falls_until_antidote():
    g = setup()
    resolve_night(g, [Action(TAIL1, K.POISON, DET)])
    next_night(g)
    ev = resolve_night(g, [Action(DET, K.TRACK, WITCH)])
    assert results(ev, "track_result", DET)[0]["leaf_fell"] is True   # 叶子没放牢

    g = setup()
    resolve_night(g, [Action(TAIL1, K.POISON, DET)])
    next_night(g)
    ev = resolve_night(g, [Action(DET, K.USE_ANTIDOTE), Action(DET, K.TRACK, WITCH)])
    assert results(ev, "track_result", DET)[0]["leaf_fell"] is False  # 解药救回来了


# ---------------- 女巫 ----------------
def test_witch_sees_poison():
    g = setup()
    ev = resolve_night(g, [Action(WITCH, K.CHECK, DET), Action(TAIL1, K.POISON, DET)])
    assert results(ev, "check_result", WITCH)[0]["poisoned"] is True


def test_poisoned_witch_sees_opposite():
    g = setup()
    resolve_night(g, [Action(TAIL1, K.POISON, WITCH)])
    next_night(g)
    ev = resolve_night(g, [Action(WITCH, K.CHECK, DET)])           # DET 其实没中毒
    assert results(ev, "check_result", WITCH)[0]["poisoned"] is True


# ---------------- 毒的限制 ----------------
def test_poison_rules():
    g = setup()
    assert raises(lambda: resolve_night(
        g, [Action(HEAD, K.BITE, WITCH), Action(TAIL1, K.POISON, WITCH)]))
    assert raises(lambda: resolve_night(
        g, [Action(TAIL1, K.POISON, DET), Action(TAIL2, K.POISON, DET)]))
    assert raises(lambda: resolve_night(g, [Action(TAIL1, K.POISON, HEAD)]))


# ---------------- 掘墓人 ----------------
def test_gravedigger_inherits_unfired_gun():
    g = setup()
    kill(g, HUNTER, DeathCause.BITTEN)
    ev = resolve_night(g, [Action(GD, K.DIG, HUNTER)])
    gd = g.players[GD]
    assert gd.role == Role.HUNTER and gd.has_bullet
    assert results(ev, "dig_result", GD)[0]["new_role"] == "猎人"


def test_gravedigger_digging_tail_joins_wolves():
    g = setup()
    kill(g, TAIL2, DeathCause.SACRIFICED)
    ev = resolve_night(g, [Action(GD, K.DIG, TAIL2)])
    assert g.players[GD].team == Team.WOLF
    assert not g.players[GD].has_decoy        # 假设：转入狼阵营后失去道具
    assert results(ev, "wolf_joined", HEAD)    # 狼群知道他加入了


def test_poisoned_gravedigger_digs_random_corpse():
    g = setup()
    kill(g, WITCH, DeathCause.BITTEN)
    kill(g, DET, DeathCause.BITTEN)
    resolve_night(g, [Action(TAIL1, K.POISON, GD)])
    next_night(g)
    resolve_night(g, [Action(GD, K.DIG, WITCH)])
    assert g.players[GD].role in (Role.WITCH, Role.DETECTIVE)


# ---------------- 狼首接任 ----------------
def test_tail_succeeds_head_and_bites_same_night():
    g = setup()
    kill(g, HEAD, DeathCause.SACRIFICED)
    g.vacant_head = HEAD
    resolve_night(g, [Action(TAIL1, K.DIG, HEAD), Action(TAIL1, K.BITE, WITCH)])
    assert g.players[TAIL1].role == Role.WOLF_HEAD
    assert not g.players[WITCH].alive
    assert g.winner is None


def test_no_successor_means_good_wins():
    g = setup()
    kill(g, HEAD, DeathCause.SACRIFICED)
    g.vacant_head = HEAD
    resolve_night(g, [])
    assert g.winner == Team.GOOD


def test_gravedigger_steals_head_first():
    g = setup()
    kill(g, HEAD, DeathCause.SACRIFICED)
    g.vacant_head = HEAD
    start_night(g)
    assert g.night.stage == NightStage.DIG and waiting_for(g) == {GD}   # 掘墓人先行动
    submit(g, GD, [Action(GD, K.DIG, HEAD)])
    close_dig_stage(g)
    assert g.players[GD].role == Role.WOLF_HEAD
    assert raises(lambda: submit(g, TAIL1, [Action(TAIL1, K.DIG, HEAD)]))  # 狼首之位已有人
    submit(g, GD, [Action(GD, K.BITE, WITCH)])        # 掘墓人参加狼群讨论并撕咬
    close_main_stage(g)
    assert not g.players[WITCH].alive and g.winner is None


def test_successor_reaching_five_corpses_wins():
    g = setup()
    for pid in (WITCH, DET, GUARD, HUNTER):
        kill(g, pid, DeathCause.BITTEN)
    kill(g, HEAD, DeathCause.SACRIFICED)          # 第 5 具
    g.vacant_head = HEAD
    resolve_night(g, [Action(TAIL1, K.DIG, HEAD)])
    assert g.winner == Team.WOLF


# ---------------- 非法行动不会留下半截结算 ----------------
def test_illegal_night_changes_nothing():
    g = setup()
    # 替身是合法的，但撕咬狼自己不合法：整夜作废，替身不应该被用掉
    assert raises(lambda: resolve_night(
        g, [Action(WITCH, K.USE_DECOY), Action(HEAD, K.BITE, TAIL1)]))
    assert g.players[WITCH].has_decoy and g.phase == Phase.NIGHT


# ---------------- 按玩家提交：掘墓阶段 + 主阶段 ----------------
def test_no_corpse_means_no_dig_stage_and_everyone_acts_together():
    g = setup()
    start_night(g)
    assert g.night.stage == NightStage.MAIN
    assert waiting_for(g) == set(range(8))               # 所有人同时行动
    submit(g, HEAD, [Action(HEAD, K.BITE, WITCH)])
    submit(g, GUARD, [Action(GUARD, K.PROTECT, DET)])
    close_main_stage(g)                                  # 没提交的人视为什么都不做
    assert not g.players[WITCH].alive and g.phase == Phase.DAY


def test_dig_stage_only_while_gravedigger_present():
    g = setup()
    kill(g, WITCH, DeathCause.BITTEN)
    start_night(g)
    assert g.night.stage == NightStage.DIG             # 有尸体、掘墓人在场：大家等这 10 秒
    submit(g, GD, [Action(GD, K.DIG, WITCH)])
    close_dig_stage(g)
    close_main_stage(g)
    next_night(g)
    kill(g, DET, DeathCause.BITTEN)
    start_night(g)
    assert g.night.stage == NightStage.MAIN            # 掘墓人已经挖过：这 10 秒消失了

    g = setup()
    kill(g, WITCH, DeathCause.BITTEN)
    kill(g, GD, DeathCause.BITTEN)
    start_night(g)
    assert g.night.stage == NightStage.MAIN            # 掘墓人死了：也没有这 10 秒


def test_gravedigger_antidote_in_dig_stage_makes_dig_reliable():
    g = setup()
    kill(g, WITCH, DeathCause.BITTEN)
    kill(g, DET, DeathCause.BITTEN)
    resolve_night(g, [Action(TAIL1, K.POISON, GD)])
    next_night(g)
    start_night(g)
    submit(g, GD, [Action(GD, K.USE_ANTIDOTE), Action(GD, K.DIG, DET)])
    close_dig_stage(g)
    assert g.players[GD].role == Role.DETECTIVE
    assert raises(lambda: submit(g, GD, [Action(GD, K.USE_DECOY)]))   # 今晚已经用过解药


def test_bad_submission_rejected_and_can_resubmit():
    g = setup()
    start_night(g)
    assert raises(lambda: submit(g, DET, [Action(DET, K.TRACK, DET)]))  # 追踪自己
    assert DET in waiting_for(g)                         # 被拒绝，不算提交
    submit(g, DET, [Action(DET, K.TRACK, HEAD)])
    submit(g, DET, [Action(DET, K.TRACK, TAIL1)])        # 改主意，覆盖
    ev = close_main_stage(g)
    assert results(ev, "track_result", DET)[0]["target"] == TAIL1


def test_wolves_see_each_others_choices():
    g = setup()
    start_night(g)
    submit(g, TAIL1, [Action(TAIL1, K.POISON, WITCH)])
    assert raises(lambda: submit(g, HEAD, [Action(HEAD, K.BITE, WITCH)]))   # 和毒重复
    assert raises(lambda: submit(g, TAIL2, [Action(TAIL2, K.POISON, WITCH)]))
    submit(g, HEAD, [Action(HEAD, K.BITE, DET)])


def test_good_cannot_dig_outside_dig_stage():
    g = setup()
    start_night(g)
    assert raises(lambda: submit(g, GD, [Action(GD, K.DIG, WITCH)]))


# ---------------- 献祭计数 ----------------
def test_fifth_corpse_by_bite_wins_for_wolves():
    g = setup()
    for pid in (DET, GUARD, HUNTER, GD):
        kill(g, pid, DeathCause.BITTEN)
    resolve_night(g, [Action(HEAD, K.BITE, WITCH)])
    assert g.death_count == 5 and g.winner == Team.WOLF


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  通过  {name}")
        except Exception as e:
            failed += 1
            print(f"  失败  {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 个测试通过")

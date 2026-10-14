"""白天结算的测试。每个测试对应规则文档 v3 里的一条规则。

运行方法：python test_day.py
"""
from engine import (
    IllegalAction, close_shoot_stage, kill, resolve_day, resolve_night, start_day,
    submit, waiting_for,
)
from models import (
    Action, ActionKind as K, DayStage, DeathCause, MAX_DAYS, Phase, Role,
    STANDARD_ROLES, Team, new_game,
)

HEAD, TAIL1, TAIL2, WITCH, GUARD, DET, GD, HUNTER = range(8)


def setup_day():
    """开局后先过一个平安夜，进入第 1 天白天。"""
    g = new_game(seed=0, roles=STANDARD_ROLES[:])
    resolve_night(g, [])
    return g


def votes(*pairs):
    return [Action(voter, K.VOTE, target) for voter, target in pairs]


def results(events, kind):
    return [e.detail for e in events if e.kind == kind]


def raises(fn):
    try:
        fn()
    except IllegalAction:
        return True
    return False


# ---------------- 投票 ----------------
def test_unique_top_vote_is_sacrificed_identity_hidden():
    g = setup_day()
    ev = resolve_day(g, votes((HEAD, WITCH), (TAIL1, WITCH), (DET, TAIL1)))
    r = results(ev, "vote_result")[0]
    assert r["counts"] == {WITCH: 2, TAIL1: 1}          # 只公布得票数
    assert r["sacrificed"] == WITCH
    assert "role" not in r                                # 不公布身份
    assert not g.players[WITCH].alive and g.death_count == 1
    assert g.phase == Phase.NIGHT and g.round == 2


def test_tie_at_top_means_no_sacrifice():
    g = setup_day()
    ev = resolve_day(g, votes((HEAD, WITCH), (DET, TAIL1)))
    assert results(ev, "vote_result")[0]["sacrificed"] is None
    assert g.death_count == 0


def test_everyone_abstains():
    g = setup_day()
    ev = resolve_day(g, votes((HEAD, None), (DET, None)))
    assert results(ev, "vote_result")[0]["sacrificed"] is None


def test_dead_cannot_vote_and_cannot_be_voted():
    g = setup_day()
    kill(g, DET, DeathCause.BITTEN)
    assert raises(lambda: resolve_day(g, votes((DET, WITCH))))
    assert raises(lambda: resolve_day(g, votes((WITCH, DET))))


def test_cannot_vote_before_shoot_stage_ends():
    g = setup_day()
    start_day(g)
    assert g.day.stage == DayStage.SHOOT
    assert waiting_for(g) == {HUNTER}
    assert raises(lambda: submit(g, WITCH, [Action(WITCH, K.VOTE, HEAD)]))


# ---------------- 猎人开枪 ----------------
def test_shoot_head_purple_blood_good_wins():
    g = setup_day()
    ev = resolve_day(g, [Action(HUNTER, K.SHOOT, HEAD)])
    assert results(ev, "shot")[0]["blood"] == "紫"
    assert g.winner == Team.GOOD


def test_shoot_tail_blue_blood_tail_dies():
    g = setup_day()
    ev = resolve_day(g, [Action(HUNTER, K.SHOOT, TAIL1)])
    assert results(ev, "shot")[0]["blood"] == "蓝"
    assert not g.players[TAIL1].alive and g.winner is None


def test_shoot_good_red_blood_nothing_happens_bullet_gone():
    g = setup_day()
    ev = resolve_day(g, [Action(HUNTER, K.SHOOT, WITCH)])
    assert results(ev, "shot")[0]["blood"] == "红"
    assert results(ev, "shot")[0]["shooter"] == HUNTER    # 开枪是公开的
    assert g.players[WITCH].alive and not g.players[HUNTER].has_bullet
    resolve_night(g, [])
    start_day(g)
    assert waiting_for(g) == set()                        # 整局只有一枪


def test_poisoned_hunter_shoots_himself():
    g = new_game(seed=0, roles=STANDARD_ROLES[:])
    resolve_night(g, [Action(TAIL1, K.POISON, HUNTER)])
    ev = resolve_day(g, [Action(HUNTER, K.SHOOT, HEAD)])
    assert results(ev, "shot")[0]["suicide"] is True
    assert not g.players[HUNTER].alive and g.players[HEAD].alive
    assert g.winner is None


def test_antidote_last_night_keeps_shot_working():
    g = new_game(seed=0, roles=STANDARD_ROLES[:])
    resolve_night(g, [Action(HUNTER, K.USE_ANTIDOTE), Action(TAIL1, K.POISON, HUNTER)])
    resolve_day(g, [Action(HUNTER, K.SHOOT, HEAD)])
    assert g.winner == Team.GOOD


def test_gravedigger_with_inherited_gun_can_shoot():
    g = new_game(seed=0, roles=STANDARD_ROLES[:])
    kill(g, HUNTER, DeathCause.BITTEN)
    resolve_night(g, [Action(GD, K.DIG, HUNTER)])
    resolve_day(g, [Action(GD, K.SHOOT, HEAD)])
    assert g.winner == Team.GOOD


# ---------------- 狼首被献祭 ----------------
def test_head_sacrificed_tail_succeeds_next_night():
    g = setup_day()
    resolve_day(g, votes((WITCH, HEAD), (DET, HEAD)))
    assert g.vacant_head == HEAD and g.winner is None
    resolve_night(g, [Action(TAIL1, K.DIG, HEAD), Action(TAIL1, K.BITE, WITCH)])
    assert g.players[TAIL1].role == Role.WOLF_HEAD and g.winner is None


def test_head_sacrificed_with_no_possible_successor_result_after_night():
    g = setup_day()
    for pid in (TAIL1, TAIL2, GD):
        kill(g, pid, DeathCause.BITTEN)
    resolve_day(g, votes((WITCH, HEAD), (DET, HEAD)))
    assert g.winner is None and g.phase == Phase.NIGHT   # 先过一个惊心动魄的夜晚
    resolve_night(g, [Action(GUARD, K.PROTECT, WITCH)])
    assert g.winner == Team.GOOD


def test_head_as_fifth_corpse_waits_for_night():
    g = setup_day()
    for pid in (WITCH, DET, GUARD, HUNTER):
        kill(g, pid, DeathCause.BITTEN)
    resolve_day(g, votes((TAIL1, HEAD), (TAIL2, HEAD)))
    assert g.death_count == 5 and g.winner is None        # 要等今晚有没有人接任
    resolve_night(g, [Action(TAIL1, K.DIG, HEAD)])
    assert g.winner == Team.WOLF


def test_head_sacrificed_on_day_ten_waits_for_night():
    g = setup_day()
    for _ in range(MAX_DAYS - 1):
        resolve_day(g, [])
        resolve_night(g, [])
    resolve_day(g, votes((WITCH, HEAD), (DET, HEAD)))     # 第十天献祭了狼首
    assert g.winner is None
    resolve_night(g, [Action(TAIL1, K.DIG, HEAD)])        # 有人接任：大祭司复活
    assert g.winner == Team.WOLF


# ---------------- 胜负 ----------------
def test_fifth_corpse_by_vote_wolves_win():
    g = setup_day()
    for pid in (DET, GUARD, HUNTER, GD):
        kill(g, pid, DeathCause.BITTEN)
    resolve_day(g, votes((HEAD, WITCH), (TAIL1, WITCH)))
    assert g.winner == Team.WOLF


def test_fifth_corpse_by_shooting_tail_wolves_win():
    g = setup_day()
    for pid in (WITCH, DET, GUARD, GD):
        kill(g, pid, DeathCause.BITTEN)
    resolve_day(g, [Action(HUNTER, K.SHOOT, TAIL1)])
    assert g.winner == Team.WOLF


def test_day_ten_ends_with_wolves_winning():
    g = setup_day()
    for _ in range(MAX_DAYS - 1):
        resolve_day(g, [])
        resolve_night(g, [])
    assert g.round == MAX_DAYS and g.phase == Phase.DAY
    resolve_day(g, [])
    assert g.winner == Team.WOLF


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

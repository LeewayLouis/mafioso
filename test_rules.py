"""可选规则（平衡性调整）的测试。

运行方法：python test_rules.py
"""
from engine import IllegalAction, resolve_day, resolve_night
from models import Action, ActionKind as K, Rules, STANDARD_ROLES, new_game

HEAD, TAIL1, TAIL2, WITCH, GUARD, DET, GD, HUNTER = range(8)


def setup(**rules):
    return new_game(seed=0, roles=STANDARD_ROLES[:], rules=Rules(**rules))


def results(events, kind, to=None):
    return [e.detail for e in events if e.kind == kind and (to is None or e.to == to)]


def raises(fn):
    try:
        fn()
    except IllegalAction:
        return True
    return False


# ---------------- 想法 1：献祭需要超过半数 ----------------
def test_majority_needed():
    g = setup(majority_vote=True)
    resolve_night(g, [])
    ev = resolve_day(g, [Action(p, K.VOTE, WITCH) for p in (HEAD, TAIL1, TAIL2, DET)])
    assert results(ev, "vote_result")[0]["sacrificed"] is None    # 4/8 刚好一半，不行
    resolve_night(g, [])
    resolve_day(g, [Action(p, K.VOTE, WITCH) for p in (HEAD, TAIL1, TAIL2, DET, GD)])
    assert not g.players[WITCH].alive                             # 5/8 超过一半


# ---------------- 想法 2：守卫闯进狼的营帐 ----------------
def test_guard_meets_tail_both_hurt_tail_skips_tonight():
    g = setup(guard_encounter=True)
    ev = resolve_night(g, [Action(GUARD, K.PROTECT, TAIL1), Action(TAIL1, K.POISON, WITCH)])
    assert results(ev, "guard_encounter", GUARD)[0]["target"] == TAIL1   # 守卫确定了一匹狼
    assert g.players[GUARD].injured_on == 1                              # 明晚不能用技能
    assert g.players[WITCH].poisoned_on is None                          # 狼尾今晚没能下毒


def test_guard_meets_head_no_bite_tonight():
    g = setup(guard_encounter=True)
    resolve_night(g, [Action(GUARD, K.PROTECT, HEAD), Action(HEAD, K.BITE, WITCH)])
    assert g.players[WITCH].alive


def test_poisoned_guard_meets_head_guard_dies_no_bite():
    g = setup(guard_encounter=True)
    resolve_night(g, [Action(TAIL1, K.POISON, GUARD)])
    resolve_day(g, [])
    ev = resolve_night(g, [Action(GUARD, K.PROTECT, HEAD), Action(HEAD, K.BITE, WITCH)])
    assert not g.players[GUARD].alive and g.players[WITCH].alive
    assert results(ev, "dawn")[0]["deaths"] == [GUARD]


def test_poisoned_guard_meets_tail_tail_slips_away():
    g = setup(guard_encounter=True)
    resolve_night(g, [Action(TAIL1, K.POISON, GUARD)])
    resolve_day(g, [])
    resolve_night(g, [Action(GUARD, K.PROTECT, TAIL2), Action(TAIL2, K.POISON, DET)])
    assert g.players[GUARD].alive and g.players[GUARD].injured_on == 2
    assert g.players[DET].poisoned_on == 2                               # 狼尾照常下毒


def test_rule_off_means_nothing_happens():
    g = setup()
    ev = resolve_night(g, [Action(GUARD, K.PROTECT, TAIL1), Action(TAIL1, K.POISON, WITCH)])
    assert not results(ev, "guard_encounter")
    assert g.players[WITCH].poisoned_on == 1


# ---------------- 想法 3：女巫看得出狼 ----------------
def test_witch_sees_wolf():
    g = setup(witch_sees_wolf=True)
    ev = resolve_night(g, [Action(WITCH, K.CHECK, TAIL1)])
    assert results(ev, "check_result", WITCH)[0]["is_wolf"] is True


def test_poisoned_witch_sees_opposite_about_wolf():
    g = setup(witch_sees_wolf=True)
    resolve_night(g, [Action(TAIL1, K.POISON, WITCH)])
    resolve_day(g, [])
    ev = resolve_night(g, [Action(WITCH, K.CHECK, DET)])
    assert results(ev, "check_result", WITCH)[0]["is_wolf"] is True      # 其实是好人


# ---------------- 想法 5：替身和解药同一晚一起用 ----------------
def test_items_same_night():
    g = setup()
    assert raises(lambda: resolve_night(
        g, [Action(WITCH, K.USE_DECOY), Action(WITCH, K.USE_ANTIDOTE)]))
    g = setup(items_same_night=True)
    resolve_night(g, [Action(WITCH, K.USE_DECOY), Action(WITCH, K.USE_ANTIDOTE)])
    assert not g.players[WITCH].has_decoy and not g.players[WITCH].has_antidote


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

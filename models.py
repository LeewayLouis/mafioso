"""Mafioso 规则引擎：数据结构（对应规则文档 v3）

这个文件只放"数据"，不放"规则逻辑"。
规则逻辑（夜晚结算、白天投票、胜负判定）以后放在 engine.py 里。
这样分开的好处：同一套引擎，既能给机器人自动对局用，
以后也能直接接到联机服务器上给真人用。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------
# 一、基础枚举：阵营、角色、阶段、行动种类、死因
# ---------------------------------------------------------------
class Team(Enum):
    GOOD = auto()
    WOLF = auto()


class Role(Enum):
    WOLF_HEAD = "狼首"
    WOLF_TAIL = "狼尾"
    WITCH = "女巫"
    GUARD = "守卫"
    DETECTIVE = "侦探"
    GRAVEDIGGER = "掘墓人"
    HUNTER = "猎人"

    @property
    def team(self) -> Team:
        if self in (Role.WOLF_HEAD, Role.WOLF_TAIL):
            return Team.WOLF
        return Team.GOOD


class Phase(Enum):
    NIGHT = auto()
    DAY = auto()
    ENDED = auto()


class DayStage(Enum):
    SHOOT = auto()  # 讨论结束后、投票之前：猎人可以开枪
    VOTE = auto()   # 匿名投票


class NightStage(Enum):
    DIG = auto()    # 掘墓阶段：有尸体时才有，掘墓人比所有人早 10 秒做决定
    MAIN = auto()   # 主阶段：所有人同时行动（好人在各自营帐，狼在公共房间一起讨论）


class ActionKind(Enum):
    # 夜晚
    DIG = auto()           # 掘墓人：target = 要挖的尸体的座位号
    PROTECT = auto()       # 守卫：target = 自己的座位号，表示留在营帐里自我保护
    TRACK = auto()         # 侦探：在 target 的营帐帘子上塞树叶
    CHECK = auto()         # 女巫：检测 target 当晚是否中毒
    BITE = auto()          # 狼首：撕咬 target
    POISON = auto()        # 狼尾：给 target 下毒
    USE_DECOY = auto()     # 好人：使用替身（target 不用填）
    USE_ANTIDOTE = auto()  # 好人：使用解药（target 不用填）
    # 白天
    SHOOT = auto()         # 猎人：开枪打 target
    VOTE = auto()          # 所有人：投票给 target，target 为 None 表示弃权


class DeathCause(Enum):
    BITTEN = auto()      # 夜里被咬死
    SACRIFICED = auto()  # 白天被票出献祭
    SHOT = auto()        # 被猎人击中（狼尾）
    SUICIDE = auto()     # 中毒的猎人开枪自杀


# ---------------------------------------------------------------
# 二、行动：玩家"想做什么"，统一成一种格式
# ---------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    """一个玩家提交的一次行动。

    不管是机器人还是以后联机的真人，都只需要提交 Action。
    引擎负责检查它合不合法、并结算。
    """
    actor: int                    # 谁做的（座位号 0~7）
    kind: ActionKind
    target: Optional[int] = None  # 目标（座位号），有些行动不需要


# ---------------------------------------------------------------
# 三、玩家：一个座位上的人，当前身份和各种状态
# ---------------------------------------------------------------
@dataclass
class Player:
    pid: int                       # 座位号，也是唯一编号
    role: Role                     # 当前身份（掘墓人继承后会改变）
    original_role: Role            # 最初分到的身份（只用来记录和统计）
    alive: bool = True

    # 好人的一次性道具（整局各一次）
    has_decoy: bool = False
    has_antidote: bool = False
    decoy_used_on: Optional[int] = None      # 用在第几夜
    antidote_used_on: Optional[int] = None

    # 猎人
    has_bullet: bool = False

    # 状态：用"发生在第几夜"来记，而不是 True/False
    # 第 N 夜中的毒，影响第 N 天白天和第 N+1 夜的准备阶段
    poisoned_on: Optional[int] = None
    injured_on: Optional[int] = None         # 守卫受伤的夜晚

    # 死亡信息
    death_order: Optional[int] = None        # 第几个死的（用来排墓地顺序）
    death_cause: Optional[DeathCause] = None

    @property
    def team(self) -> Team:
        return self.role.team


# ---------------------------------------------------------------
# 四、事件：游戏里发生的一切都记成事件
# ---------------------------------------------------------------
@dataclass
class Event:
    """日志里的一条记录。

    public=True  ：所有人都看得到（死亡公告、投票结果、开枪）
    public=False ：只有 to 这个玩家看得到（侦探结果、女巫结果等）
    以后做界面和联机时，就是把这些事件发给对应的人。
    """
    round: int
    phase: Phase
    kind: str
    public: bool = True
    to: Optional[int] = None
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------
# 五、夜晚、白天进行中的状态
# ---------------------------------------------------------------
@dataclass
class NightState:
    """夜晚分两个阶段，每个玩家各自提交，不需要按角色排队等。"""
    stage: NightStage = NightStage.MAIN
    dig_subs: dict[int, list[Action]] = field(default_factory=dict)   # 掘墓阶段：座位号 -> 行动
    subs: dict[int, list[Action]] = field(default_factory=dict)       # 主阶段：座位号 -> 行动
    decoy: set[int] = field(default_factory=set)          # 今晚用了替身的人
    antidote: set[int] = field(default_factory=set)       # 今晚吃了解药的人
    prep_poisoned: set[int] = field(default_factory=set)  # 准备阶段仍在中毒的人
    left: set[int] = field(default_factory=set)           # 今晚离开过营帐的人


@dataclass
class DayState:
    """白天：讨论（由服务器计时）→ 猎人开枪 → 匿名投票。"""
    stage: DayStage = DayStage.SHOOT
    shots: dict[int, Optional[int]] = field(default_factory=dict)   # 猎人座位 -> 目标（None 表示不开枪）
    votes: dict[int, Optional[int]] = field(default_factory=dict)   # 投票人 -> 目标（None 表示弃权）


# ---------------------------------------------------------------
# 六、可选规则（用来测试平衡性，全部默认关闭 = 规则文档 v3）
# ---------------------------------------------------------------
@dataclass
class Rules:
    majority_vote: bool = False     # 献祭需要得票超过活着人数的一半（刚好一半不行）
    guard_encounter: bool = False   # 守卫闯进狼的营帐会触发"意外"
    witch_sees_wolf: bool = False   # 女巫检测到狼会收到提示（中毒时结论相反）
    items_same_night: bool = False  # 替身和解药可以在同一晚一起用


# ---------------------------------------------------------------
# 七、整局游戏的状态
# ---------------------------------------------------------------
@dataclass
class GameState:
    players: list[Player]
    rng: random.Random                       # 所有随机性都从这里来，方便重现 bug
    rules: Rules = field(default_factory=Rules)

    round: int = 1                           # 第几天夜（夜 r 之后是白天 r）
    phase: Phase = Phase.NIGHT
    death_count: int = 0                     # 累计死亡人数
    graveyard: list[int] = field(default_factory=list)  # 死者座位号，按死亡顺序
    dug: set[int] = field(default_factory=set)  # 已经被挖开过的尸体
    vacant_head: Optional[int] = None        # 被献祭、等待接任的狼首尸体座位号；None 表示没有空缺
    left_tent: dict[int, set[int]] = field(default_factory=dict)  # 夜晚编号 -> 离开营帐的人
    night: Optional[NightState] = None       # 夜晚进行中时才有
    day: Optional[DayState] = None           # 白天进行中时才有
    winner: Optional[Team] = None
    win_reason: str = ""
    log: list[Event] = field(default_factory=list)

    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def current_head(self) -> Optional[Player]:
        for p in self.players:
            if p.alive and p.role == Role.WOLF_HEAD:
                return p
        return None


# ---------------------------------------------------------------
# 八、开局设置（v3：固定 8 人，献祭阈值 5，最多 10 天）
# ---------------------------------------------------------------
STANDARD_ROLES = [
    Role.WOLF_HEAD, Role.WOLF_TAIL, Role.WOLF_TAIL,
    Role.WITCH, Role.GUARD, Role.DETECTIVE, Role.GRAVEDIGGER, Role.HUNTER,
]
ONE_TAIL_ROLES = [                        # 想法 4：只有一个狼尾（7 人局）
    Role.WOLF_HEAD, Role.WOLF_TAIL,
    Role.WITCH, Role.GUARD, Role.DETECTIVE, Role.GRAVEDIGGER, Role.HUNTER,
]
# v4 规则：7 人局 + 献祭过半数 + 守卫意外 + 女巫识狼 + 道具可同晚使用
V4_ROLES = ONE_TAIL_ROLES
RULES_V4 = Rules(majority_vote=True, guard_encounter=True, witch_sees_wolf=True,
                 items_same_night=True)
SACRIFICE_TARGET = 5
MAX_DAYS = 10


def new_game(seed: Optional[int] = None,
             roles: Optional[list[Role]] = None,
             rules: Optional[Rules] = None,
             role_set: Optional[list[Role]] = None) -> GameState:
    """创建一局新游戏。

    不传 roles：把 role_set（默认标准 8 人）随机分给各个座位，同一个 seed 会得到同一局。
    传入 roles：按顺序指定每个座位的身份（写测试时用）。
    rules：可选规则，不传就是规则文档 v3。
    """
    rng = random.Random(seed)
    if roles is None:
        roles = list(role_set or STANDARD_ROLES)
        rng.shuffle(roles)
    players = []
    for pid, role in enumerate(roles):
        is_good = role.team == Team.GOOD
        players.append(Player(
            pid=pid,
            role=role,
            original_role=role,
            has_decoy=is_good,
            has_antidote=is_good,
            has_bullet=(role == Role.HUNTER),
        ))
    return GameState(players=players, rng=rng, rules=rules or Rules())


if __name__ == "__main__":
    game = new_game(seed=42)
    for p in game.players:
        extra = "（有一发子弹）" if p.has_bullet else ""
        print(f"座位 {p.pid}: {p.role.value}{extra}")

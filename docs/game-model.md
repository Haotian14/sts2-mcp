# StS2 运行时数据结构地图

游戏版本 **v0.107.1**。以下路径均于 2026-07-30 在真实战斗中经
`/eval` 实测得到，非依据文档推断。

> 这些路径随游戏版本变动。若某处失效，用 `/describe?type=X` 与
> `/eval?expr=...` 重新探查即可，无须改动代码重启。

## 根路径

```
MegaCrit.Sts2.Core.Combat.CombatManager::Instance
    .IsInProgress            战斗进行中
    .IsPaused / .PlayerActionsDisabled
    ._state                  ← 私有字段，真正的状态容器
```

`CombatManager` 本身只负责流程控制，**实际状态全在私有字段 `_state` 上**。

## 战斗状态 CombatState

```
._state : CombatState
    .RoundNumber             回合数（实测 1）
    .CurrentSide             Player / Enemy
    .Encounter               遭遇模型，例：CorpseSlugsWeak
    .Players    : List<Player>
    .Enemies    : List<Creature>
    .Allies / .Creatures / .PlayerCreatures
    .HittableEnemies         可攻击目标（选取目标时用这个，而非 Enemies）
    .RunState                → 爬塔层面的状态
```

## 玩家

```
._state.Players[0] : Player
    .Character               例：Silent（静默猎手）
    .Gold                    实测 99
    .MaxEnergy               实测 3
    .Relics      : List<RelicModel>
    .PotionSlots : List<PotionModel>
    .Deck        : CardPile
    .Creature    : Creature          ← HP / 格挡在这里
    .PlayerCombatState               ← 手牌 / 能量在这里
```

注意 HP 与手牌**不在同一个对象上**：`Creature` 承载生命体属性，
`PlayerCombatState` 承载本场战斗的牌堆与能量。

### Creature（玩家与怪物共用）

```
.CurrentHp / .MaxHp / .Block
.Name                        本地化名称，例：静默猎手 / 噬尸蛞蝓
.IsAlive / .IsDead / .IsStunned
.IsHittable                  能否被指定为目标
.Powers      : List<PowerModel>      增益与减益
.Side                        Player / Enemy
.Monster                     怪物模型（玩家为 null）
.Player                      玩家对象（怪物为 null）
```

### PlayerCombatState

```
.TurnNumber
.Phase                       ★ PlayerTurnPhase，实测 Play
.Energy / .MaxEnergy         实测 3 / 3
.Hand / .DrawPile / .DiscardPile / .ExhaustPile / .PlayPile : CardPile
.AllPiles / .AllCards
.Stars                       StS2 新增资源
.OrbQueue
```

`.Phase` 是**执行动作前的就绪判据**：仅当其为 `Play` 时下发出牌才安全。

## 卡牌 CardModel

```
._state.Players[0].PlayerCombatState.Hand.Cards[i] : CardModel
    .Title                   本地化名称，例：防御
    .Type                    Skill / Attack / Power ...
    .Rarity                  Basic / Common ...
    .EnergyCost.Canonical    基础能量消耗（实测 1）
    .TargetType              Self / 单体 / 全体 ...
    .IsPlayable
    .CurrentUpgradeLevel / .IsUpgraded / .IsUpgradable
    .Keywords / .Tags
    .Description             LocString
```

**注意**：`CardModel` 属性极多（逾百项），其中大量是 Godot 视觉资源
（`Portrait`、`Frame`、`BannerMaterial` 等）。状态导出**必须显式挑选字段**，
不可整体序列化 —— 否则输出体积与 token 成本都不可接受。

另有若干属性在特定状态下会抛异常（实测 `SelectionScreenPrompt`、
`AncientTextBg` 抛 `InvalidOperationException`），逐个读取时须各自隔离。

## 怪物与意图

```
._state.Enemies[i] : Creature
    .CurrentHp / .MaxHp / .Block     实测 27 / 27 / 0
    .Name                            实测 噬尸蛞蝓
    .Monster : MonsterModel
        .NextMove : MoveState        ★ 意图
            .StateId                 实测 WHIP_SLAP_MOVE
            .Intents : AbstractIntent[]   具体意图（含伤害数值）
            .FollowUpState
        .IntendsToAttack             实测 True
        .MoveStateMachine
```

招式的数值参数直接暴露在怪物模型上，例如 `CorpseSlug`：

```
WhipSlapDamage = 3   WhipSlapRepeat = 2      鞭击 3x2
GlompDamage    = 8                            吞噬 8
GoopFrailAmt   = 2                            施加虚弱 2
RavenousStr    = 4                            贪食 力量+4
IsRavenous     = False
```

即：伤害数字既可从 `Intents` 读取，也可结合 `StateId` 与模型上的具名
参数推算。前者更通用，应优先。

## 动作执行（阶段 3 的完整路径）

### ⚠️ 不要用 `CardCmd.AutoPlay` 出牌

官方文档明确写道：

> Automatically play a card **for free**. Used for **non-player-choice** card
> playing effects. Examples: Havoc, Duplication Potion

它服务于「劫掠」「复制药水」这类**自动打出**效果，**且不消耗能量**。
用它模拟玩家出牌既是作弊，语义也不对。

### 正确路径：构造 GameAction 并入队

```
RunManager::Instance
    .ActionQueueSet         : ActionQueueSet      入队
    .ActionExecutor         : ActionExecutor      自动取出并执行
    .ActionQueueSynchronizer
    .NetService             : INetGameService
```

```csharp
var rm     = RunManager.Instance;
var ctx    = new PlayerChoiceContext();
var action = new PlayCardAction(player, card, target, ctx);
rm.ActionQueueSet.EnqueueWithoutSynchronizing(action);
await action.CompletionTask;
```

此路径与玩家点击出牌一致：正常消耗能量、触发全部 hook。

### `PlayerChoiceContext` 可直接 new

结构极简 —— 基类仅一个字段 `_modelStack : Stack<AbstractModel>`。
官方注释说明其用途：

> A stack of models that are involved with this choice context... **when we
> display the context to remote players**, we want to show the Prepared as the
> model that is involved in the choice, not the Survivor.

即**纯粹是向远程玩家显示归因用的**，不参与游戏逻辑判定。玩家主动出牌时，
语义上正确的就是一个空栈新实例。

注意其子类 `HookPlayerChoiceContext` 复杂得多（`ActionExecutor`、
`ActionQueueSet`、三个 `TaskCompletionSource`），那是给 hook 用的，不要混用。

### `EnqueueWithoutSynchronizing` 的警告在单机下不适用

该方法挂有警告：*"Only use this if you really know what you're doing!
Improper use can lead to state divergence."*

其中 **state divergence 指多人玩家之间的状态分歧**。实测单机运行时：

```
RunManager.Instance.NetService = NetSingleplayerGameService
    Type = Singleplayer,  IsConnected = True,  NetId = 1
```

单机模式下网络服务是空转实现，全部 Synchronizer 均为空操作，不存在对端，
因而没有分歧风险。**本项目仅用于单机。**

### 执行前后的判据

```
出牌前： PlayerCombatState.Phase == Play
        CardModel.IsPlayable
        CombatManager.Instance.PlayerActionsDisabled == false
出牌后： await action.CompletionTask
        并确认 CombatManager.IsExecutingCardOrPotionEffect(player) == false
        （效果可能嵌套触发，如 Sly 牌被弃置时自动打出）
选目标： 使用 CombatState.HittableEnemies
```

## 尚未探查

- `RunState` 的完整结构（地图、层数、遗物池）
- `AbstractIntent` 的具体子类与字段
- 非战斗场景：卡牌奖励、商店、事件、休息点
- `CardCmd.AutoPlay` 所需的 `PlayerChoiceContext` 如何构造

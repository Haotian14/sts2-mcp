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

> 本节 2026-08-01 由 ILSpy 反编译 `sts2.dll` 逐行核对重写。此前版本是照
> `sts2.xml` 的文档注释推断的，**三处都错**（见文末「更正记录」）。

### ⚠️ 不要用 `CardCmd.AutoPlay` 出牌

官方文档明确写道：

> Automatically play a card **for free**. Used for **non-player-choice** card
> playing effects. Examples: Havoc, Duplication Potion

它服务于「劫掠」「复制药水」这类**自动打出**效果，**且不消耗能量**。
用它模拟玩家出牌既是作弊，语义也不对。
（游戏自带的 AutoSlay 冒烟测试用的正是它 —— 那是压力测试，不在乎能量。）

### 正确路径：调用游戏自己的手动出牌入口

玩家点击一张牌，走的是这一条：

```csharp
// CardModel
public bool TryManualPlay(Creature? target)
{
    if (CanPlayTargeting(target)) { EnqueueManualPlay(target); return true; }
    return false;
}

private void EnqueueManualPlay(Creature? target)
{
    TaskHelper.RunSafely(OnEnqueuePlayVfx(target));                 // 出牌特效
    RunManager.Instance.ActionQueueSynchronizer
        .RequestEnqueue(new PlayCardAction(this, target));          // 注意是 Synchronizer
}
```

**桥接层直接调 `TryManualPlay` 即可** —— 合法性判定、特效、入队一步到位，
没有「哪一步漏了」的问题。

三类动作的入口一览：

| 动作 | 入口 |
|---|---|
| 出牌 | `CardModel.TryManualPlay(Creature target)` → bool |
| 用药水 | `PotionModel.EnqueueManualUse(Creature target)` → void |
| 结束回合 | `PlayerCmd.EndTurn(Player, canBackOut:false, null)` → void |

`EndTurn` 的第三参 `actionDuringEnemyTurn` 是测试钩子，传 null。
**反射调用不会代填可选参数的默认值，三个形参都得给。**

### 目标合法性：`IsValidTarget` 的完整规则

`CardModel.IsValidTarget`（与 `PotionModel` 的**不一样**，游戏源码专门为此
写了警告注释）：

```
target == null →  TargetType 不是 AnyEnemy 也不是 AnyAlly 才合法
target != null →  必须 IsAlive，且
                  AnyEnemy → target.Side != 自己的 Side
                  AnyAlly  → target.Side == 自己的 Side
                  其他一律 false     ← Self / AllEnemies 类的牌不可传目标
```

即：**仅 `AnyEnemy` / `AnyAlly` 需要目标，其余传目标反而非法。**

药水的差别在于 `TargetType.Self` **要**传目标（卡牌不传），
`EnqueueManualUse` 内部有兜底：目标为 null 且自身合法时自动指向自己。
桥接层复刻了这段兜底，否则「喝一瓶加血药」会被误判为缺目标。

`TargetType` 全部取值：
`None, Self, AnyEnemy, AllEnemies, RandomEnemy, AnyPlayer, AnyAlly, AllAllies,
TargetedNoCreature, Osty`

### `UnplayableReason` 全部取值

`CanPlay(out reason, out preventer)` 的 reason 是**按位或**累加的：

```
None, HasUnplayableKeyword, BlockedByHook, BlockedByCardLogic,
EnergyCostTooHigh, StarCostTooHigh, NoLivingAllies
```

判定顺序：`Unplayable` 关键字 → 资源够不够（`HasEnoughResourcesFor`）→
`AnyAlly` 牌是否还有活着的队友 → hook 是否拦截 → `IsPlayable`。

### 执行前后的判据

```
下发前： PlayerCombatState.Phase == Play          ★ 唯一可下发动作的阶段
        CombatManager.Instance.PlayerActionsDisabled == false
        CardModel.CanPlay(out reason, out preventer)      ← 不是 IsPlayable
        CardModel.IsValidTarget(target)
下发后： 轮询至「不忙」：
          RunManager.Instance.ActionQueueSet.IsEmpty
        且 RunManager.Instance.ActionExecutor.IsRunning == false
        且 CombatManager.IsExecutingCardOrPotionEffect(player) == false
        且 Phase 回到 Play（或 IsInProgress 已为 false —— 战斗结束了）
选目标： CombatState.HittableEnemies 是「可打的敌人」，
        但 /state 与 /action 的下标一律以 CombatState.Enemies 为准 ——
        两端必须用同一个集合，否则下标对不上
```

**为何不 `await action.CompletionTask`**：`TryManualPlay` 只返回 bool，
拿不到 `GameAction`。且桥接层的等待发生在 HTTP 线程，主线程绝不能阻塞等待
—— 动作正是要靠后续帧才能跑完的。故一律轮询。

`GameAction.CompletionTask` / `.State`（`None, WaitingForExecution, Executing,
GatheringPlayerChoice, ReadyToResumeExecuting, Finished, Canceled`）确实存在，
将来若需要精确到单个动作的完成，可改为自行构造动作以持有引用。

### 更正记录：此前三处推断错误

| 此前写的 | 实际 |
|---|---|
| `new PlayCardAction(player, card, target, ctx)` | 构造函数是 `(CardModel, Creature)` 两参；四参那个是给网络同步用的 `(Player, NetCombatCard, ModelId, uint?)` |
| `ActionQueueSet.EnqueueWithoutSynchronizing(action)` | 游戏自己走 `ActionQueueSynchronizer.RequestEnqueue(action)` |
| 「`PlayerChoiceContext` 可直接 new」 | 其构造函数是 **protected**，无法直接 new。所幸出牌路径根本不需要它。需要时可用具体子类 `BlockingPlayerChoiceContext` / `ThrowingPlayerChoiceContext` |

教训：`sts2.xml` 只有文档注释，没有签名细节。**签名必须从程序集元数据核对**，
不能从注释推断。现已有离线反编译工具链（见 `docs/spec.md` 的 §0.2），
成本几分钟，没有理由再猜。

## 意图 AbstractIntent

```
AbstractIntent
    .IntentType              Attack / Debuff / Buff / Stun ...
    .IntentPrefix            例：ATTACK
    .IntentLabelFormat       LocString，Variables 恒为空（渲染时才填）
  ├ AttackIntent
  │     .DamageCalc  : Func<decimal>            ⚠️ 只是基础伤害，不要直接用
  │     .Repeats     : int
  │     .GetSingleDamage(IEnumerable<Creature> targets, Creature owner) : int  ★
  │     .GetTotalDamage (IEnumerable<Creature> targets, Creature owner) : int  ★
  ├ SingleAttackIntent : AttackIntent           GetTotalDamage = GetSingleDamage
  └ MultiAttackIntent  : AttackIntent           GetTotalDamage = 单次 × Repeats
```

### ⚠️ `DamageCalc` 不含力量等修正 —— 必须用 `GetSingleDamage`

```csharp
public int GetSingleDamage(IEnumerable<Creature> targets, Creature owner)
{
    decimal num = DamageCalc();
    Player me = LocalContext.GetMe(owner.CombatState);
    if (me != null)
        num = Hook.ModifyDamage(me.RunState, me.Creature.CombatState, me.Creature,
                                owner, DamageCalc(), ValueProp.Move, null,
                                ModifyDamageHookType.All, CardPreviewMode.None, out _);
    return Math.Max(0, (int)num);
}
```

`Hook.ModifyDamage` 才是完整的伤害管线（力量、虚弱、易伤、遗物……），
游戏画在意图上的数字就来自这里。

**2026-08-01 实测判别**：噬尸蛞蝓吃掉同伴后获得 `StrengthPower 4`，
此时 `DamageCalc()` 仍报 3、`Repeats` 2，而结束回合实际掉血 **11**：

```
(3 + 4) × 2 − 3 格挡 = 11        ← 与实测吻合
 3      × 2 − 3 格挡 =  3        ← 若照 DamageCalc 决策会以为只挨这么多
```

照 `DamageCalc` 做决策会**系统性少挡**，且敌人力量越高低估得越离谱 ——
Boss 与精英恰恰是力量最高的地方。

`targets` 参数在 `GetSingleDamage` 内部并未被使用（它按
`LocalContext.GetMe(owner.CombatState)` 自行确定目标），传空数组即可。
桥接层无法编译期引用 `Creature`，用 `Array.CreateInstance(creature.GetType(), 0)`
造零长数组，靠数组协变满足 `IEnumerable<Creature>`。

`IntentLabelFormat.Variables` 实测恒为空字典 —— 它要到 UI 渲染时才被填充，
读它永远拿不到数字。

非攻击意图（如 `GOOP_MOVE` 的 Debuff、被吃后的 `Stun`）**没有**
`DamageCalc` / `Repeats` 成员，读取前须判断存在性，否则会把正常的多态缺失
误报成错误。

### 意图渲染的调用链（供将来核对）

```
NCreature.UpdateIntent(targets)
  → NIntent.UpdateIntent(intent, targets, Entity)      Entity 即怪物的 Creature
      → intent.GetTexture(_targets, _owner)            按 GetTotalDamage 选图标大小
      → attackIntent.GetIntentLabel(_targets, _owner)  显示的那个数字
```

## 卡牌可打性

```
CardModel.IsPlayable                                    ✗ 不要用
CardModel.CanPlay()                              : bool
CardModel.CanPlay(out UnplayableReason reason,
                  out AbstractModel preventer)   : bool  ★ 用这个
CardModel.CanPlayTargeting(Creature target)      : bool
```

⚠️ **`IsPlayable` 不表示「现在能不能打」**。实测诅咒牌 `AscendersBane`：

```
IsPlayable            = True     ← 但它的卡面写着「不能被打出」
EnergyCost.Canonical  = -1       ← 负费用即不可打出标记
EnergyCost.CostsX     = False    ← 排除 X 费牌的可能
```

`CanPlay` 才是游戏用来把卡牌置灰的判定，涵盖能量不足、诅咒、敌人封锁等全部
情形，且 out 参数直接给出原因。

## 文本渲染

卡牌与遗物的描述字段都是**未渲染的 `LocString`**（只有表名与键，如
`cards / PREPARED.description`），直接读取拿不到文本。两条渲染路径：

```
卡牌： CardModel.GetDescriptionForPile(PileType pile, Creature target) : string
       ★ 输出已代入数值的最终文本（含升级）
       ⚠️ 不含力量/虚弱/易伤等战斗修正，原因见下节「卡牌的实时数值」
       传 (PileType.Hand, null) 即可，目标相关描述退化为通用措辞

通用： LocManager.Instance.SmartFormat(LocString s,
                                      Dictionary<string,object> vars) : string
       遗物、药水、增益走这条 —— 它们没有 GetDescriptionFor* 方法
       vars 传该 LocString 自己的 .Variables
```

渲染结果含 Godot 的 BBCode 着色标记，须剥除：

```
获得5点[gold]格挡[/gold]。          →  获得5点格挡。
额外抽[blue]2[/blue]张牌。          →  额外抽2张牌。
```

`[img]path[/img]` 不能一律整段删除。`energyIcons()` / `starIcons()` 用连续图片的
**个数**表示数值（如两张能量图标就是 2 点能量），删掉会把「获得 2 点能量」
变成「获得。」。导出时连续同类图标转成 `【能量×N】` / `【星星×N】`；未知图标
至少保留短文件名。2026-08-01 用战鼓实机验证：商店、`/glossary`、
`choice.options[]` 均显示 `【能量×2】`，升级后变为 `【能量×3】`。

`CardModel.Title` 是**已渲染的 string**（实测「早有准备」），而 `RelicModel.Title`
是 `LocString` —— 两者不一致，不要想当然。

## 卡牌的实时数值 DynamicVars

> 2026-08-01 由 ILSpy 核对并实机验证。这是「卡面伤害 ≠ 实际伤害」那个坑的根。

每张牌的可变数字都装在 `DynamicVarSet` 里，一张牌通常只有 1~3 个：

```
CardModel.DynamicVars : DynamicVarSet          （由 CanonicalVars 克隆而来）
    ["Damage"]          : DamageVar            StrikeIronclad 6
    ["Block"]           : BlockVar             DefendIronclad 5
    ["VulnerablePower"] : PowerVar<...>        Bash 施加 2 层
    ["Repeat"] / ["ExtraDamage"] / ["Heal"] …  按卡而异

DynamicVar
    .BaseValue    : decimal   ★ 卡面裸值。游戏拿它做真实结算的输入
    .PreviewValue : decimal   ★ 过完修正管线后的值，即玩家在牌面上看到的数字
    .IntValue                 = (int)BaseValue —— 注意是 base，不是 preview
```

### PreviewValue 必须先算，否则等于 BaseValue

`PreviewValue` 不是属性算出来的，而是被**写**进去的：

```csharp
DynamicVarSet.ClearPreview()                                  // 全部退回 BaseValue
CardModel.UpdateDynamicVarPreview(CardPreviewMode, Creature? target, DynamicVarSet)
    → DamageVar.UpdateCardPreview 内部走 Hook.ModifyDamage(…全部修正…)
    → 结果写入 PreviewValue
```

游戏界面（`NCardVisuals`）每次刷新卡面都是先 `ClearPreview` 再 `Update`，所以
玩家看到的永远是修正后的数字。**而 `GetDescriptionForPile` 自己不调这一步** ——
它只是用 `{Damage:diff}` 这样的格式串去读当时的 `PreviewValue`
（`HighlightDifferencesFormatter` → `DynamicVar.ToHighlightedString` → `(int)PreviewValue`）。

于是从未被预览过的牌，渲染出来的就是裸值。`/glossary` 正是这种情况，加之它
一局只取一次，双重意义上给不出「此刻」的数字。这就是
`strategy.md` §5 那次 Boss 战算错斩杀线的全部原因。

### `CardPreviewMode` 在这条路上不影响结果

`Hook.ModifyDamage` 只在 `previewMode == MultiCreatureTargeting` 时分叉（对全体
敌人各算一遍、一致才合并显示）；其余取值一律走同一个 `ModifyDamageInternal`，
力量/虚弱/易伤/遗物照常生效。故取 `None` 即可 —— 与
`AttackIntent.GetSingleDamage` 的用法一致。

### 目标侧修正只有传目标才算得出

易伤挂在**挨打的那只怪**身上，`target` 传 null 时算不进去。因此
`/state` 对每只敌人各算一遍，得出 `hand[].damage_vs`。实测（打击基础 6）：

```
目标挂 VulnerablePower 2  →  9        6 × 1.5，截断
实际打出去                →  17 → 8   正好 9，与预报一致
```

### 多段次数不在 CardModel 的统一成员里

v0.107.1 反编译代码中，玩家牌共有 37 处 `AttackCommand.WithHitCount(...)` 调用。
次数有的写死为 2（双重打击），有的读 `RepeatVar.IntValue`，有的取预览管线已经
算出的 `CalculatedHits`，还有 X 能量、手牌数、目标易伤等牌面专属公式。因此
不能像敌人意图那样读取一个统一的 `Repeats` 属性。

`/state` 按逐牌核对过的公式输出：

```
hits       固定或与目标无关的攻击次数；省略等于 1
hits_vs    次数随目标变化时的逐敌数组，下标与 enemies[].i 对齐
```

`values.Damage` 和 `damage_vs` 始终是**单段**伤害，总伤害要再乘次数。实机
验证（力士第 9 层）：`TwinStrike damage_vs=[7], hits=2`，敌人 51→37，正好
`7×2=14`。其余 36 处是反编译静态核对，尚未逐张实机打出。

### 取整是截断，不是四舍五入

卡面显示走 `(int)PreviewValue`，`GetSingleDamage` 也是 `Math.Max(0, (int)num)`。
实测虚弱化（`FrailPower`，格挡 ×0.75）下的防御：`5 × 0.75 = 3.75 → 3`，
打出去玩家格挡确实是 3。导出侧必须同样截断，否则会比游戏多报 1 点。

## 爬塔层面 RunState

战斗外也能取到，故 `/state` 在地图与商店界面同样有效：

```
MegaCrit.Sts2.Core.Runs.RunManager::Instance
    .State : RunState        ★ 战斗外唯一入口
    .IsInProgress / .IsGameOver
    .ActionQueueSet / .ActionExecutor      阶段 3 用
```

```
RunState
    .CurrentActIndex         0 基，对外 +1
    .ActFloor / .TotalFloor  实测 2 / 2
    .AscensionLevel          实测 6
    .CurrentRoom             例：CombatRoom
    .RunLocation             例：act 0 coord (2, 1) room 0
    .Map / .CurrentMapCoord / .VisitedMapCoords     阶段 2.4 用
    .IsGameOver / .GameMode
    .Players : List<Player>  与 CombatState.Players 是同一批对象
```

## 多人同步快照 NetFullCombatState（**不适合状态导出**）

```
MegaCrit.Sts2.Core.Entities.Multiplayer.NetFullCombatState
    .Creatures : List<CreatureState>
         monsterId, playerId, currentHp, maxHp, block, powers
    .Players   : List<PlayerState>
         playerId, characterId, turnNumber, phase, energy, stars,
         maxPotionCount, gold, piles, potions, relics, orbs, rngSet, relicGrabBag
    .Rng       : SerializableRunRngSet
```

结构紧凑且本就为序列化设计，但**缺意图、缺可打性、缺卡面文本**，标识全为
`ModelId` 而非可读名称 —— 决策最需要的三样恰好都不在里面。故仍须手写导出。

## ★ 游戏自带一个完整的自动爬塔器：AutoSlay

命名空间 `MegaCrit.Sts2.Core.AutoSlay`。这是 MegaCrit 自用的冒烟测试 ——
**能无人值守打完一整局**（25 分钟超时、49 层、失败时 dump 状态并置退出码）。

它不是我们要用的东西（出牌走免费的 `AutoPlay`、选择一律随机、跑完直接退出
游戏），但它是**一份逐场景的、经官方验证可用的驱动路径清单** ——
阶段 2.4 / 3.4 里「非战斗场景怎么读、怎么点」的问题，答案全在这里。

```
AutoSlayer
  ._roomHandlers    : RoomType -> IRoomHandler
       Monster / Elite / Boss  → CombatRoomHandler
       Event                   → EventRoomHandler
       Shop                    → ShopRoomHandler
       Treasure                → TreasureRoomHandler
       RestSite                → RestSiteRoomHandler
  ._screenHandlers  : Type -> IScreenHandler
       NRewardsScreen              → RewardsScreenHandler          战斗奖励
       NCardRewardSelectionScreen  → CardRewardScreenHandler       卡牌三选一
       NChooseARelicSelection      → ChooseARelicScreenHandler     遗物三选一
       NDeckUpgradeSelectScreen    → DeckUpgradeScreenHandler      升级
       NDeckTransformSelectScreen  → DeckTransformScreenHandler
       NDeckEnchantSelectScreen    → DeckEnchantScreenHandler
       NDeckCardSelectScreen       → DeckCardSelectScreenHandler
       NSimpleCardSelectScreen     → SimpleCardSelectScreenHandler 「X 选 1」
       NChooseACardSelectionScreen → ChooseACardScreenHandler
       NChooseABundleSelectionScreen → ChooseABundleScreenHandler
       NGameOverScreen             → GameOverScreenHandler
       NCrystalSphereScreen        → CrystalSphereScreenHandler
  ._mapHandler      : MapScreenHandler                             地图导航
```

### 关键结论：战斗是模型驱动，非战斗是 UI 驱动

`CombatRoomHandler` 全程操作模型层（`CardModel` / `PlayerCmd`），
而**其余每一个 handler 都是找到 Godot 节点然后点它**：

```csharp
List<NMapPoint> points = UiHelper.FindAll<NMapPoint>(runNode.GlobalUi.MapScreen);
await UiHelper.Click(nextRoom);          // NClickableControl
```

即：非战斗场景没有「模型层 API」可走，官方自己也是点 UI。阶段 3.4 应照办，
不要试图绕过 UI 去改模型 —— 那会绕开一大堆界面状态机。

所需工具（均在 `MegaCrit.Sts2.Core.AutoSlay.Helpers`）：

```
UiHelper.FindAll<T>(Node start)    : List<T>     递归找某类型的全部节点
UiHelper.FindFirst<T>(Node start)  : T
UiHelper.Click(NClickableControl button, int delayMs = 100) : Task
WaitHelper.Until(Func<bool>, ct, TimeSpan?, string) : Task
WaitHelper.ForNode(Node root, string nodePath, ct, TimeSpan?) : Task<T>
```

节点根路径（`EventRoomHandler` 中写死的那种）形如
`/root/Game/RootSceneContainer/Run/RoomContainer/EventRoom`；
覆盖层用 `NOverlayStack.Instance.Peek()` / `.ScreenCount` 取当前界面 ——
**「现在该做什么决策」的判据就是它**：栈顶界面的类型。

### 地图导航

```
RunState
    .Map               : SavedActMap
        .BossMapPoint / .SecondBossMapPoint
    .CurrentMapCoord   : MapCoord    (row, col)
    .VisitedMapCoords  : IReadOnlyList<MapCoord>    空表示本章尚未走第一步
MapPoint
    .coord    : MapCoord
    .Children : IEnumerable<MapPoint>     ★ 下一步的可走节点
NMapPoint（UI 节点）
    .Point      : MapPoint
    .IsEnabled  能否点击
RunManager.Instance.RoomEntered   事件，进房完成的信号
```

AutoSlay 的走法是点 UI 节点，但**我们不必照办** —— 移动有纯模型层的路：

```csharp
// MapSelectionSynchronizer.MoveToMapCoord()，玩家点完节点后游戏自己走的
var action = new MoveToMapCoordAction(LocalContext.GetMe(runState), coord);
actionQueueSynchronizer.RequestEnqueue(action);
```

与出牌完全同形。`MoveToMapCoordAction(Player, MapCoord)` 的 `ExecuteAction`
内部再去驱动 `NMapScreen.TravelToMapCoord` 与 `RunManager.EnterMapCoord`。
连 `MapCoord` 都不用自己构造 —— 直接把子节点自带的 `coord` 传回去。

可走节点：`VisitedMapCoords` 为空（本章还没走第一步）时取 `Map.Grid` 里
`coord.row == 0` 的全部节点，否则取 `CurrentMapPoint.Children`。

### ⚠️ 地图是「界面」不是「房间」

```
NMapScreen.Instance          = NRun.Instance?.GlobalUi.MapScreen
    .IsOpen           : bool     ★ 纯托管自动属性，读它不碰原生侧
    .IsTravelEnabled  : bool     ★ 游戏自己用来控制「此刻能否点节点」
```

`CurrentRoom is MapRoom` **恒为 false**：打完一个房间后地图界面浮出来，
而 `CurrentRoom` 仍停在刚打完的那个（实测停在 `EventRoom`、`IsPreFinished=true`）。
判断能否移动只能用 `IsOpen && IsTravelEnabled`。

### ⚠️ `MapPoint.Children` 是 HashSet，枚举顺序不稳定

对外给下标前必须排序（按 `(row, col)`），且导出与执行要共用同一个排序入口，
否则模型读到的选项与执行时的下标可能对不上。

### ⚠️ 坐标先于房间就位

移动后 `CurrentMapCoord` 会**早于** `CurrentRoom` 更新。只判坐标会读到
`room=null, in_combat=false` 的中间态。完成判据须是：
坐标到位 → `CurrentRoom != null` → 若为 `CombatRoom` 还要等 `IsInProgress`
且 `Phase == Play`。

### ★ 玩家选择：`CardSelectCmd.UseSelector` 是官方注入点

一切「让玩家挑牌」的场景 —— 弃牌、检索、除卡、升级、转化、卡牌奖励、
「三选一」—— 最终都收口到一个可替换的接口：

```csharp
namespace MegaCrit.Sts2.Core.TestSupport;
public interface ICardSelector
{
    Task<IEnumerable<CardModel>> GetSelectedCards(
        IEnumerable<CardModel> options, int minSelect, int maxSelect);
    CardRewardSelection GetSelectedCardReward(
        IReadOnlyList<CardCreationResult> options,
        IReadOnlyList<CardRewardAlternative> alternatives);
}

CardSelectCmd.UseSelector(ICardSelector)  : IDisposable    ★ 装上自己的选择器
CardSelectCmd.PushSelector(ICardSelector) : IDisposable
CardSelectCmd.Selector                    : ICardSelector
```

AutoSlay 就是这么做的：`CardSelectCmd.UseSelector(new AutoSlayCardSelector(rng))`。

调用点覆盖了几乎全部选牌场景：

```
FromHand / FromHandForDiscard / FromHandForUpgrade      手牌内选择（无覆盖界面）
FromCombatPile                                          从牌堆里选
FromDeckForRemoval / ForUpgrade / ForTransformation
      / ForEnchantment / Generic                        商店与休息点
FromSimpleGrid / FromSimpleGridForRewards               「X 选 1」
FromChooseACardScreen / FromChooseABundleScreen         事件类
```

**这比点 UI 干净得多**，且 `options` / `minSelect` / `maxSelect` 正是要回报给
模型的信息。阶段 3.4 的选牌部分应走这条路，只有地图、商店按钮、事件选项这类
非选牌交互才需要退回 UI 点击。

桥接层无法编译期实现游戏的接口，用 BCL 的 `System.Reflection.DispatchProxy`
在运行时生成实现即可 —— **已落地**，见 `src/Sts2Bridge/CardChoice.cs`。
它有三条要求，且全都只在运行时报错：

- `.NET 9` 起 `Create` 有泛型与非泛型两个重载，`GetMethod("Create", …)` 会抛
  `AmbiguousMatchException`；按签名遍历挑非泛型的 `Create(Type, Type)` 最省事
- 代理类型**不能 sealed**（DispatchProxy 靠继承它来生成实现）
- 代理类型**必须是 public 顶层类型**（生成的代理在另一个动态程序集里）

应答时 `SetResult` **必须在主线程**：`TaskCompletionSource` 的后续会在完成它
的那个线程上就地执行，而那后续是游戏的战斗逻辑。

### ⚠️ 选择未决时，入队的出牌会被取消

`PlayCardAction` 专门重写了 `CancelAction`，注释直言：

> We override this to handle the case where some external action (like showing
> the hand selection screen) **needs to cancel queued card plays**.

2026-08-01 实测：「求生者」的弃牌选择未决时下发「中和」，桥接层报了
`ok:true`，而牌原封不动留在手里、敌人毫发无损、队列随后变空 —— 动作被取消了。

**故有未决选择时必须拒绝下发动作，而不是排队等**。桥接层已在
`ActionApi.Begin` 加了前置检查，返回 `error:"awaiting_choice"`。

判据是 `RunManager.Instance.ActionExecutor.CurrentlyRunningAction.State
== GatheringPlayerChoice`。

⚠️ **手牌内选择没有覆盖界面**：实测「求生者」弃牌时 `NOverlayStack.ScreenCount`
为 0，`Peek()` 为 null。只有弹出式界面（`NSimpleCardSelectScreen` 一类）才有。
故 `screen` 字段可能为空，不能拿它当「有没有未决选择」的判据。

⚠️ **选择期间手牌张数不变** —— 弃牌要确认后才生效。曾据此推断「选择已完成」，
结论是错的。判断只能看 `GameAction.State`。

### 战斗奖励与「界面栈排空」

`RewardsScreenHandler` 的模式值得照抄：反复找 `NRewardButton`（`IsEnabled`
且未点过；药水奖励还要先判 `player.HasOpenPotionSlots`），点一个就检查
`NOverlayStack.Instance.Peek()` 是否变了 —— 变了说明弹出了子界面
（如卡牌三选一），退出去让外层的排空循环处理。最后点 `NProceedButton`。

`AutoSlayer.DrainOverlayScreensAsync` 是那个外层循环：只要覆盖层非空就取栈顶、
按类型分派 handler、处理完继续，并带「同一界面处理 3 次仍不关闭即报死循环」
的保护。**这个结构就是阶段 3.4 的骨架。**

## 探查进度

这里原先列着三条「尚未探查」，到 2026-08-01 全部落地并实机验证过了。过期的
「尚未」标记比没有标记更坏 —— 它会让后来的人以为这条路还没走过，故改为记去处：

- **商店条目的读取与购买** → `Screens.cs` 的 `MerchantRoom` 分支。每个
  `NMerchantSlot`（**抽象基类，必须按基类匹配**）挂一个 `Entry`：`Cost` 给价格、
  `EnoughGold` 给「钱够不够」、`IsStocked` 为假的槽位不列出（卖掉之后槽位还在
  但已无内容）。除卡服务没有商品模型，标识退回 `entry` 的类型名。买 = `pick(i)`，
  除卡会接着弹 `choice`，用 `choose` 应答。见 spec.md §3.4f
- **事件选项的文本** → 走 `Screens.cs` 的 `default:` 兜底分支列出可点按钮，
  选项的中文名与效果文本由 6.4c 补在 `screen.options[].title` / `.text` 上。
  ⚠️ 读到的是**游戏显示给玩家的那段文本**，不是程序化的后果预测；后果仍然只能
  从文本里读，读错就是选错。见 spec.md §3.4e / §6.4c
- **休息点选项集合 `RestSiteOption`** → `Screens.cs` 的 `RestSiteRoom` 分支。
  标识取 `Option.OptionId`，**可用性挂在 `Option.IsEnabled` 上，不在按钮上** ——
  这是当初最容易踩空的一处。见 spec.md §3.4d

真正还没探到的是**第二章 Boss 之后的房间与界面**：截至 2026-08-01，
`logs/decisions.jsonl` 里 5 局最远只到第 2 章 19 层，之后的界面类型一次都没见过。
但已见过的 15 种停手界面全部有名有姓，`bridge.log` 里没有任何「认不出界面」的
记录 —— 所以这更像是**没打到**，不是读不出来。

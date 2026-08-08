# 《酒馆德州》朋友联机协议 v2（Alpha 冻结稿）

状态：Windows 权威核心、WebSocket 服务、桌面客户端与 Android v0.6.0 已按本协议
实现；该 APK 已完成当前 Windows 服务下的双手机人工可玩性验收。v1 文档保留为
历史，v1 客户端不能连接只接受
`v:2` 的服务端。当前仍是单进程、默认单活跃房间、内存态的朋友局 Alpha。

v2 的破坏性变化是：恢复令牌绑定稳定成员，不再绑定座位；创建者和加入者都先成为
未入座成员；2–9 个物理座位初始全部为空，由真人自由选座或由房主在指定座位加入
服务器 AI。v2 同时增加逐席买入、补码审批、低码提示、暂停和自动续手。

## 1. 不可越过的边界

- `engine.Table` 是唯一权威状态；客户端只提交意图，不能结算、发牌或自行推进街道。
- JSON over WSS；单条消息最多 64 KiB，仅接受 UTF-8 文本。
- 拒绝重复 JSON 键、NaN/Infinity、未知 envelope 字段、未知消息类型，以及客户端
  envelope 任意深度出现的字面字段 `seat`。选座/管理使用 `target_seat`。
- 所有筹码是整数，`bool` 不算整数；wire 整数绝对值不得超过
  `9,007,199,254,740,991`。
- CALL 金额和 ALL-IN 总额由服务器计算；BET/RAISE 的 `to` 表示本街“加到”的总额。
- 私牌只来自认证成员当前座位的 perspective；未入座成员只能取得公共投影。
- `resume_token`、完整 WSS 私密路径和原始 hello 不得写日志、截图、仓库或错误文本。

## 2. 客户端 envelope 与连接握手

通用变更消息：

```json
{
  "v": 2,
  "type": "game.action",
  "id": "7b57c774-3138-4a38-9096-97695e37ac65",
  "room_id": "K7P4Q2",
  "expected_state": 42,
  "body": {"kind": "raise", "to": 120}
}
```

- `v` 必须是整数 `2`。
- 除 `hello/ping` 外都必须有标准格式 UUID `id`。
- 除 `hello/ping/room.create` 外都必须有 `room_id`。
- 除 `hello/ping/room.create/room.join` 外都必须有当前非负整数
  `expected_state`。
- `body` 必须是对象；省略时按 `{}`。

连接后 5 秒内首条消息必须是 hello，且同一连接只能发送一次：

```json
{"v":2,"type":"hello","body":{"client":"android","client_version":"0.6.0"}}
```

恢复连接：

```json
{
  "v":2,
  "type":"hello",
  "body":{
    "client":"android",
    "client_version":"0.6.0",
    "resume":{"room_id":"K7P4Q2","token":"APP_PRIVATE_TOKEN"}
  }
}
```

`client` 最长 32 位，`client_version` 最长 64 位。合法恢复依次收到 `welcome` 和
最新 `room.state`；同 token 新连接以关闭码 4001 替换旧连接。恢复不会补播旧动画，
且 `welcome.body.seat` 可以为 `null`。应用层 `ping` 收到 `pong`。

## 3. 创建、加入与成员凭据

创建房间：

```json
{
  "v":2,
  "type":"room.create",
  "id":"标准 UUID",
  "body":{
    "display_name":"房主",
    "player_count":6,
    "small_blind":5,
    "big_blind":10,
    "buyin":1000
  }
}
```

加入房间：

```json
{
  "v":2,
  "type":"room.join",
  "id":"标准 UUID",
  "room_id":"K7P4Q2",
  "body":{"display_name":"朋友"}
}
```

两个 body 都是严格字段集合。`player_count` 是 2–9 个物理座位容量，不是开局必须
坐满的人数；盲注和默认 `buyin` 都是整数，默认买入也须在 10–1000BB。创建/加入
只建立成员，不自动入座。成功 ACK 只在私有响应中返回：

```json
{
  "result":{
    "credential":{
      "room_id":"K7P4Q2",
      "seat":null,
      "resume_token":"仅私发的高熵 token",
      "state_version":0,
      "member_id":"稳定成员 ID",
      "is_host":true
    }
  }
}
```

token 绑定 `member_id`，换座或离座不会换 token。客户端每次均以
`room.state.viewer_seat` 作为当前座位，不得把 credential 中的签发时座位当作长期
身份。房间码仅用于发现，不是认证秘密。

## 4. v2 命令表

下表 body 字段必须精确匹配，不能附加字段。

| 命令 | body | 权限/阶段与语义 |
|---|---|---|
| `seat.claim` | `{"target_seat":N,"buyin":X}` | 未准备真人选空位；`buyin` 10–1000BB。开局后只能预约已移出的空位，下一手生效 |
| `seat.release` | `{}` | 未准备者释放大厅座位，或取消尚未生效的预约 |
| `room.ready` | `{"ready":true/false}` | 仅 LOBBY；必须先入座才能准备，取消准备后才可换座 |
| `room.start` | `{}` | 仅房主；房主须已入座且准备；至少两名真人/AI，所有已入座真人已准备 |
| `room.ai.add` | `{"target_seat":N,"persona_id":"fox","style_key":"LAG","buyin":X}` | 房主在指定空位加入 AI；进行中预约到下一手 |
| `room.ai.remove` | `{"target_seat":N}` | 房主移除 AI；进行中的有筹码 AI 不可移除 |
| `room.ai.rebuy` | `{"target_seat":N,"target_stack":X}` | 仅 BETWEEN_HANDS；房主为爆仓 AI 排队补至目标栈 |
| `room.ai.style` | `{"target_seat":N,"style_key":"BAL"}` | 房主在 LOBBY 或两手间更换 AI 打法 |
| `room.ai.fill` | `{}` | v2 兼容便利命令；仅 LOBBY 房主，按默认阵容填满空位 |
| `room.ai.clear` | `{}` | v2 兼容便利命令；仅 LOBBY 房主，只清 AI |
| `game.action` | 见下 | 仅当前行动真人 |
| `game.show` | `{}` | BETWEEN_HANDS；刚结束该手的在座参与者自愿亮牌，幂等 |
| `seat.topup.request` | `{"target_stack":X}` | 真人申请在下一手前补至目标栈；可在 PLAYING/BETWEEN_HANDS 提交 |
| `seat.topup.cancel` | `{}` | 撤销本人的补码申请 |
| `seat.topup.decline` | `{}` | BETWEEN_HANDS；本人明确跳过本次低码补码提示 |
| `seat.topup.approve` | `{"target_seat":N}` | 房主批准待审批申请 |
| `seat.topup.reject` | `{"target_seat":N}` | 房主拒绝待审批申请 |
| `seat.leave` | `{}` | BETWEEN_HANDS；爆仓真人离座并转为旁观成员 |
| `room.pause` / `room.resume` | `{}` | 房主暂停/恢复；只能在开局后的 PLAYING/BETWEEN_HANDS 暂停 |
| `game.next_hand` | `{}` | 房主兼容命令；通常由服务器 3 秒计时自动续手 |
| `room.leave` | `{}` | 非房主退出房间；进行中的有效座位不可退出 |
| `seat.rebuy` | `{"amount":X}` | 旧名兼容；v2 明确把 `amount` 解释为目标栈，等价 topup request |

AI 身份 ID：`bull fox rhino boar dog cat raven rabbit wolf bear lion tiger turtle owl
panther`，同一房间 AI 身份不能重复。打法 ID：`TAG LAG ROCK CALLER BAL MANIAC
SMALL MIX`。

投注意图严格为：

```json
{"kind":"fold"}
{"kind":"check"}
{"kind":"call"}
{"kind":"allin"}
{"kind":"bet","to":80}
{"kind":"raise","to":120}
```

前四种不能附带 `to/amount`。客户端只按 `table.legal_actions` 开按钮和限制滑杆，
最终合法性仍由服务器校验。

## 5. 选座、补码、爆仓与自动续手

- 创建后 N 个座位均为空。房主也必须点击一个“＋”并 `seat.claim`，准备后才能开始。
- 已准备成员先 `room.ready {"ready":false}`，才能 `seat.release` 或换座。
- 牌局开始后加入者可旁观并预约 removed 空位；`waiting_next_hand:true` 期间没有私牌、
  合法动作或 SHOW 权限。预约和补码在发下一手前原子生效。
- 真人 `0 < stack < 100BB` 时，结算后进入 `low_stack_prompts`；本人必须申请补码或
  `seat.topup.decline`，否则自动续手被阻塞。stack 为 0 走 `bust_decisions`。
- `target_stack` 是补后总码量，不是增加量，范围 10–1000BB，且必须高于申请时
  当前码量。当前码量 `<=400BB`（或申请者就是房主）直接为 `APPROVED`；非房主
  当前码量 `>400BB` 时为 `PENDING_APPROVAL`，由房主 approve/reject。
- 爆仓真人选择补码或 `seat.leave`；爆仓 AI 由房主 `room.ai.rebuy` 或
  `room.ai.remove`，并可先 `room.ai.style` 更换打法。
- 进入 BETWEEN_HANDS 后，客户端必须先完整展示公共牌面、摊牌/未摊牌赢家说明与
  派彩演出，再开放真人低码/爆仓弹窗或房主的 AI 爆仓处置；不得用处置弹窗遮掉
  结算。低码“暂不补码”只有在 `seat.topup.decline` 成功发出并获服务器确认后才算
  完成，发送失败、断线或拒绝时必须允许重试。
- 没有未决爆仓、低码确认和房主审批，且投影到下一手至少两席有效时，服务器在
  默认 3 秒结算停留后自动发下一手。只有真实改变状态的命令才会按新版本重排；
  错误、重放、no-op 与单纯重连不得重置原 deadline。
- AI 只在服务器 actor mailbox 中行动，每步只执行一次合法动作、推进一个版本并
  逐成员广播；默认动作间隔随机 0.8–1.2 秒，到真人行动立即停止。

暂停时服务器取消自动转换；除房主 `room.resume` 和非房主必要的 `room.leave` 外，
所有房内变更返回 `ROOM_PAUSED`。客户端可继续查看最后收到的公共牌局摘要和全桌
码量，但不能在本地伪改状态或开放投注/补码/选座/AI 控件。

## 6. `room.state` 冻结结构

外层消息：

```json
{
  "v":2,
  "type":"room.state",
  "room_id":"K7P4Q2",
  "state_version":12,
  "body":{ "schema":"tavern.room-state.v2" }
}
```

`body` 的顶层字段：

```text
schema="tavern.room-state.v2" / protocol=2 / room / state_version
phase=LOBBY|PLAYING|BETWEEN_HANDS|CLOSED
viewer_member_id / viewer_seat:int|null / viewer_is_host
host_member_id / host_seat:int|null
paused / paused_by:member_id|null / paused_by_name|null
features / config / members / seats
busted_pending / bust_decisions / low_stack_prompts / top_up_requests
public_hand_summaries / transition / table
```

能力位固定为：

```json
{
  "free_seating":true,
  "server_ai_by_seat":true,
  "top_up_approval":true,
  "pause":true,
  "automatic_next_hand":true
}
```

`config` 含 `player_count/small_blind/big_blind/buyin/min_buyin/max_buyin`，以及以筹码
表示的 `low_stack_prompt_below`（100BB）和 `self_top_up_at_or_below`（400BB）。

`members[*]` 是稳定成员而不是座位：

```text
member_id / display_name / seat:int|null / ready / is_host / waiting_next_hand
```

`seats` 始终有 `player_count` 项。空位至少包含
`seat/occupied:false/occupant_type:null`；真人/AI 共含
`seat/occupied/occupant_type/display_name/ready/is_host/waiting_next_hand`。真人另有
`member_id`，AI 另有 `persona_id/style_key`。牌局未开始时占用席有 `buyin`；开始后
各席有权威 `stack`。

决策队列：

- `bust_decisions[*]`：`target_seat/occupant_type/display_name/decision_by=SELF|HOST`。
- `low_stack_prompts[*]`：`member_id/target_seat/decision_by=SELF/visible_to_viewer`；只有
  `visible_to_viewer:true` 的本人显示强提示。
- `top_up_requests[*]`：`member_id/display_name/target_seat/target_stack/status/
  requires_host_approval`。
- `public_hand_summaries` 最多 20 手，只含公共 board、逐池 amount/eligible/payout、
  赢家及已公开牌可推导的牌型；未公开赢家只叙述“未摊牌收池”。

## 7. 安全牌桌投影

`table` 在开局前为 `null`；开局后 schema 继续是兼容的
`tavern.table-state.v1`：

```text
schema / room / state_version / viewer_seat
hand_id / street / board / button_seat / acting_seat
pots / seats / straddle / shown / result
legal_actions（仅本人正好行动时出现）
```

已入座且已生效的成员直接由 `table.snapshot(perspective=viewer_seat)` 逐字段投影；
未入座/预约下一手成员使用 `table.public_snapshot()`。禁止取得全知 snapshot/history
后删字段。不可见底牌完全省略 `cards` 字段；客户端只能画牌背，不得从本地缓存补全。
预约下一手是唯一允许房间级 `viewer_seat=N`、但 `table.viewer_seat=null` 的状态：N
只表示已占用下一手座位，不授予当前手视角；此时 table 不能含 legal_actions 或本人
私牌。客户端须结合 viewer member/room seat 的 `waiting_next_hand:true` 精确校验，
不能简单强制两个 viewer_seat 永远相等。

实时显示底池为：

```text
sum(table.pots[*].amount) + sum(table.seats[*].bet)
```

视觉层可旋转座位让本人居下，但所有 wire 座位号保持原值。`legal_actions` 缺失即
不可行动；CALL/ALL-IN 不由客户端计算。结算只读 `result`、`pot_awards`、
`public_hand_summaries` 和实际可见 `cards`。

## 8. 版本、transition 与动画

- 每次真实业务变更或每个 AI/自动续手步骤恰好增加一次 `state_version`。
- no-op ACK、错误和 UUID 缓存重放不增版本、不广播。
- `expected_state` 过期返回 `STALE_STATE`，并附该成员最新安全 `state`。
- 同一 UUID 重试返回原响应；同 UUID 改命令返回 `IDEMPOTENCY_CONFLICT`。
- 成功变更先私发 ACK，再向每条连接发送按其 token 生成的独立 `room.state`。

`transition` 是当前版本的一步表现提示，结构为
`{"state_version":N,"kind":"...",...}`。当前 kind 包括：

```text
MEMBER_JOIN / MEMBER_LEAVE
SEAT_JOIN / SEAT_MOVE / SEAT_LEAVE / READY_CHANGED
AI_ADD / AI_REMOVE / AI_STYLE
ACTION / SHOW / HAND_STARTED
TOPUP_REQUEST / TOPUP_CANCELLED / TOPUP_APPROVED / TOPUP_REJECTED / TOPUP_QUEUED
LOW_STACK_DECLINED / PAUSE / RESUME / ROOM_CLOSED
```

`ACTION` 额外给出 `hand_id/street/target_seat/action/amount/paid`；其中 `paid` 必须是
该动作当刻从行动者码量实际新增投入的非负筹码数。即使该动作终结手牌并在同一次
引擎调用中立即派奖，也不能用“动作前 stack - 派奖后 stack”算出负数。客户端应优先
用 `paid` 驱动 action bubble 和 ChipFly，但不把它反写进账目。

连续版本应把 `transition` 与**该版本逐成员安全投影的冻结 state** 作为一个不可拆的
事件排队；不能在稍后播放旧 transition 时重新读取已经前进的最新 state，否则会把
新街牌、筹码或私牌错配到旧动画。Windows 的 `ClientEvent` 采用这一契约，Android
也应保存逐版冻结副本。断线恢复或版本跳跃时立即吸附到最新权威状态，不猜测或补造
漏失动画。v2 仍没有独立 `game.event` 流；全下可能从动作直接收到最终牌面/结算
状态，UI 只能基于权威结果安排本地展示顺序。

AI/续手 deadline 绑定产生它的有效 `state_version + transition kind`。错误、UUID
缓存重放、同版本 no-op、单纯重连/替换连接或重复收到同一状态都不得取消、延后或
重置已有 deadline；只有真实状态变更、暂停、房间失去全部连接或服务关闭才可使旧
deadline 失效。这样重复点相同 ready 或网络抖动不会无限拖延 AI/下一手。

同 token 恢复会以 4001 替换旧连接。服务端对每条房内命令除锁外快速检查外，还必须
在 lifecycle lock 内、提交 actor 前再次确认 session 未 revoked、连接注册仍是同一
对象且 token→connection 映射未改变；否则旧连接可能在等待锁期间被替换后抢入一条
命令。锁内复核失败不得推进版本或广播。

## 9. 服务端消息与错误

```text
welcome     hello 成功；body.server="tavern-mp-v2-alpha1"
ack         对应请求结果；create/join 凭据只在这里私发
room.state  当前成员的完整安全房间投影
error       业务错误或传输错误
pong        ping 回复
```

常规 ACK/业务 error 用顶层 `id/room_id/ok/state_version/result|error`；常规 ACK 内嵌
最新 `state`。传输错误使用 `body:{code,message}`，通常随后关闭连接。

`room.leave` 是唯一的退役成员 ACK：服务端先使该成员失效，因此 ACK 中的安全
`state.viewer_member_id` 仍指向离开者，但该 ID 已不在 `state.members`，且
`viewer_seat:null`、没有本人 legal actions。客户端应单独校验这一退役投影，只用它
确认离房成功，随后清除 room/token/member 本地凭据；不得把它安装成仍在房内的
`latest_state`。其余常规 `room.state`/ACK 仍必须要求 viewer member 存在于 members。

客户端至少要稳定处理：

```text
UNSUPPORTED_VERSION / INVALID_JSON / DUPLICATE_FIELD / MESSAGE_TOO_LARGE
CLIENT_SEAT_FORBIDDEN / UNKNOWN_FIELD / STATE_REQUIRED / STALE_STATE
IDEMPOTENCY_CONFLICT / AUTH_FAILED / AUTH_REQUIRED / ROOM_NOT_FOUND / ROOM_FULL
ROOM_LIMIT / ROOM_CLOSED / ROOM_PAUSED / HOST_ONLY / PHASE_MISMATCH
SEAT_REQUIRED / SEAT_OCCUPIED / SEAT_NOT_AVAILABLE / CANCEL_READY_FIRST
WAITING_NEXT_HAND / HOST_NOT_READY / NOT_ENOUGH_PLAYERS / PLAYERS_NOT_READY
NOT_YOUR_TURN / ILLEGAL_ACTION / SHOW_FORBIDDEN
LOW_STACK_PENDING / LOW_STACK_PROMPT_REQUIRED / BUSTED_PENDING
INVALID_TOP_UP / TOP_UP_APPROVAL_PENDING / TOP_UP_NOT_FOUND / TOP_UP_NOT_PENDING
AI_REQUIRED / AI_ACTIVE / AI_NOT_BUSTED / AI_PERSONA_OCCUPIED / INVALID_AI
RATE_LIMITED
```

错误文案用于展示，不作为业务分支；客户端只按稳定 `code` 分支。未知字段和未知错误
必须安全忽略/通用展示，不能崩溃。

## 10. Windows 临时公网启动

可玩 Windows 房主入口可直接指定本轮物理座位容量：

```powershell
.\tools\run_friends_desktop_public.cmd --players 6
```

`--players` 接受 2–9，省略时默认 2。参数经一次性环境传给桌面客户端；创建成功后
应立即看到 N 个空座“＋”，房主不会自动占 seat 0。完整 WSS 仍只在用户明确点击
复制后进入剪贴板，Quick Tunnel 只用于短时朋友联调。

## 11. Android v0.6.0 兼容性清单

1. 保留离线单机、WSS 粘贴、强制横屏、2–9 人和服务端合法范围滑杆。
2. 协议常量改为 `2`，接受 `tavern.room-state.v2`；v1 会话须明确提示不兼容。
3. credential 保存 `member_id/is_host/token`，允许 `seat:null`；所有当前座位只读
   `viewer_seat`，断线仍以 room+token 恢复。
4. 创建/加入后直接显示 N 个圆桌“＋”。真人点空位提交 `seat.claim`；取消准备后可
   释放/换座。已占座和 AI 座位不可选。
5. 房主点“＋”时提供“自己入座 / 添加 AI”，AI 可选十五身份、八打法和 10–1000BB
   买入；两手间支持 AI 补码、移除和换打法。
6. 实现真人低码/爆仓提示、目标栈输入、400BB 审批边界和房主审批面板；所有申请
   明示“下一手生效”。先展示结算/赢家/派彩，再弹处置；decline 发送失败时不得把
   提示永久标成已处理。
7. 实现房主暂停/恢复；暂停卡片覆盖操作区，但保留只读公共历史和全桌筹码查看。
8. 联机牌桌布局向离线牌桌靠齐：清晰牌面/牌型提示/对子起黄色高亮、行动气泡、
   下注与收池 ChipFly、muck/SHOW；动画只消费 `transition` 和安全投影。
9. 以 `(transition, 该版本冻结安全 state)` 对连续版本排队动画；不得用最新 state
   回放旧 transition。版本跳跃/恢复直接吸附，且不得因等待动画阻塞网络 reader。
10. 不把服务器、Tunnel 配置、token、TexasSolver、torch/rlcard 或完整训练权重打进
    APK；不向远端 push，只在 WSL 工作树提交并回交 APK、哈希、测试与截图报告。

## 12. 当前 Alpha 限制

- 服务只监听 `127.0.0.1`；公网须由外层 TLS/WSS Tunnel 提供，客户端不得关闭证书
  校验。Quick Tunnel 和私密路径不是正式认证。
- 默认一个服务进程只允许一个活跃房间；服务器重启丢失全部房间。
- 全房无连接默认保留 900 秒供有效 token 恢复；这不是单玩家掉线托管。
- 房主不能退出房间，尚无房主迁移、sit-out、cash-out、固定邀请链接或多房目录。
- 当前没有独立分阶段 `game.event`；动画是客户端对相邻权威状态的可丢弃表现层。
- Android v0.6.0 已完成当前 Windows 权威服务下的双手机人工可玩性验收；尚未
  逐项记录的 Windows+Android、3/6/9 人布局、生命周期与断线恢复矩阵仍须分别
  验证，不能由本轮可玩结论外推为全部通过。

v1 历史契约见 [`MULTIPLAYER_PROTOCOL_V1.md`](MULTIPLAYER_PROTOCOL_V1.md)，临时公网
启动与停止边界见 [`FRIENDS_SERVER_QUICKSTART.md`](FRIENDS_SERVER_QUICKSTART.md)，
Android v0.6.0 交付信息见 [`ANDROID_CLIENT.md`](ANDROID_CLIENT.md)。

# 《酒馆德州》朋友联机协议 v1（历史归档）

> **历史协议，不能连接当前 v2 服务。** 本文仅用于追溯旧版 Android v0.5.1
> 与后端 Alpha 的 wire 契约；当前实现、客户端和新测试统一以
> [`MULTIPLAYER_PROTOCOL_V2.md`](MULTIPLAYER_PROTOCOL_V2.md) 为准。

状态：协议门禁、RoomCore、串行 actor、单房间注册表与 localhost WebSocket
服务已经实现。Windows 源码端已有后台客户端、房主大厅和服务器权威牌桌；
Android v0.5.1 已交付 2–9 真人建房、个人视角移动图形牌桌、服务端额度滑杆和
前后台横屏恢复，冻结源码为 `8650fce…`，APK SHA-256 为
`53723b73d8ee411bdea1a2c7a14070c5c465f1ccc5355ad76708a9650263a20f`。
服务端现已冻结最小权威 AI 填位契约，但 v0.5.1 的 AI 按钮仍只显示“需服务器
升级”，要到下一轮客户端接线后才能发送命令。旧稳定 EXE 尚未重新打包联机代码；
双 Android 完整整手/恢复、分阶段结算事件和长期公网入口仍待验收/实现，当前能力
仍属于朋友局 Alpha。

## 1. 设计原则

- 保留现有离线单机模式；联机是独立入口。
- 服务端 `engine.Table` 是唯一权威状态，客户端只发送意图。
- JSON over WSS；禁止 pickle、全知快照、原始历史记录和 `Enum.auto().value`。
- 客户端不发送座位、底牌、牌堆、赢家、随机种子或脚本牌面。
- 所有金额都是整数筹码，`bool` 不视为整数。
- CALL 是需补增量；BET/RAISE 是本街“加到”的总额。

## 2. 客户端 envelope

```json
{
  "v": 1,
  "type": "game.action",
  "id": "7b57c774-3138-4a38-9096-97695e37ac65",
  "room_id": "K7P4Q2",
  "expected_state": 42,
  "body": {"kind": "raise", "to": 120}
}
```

字段：

- `v`：当前只能为严格整数 `1`。
- `type`：下方列出的稳定消息字符串。
- `id`：除 `hello/ping` 外必须提供标准 UUID，用于幂等去重。
- `room_id`：除 `hello/ping/room.create` 外必须提供，最长 32 个安全字符。
- `expected_state`：除 `hello/ping/room.create/room.join` 外必须提供非负整数。
- `body`：JSON 对象；省略时按空对象处理。

单条消息最大 64 KiB。解析器拒绝重复 JSON 键、NaN/Infinity、未知 envelope
字段、未知消息类型及任意深度的 `seat` 字段。所有整数限制在
`±9,007,199,254,740,991` 内，避免客户端精度差异和超大整数 DoS。

客户端消息集合：

```text
hello
ping
room.create
room.join
room.ai.fill
room.ai.clear
room.ready
room.start
room.leave
game.action
game.show
game.next_hand
seat.rebuy
seat.leave
```

## 3. 动作意图

无额度动作：

```json
{"kind":"fold"}
{"kind":"check"}
{"kind":"call"}
{"kind":"allin"}
```

客户端不能为以上动作附带 `amount` 或 `to`。服务器根据认证座位的最新
`legal_actions` 推导 CALL 与 ALLIN 的真实金额。

有额度动作：

```json
{"kind":"bet","to":80}
{"kind":"raise","to":120}
```

`to` 必须是正整数。服务端仍需调用 `Table.apply()` 做最终合法性校验。

## 4. 幂等与状态版本

- 房间按认证座位维护有限的 `id → 原响应` 缓存。
- 相同 UUID 重试必须返回原 ACK，不能再次改变筹码。
- 相同 UUID 携带不同命令会返回 `IDEMPOTENCY_CONFLICT`。
- 每次成功变更只增加一次 `state_version`。
- 重复设置相同 ready、重复 SHOW 等业务 no-op 返回 ACK，但不增加版本也不广播。
- `expected_state` 不匹配时返回 `STALE_STATE` 与该座位的最新完整快照。
- 两个基于同一版本的并发动作最多接受一个。

## 5. 认证与恢复

- 房间码默认 6 位，字母表排除容易混淆的 `0/O/1/I`；房间码只用于发现。
- `resume_token` 使用 `secrets.token_urlsafe(32)`，具有 256 bit 来源熵。
- 传输层由 token 绑定 `room/seat`，不得信任消息 body 中的身份声明。
- 同 token 重连替换旧连接，并发送新的个人视角完整快照。
- token 只能写入 Android app-private storage；日志、URL、APK 和仓库都不能保存。

## 6. 安全牌桌投影

唯一入口：

```python
project_table_state(
    table,
    viewer_seat=authenticated_seat,
    room=room_id,
    state_version=state_version,
)
```

投影必须直接调用：

```python
table.snapshot(perspective=authenticated_seat)
```

再逐字段构造 JSON。禁止先取得全知 `snapshot()` 或 `_history_record` 后删字段。
当前 schema 为 `tavern.table-state.v1`，输出包括：

```text
schema / room / state_version / viewer_seat
hand_id / street / board / button_seat / acting_seat
pots / seats / straddle / shown / result
legal_actions（只在 viewer 正好是 acting_seat 时出现）
```

座位只在引擎确认可见时才出现 `cards` 字段：

- 行动中只见自己的底牌；
- 未摊牌收池时赢家仍隐藏；
- 摊牌只公开未弃牌参与者；
- 主动 SHOW 后公开对应座位；
- 重连继续按同一认证座位生成视角。

结果可以公开逐池金额、eligible 座位、实际 payout 与赢家，但不能附带未公开牌。

### 6.1 客户端渲染语义

`table.pots[*].amount` 只表示已经归集的主池/边池，`table.seats[*].bet` 表示仍在
本街桌面的投入。因此客户端显示的实时总底池必须为：

```text
sum(table.pots[*].amount) + sum(table.seats[*].bet)
```

例如盲注 5/10 刚投入且尚未归集时，`pots` 可以为 0，但画面必须显示底池 15。
该公式只用于展示；客户端不得据此改写筹码、下注或结算。

`viewer_seat`、`button_seat`、`acting_seat` 和每个 `seat` 都是稳定的服务端座位号。
客户端可以为了让本人位于屏幕下方而旋转视觉坐标，但不得修改座位号，也不得在
动作命令中发送 seat。`legal_actions` 只会出现在当前认证座位正好行动时；字段缺失
表示当前不可行动，客户端不得自行推导一套合法动作。

座位没有 `cards` 字段表示牌面未授权，图形客户端最多绘制牌背，不能从本地牌局、
缓存或动画补全。结算叙述只读 `result/pot_awards/winners` 与实际公开的 `cards`。
当前协议没有 `game.event`，重连后只保证最新权威状态，不保证补播发牌或筹码动画。

### 6.2 房间能力与座位身份

`room.state` 顶层用能力位声明服务器是否支持本节契约：

```json
"features":{"server_ai_fill":true}
```

大厅的每个 `seats[*]` 都带 `occupant_type`：真人为 `"HUMAN"`，服务器 AI 为
`"AI"`，空位为 `null`。只有 AI 座位额外公开稳定的 `persona_id` 与
`style_key`；例如：

```json
{
  "seat":2,
  "occupied":true,
  "occupant_type":"AI",
  "display_name":"狐狸 Foxy",
  "ready":true,
  "is_host":false,
  "persona_id":"fox",
  "style_key":"LAG"
}
```

AI 座位没有 `resume_token`、WebSocket 连接或客户端身份，不能用
`projection_for_seat` 冒充真人认证。旧客户端必须忽略不认识的 `features`、
`occupant_type`、`persona_id` 和 `style_key`，因此仍可加入纯真人房；只有显式看到
`features.server_ai_fill == true` 的新客户端才应开放 AI 填位控件。

## 7. 房间命令 body

创建房间（当前服务默认只允许一个活跃房间）：

```json
{
  "v":1,
  "type":"room.create",
  "id":"标准 UUID",
  "body":{
    "display_name":"房主",
    "player_count":2,
    "small_blind":5,
    "big_blind":10,
    "buyin":1000
  }
}
```

加入房间：

```json
{
  "v":1,
  "type":"room.join",
  "id":"标准 UUID",
  "room_id":"K7P4Q2",
  "body":{"display_name":"朋友"}
}
```

其余严格 body：

```text
room.ready     {"ready": true|false}
room.ai.fill   {}
room.ai.clear  {}
room.start     {}
room.leave     {}
game.action    见第 3 节
game.show      {}
game.next_hand {}
seat.rebuy     {"amount": 1000}
seat.leave     {}
```

`room.start`、`game.next_hand`、`room.ai.fill` 与 `room.ai.clear` 仅房主可发。
两个 AI 命令只允许在 `LOBBY` 使用，并严格要求空 body：`fill` 一次补满当前所有
空席，`clear` 只移除 AI、绝不触碰真人座位与 token；重复 fill/clear 是不推进版本
也不广播的 no-op。AI 视为已准备；所有目标座位占满且所有真人 ready 后才能开局。
成功 ACK 分别在 `result.added_count` / `result.removed_count` 返回实际变更席位数。
最小 v1 契约不接受数量、指定座位、身份或打法参数，这些都由服务器选择。爆仓真人
必须 `seat.rebuy` 或 `seat.leave`，处理完前禁止下一手。

## 8. 已实现的服务端消息

```text
welcome       hello 成功；恢复时私发 room_id/seat，但永不回显 token
ack           对应请求结果；create/join 的 credential 只在这里私发一次
room.state    body 中是该认证座位的完整房间 + 私有牌桌投影
error         传输或业务错误
pong          应用层 ping 回复
```

创建/加入成功 ACK 的凭据结构：

```json
{
  "result": {
    "credential": {
      "room_id":"K7P4Q2",
      "seat":0,
      "resume_token":"仅私发一次的高熵 token"
    }
  }
}
```

常规房内 ACK 含 `id / room_id / ok / state_version / result / state`。业务错误
含相同请求 `id` 和顶层 `error:{code,message}`，连接保持可用；hello、JSON、
消息大小等传输错误没有请求 id，使用 `body:{code,message}` 后按策略关闭。

成功且真实改变状态的命令先私发 ACK，再向每个座位发送各自的
`room.state`。错误、缓存重放和 no-op 不广播。恢复连接依次收到 `welcome`
与最新 `room.state`，不补播旧动画。

真人命令完成 ACK 与该次广播后，服务端 actor 才可启动自动推进。每次只让一个
AI 从 `table.snapshot(perspective=该 AI 座位)` 选择并执行一个动作；每个真实 AI
动作各自增加一次 `state_version`，并立即为当前每条真人连接生成独立
`room.state`。actor 在这些步骤之间保持 mailbox 串行，AI 不会与真人命令、重连
或离席并发修改牌桌。到真人行动时立即停止；两手之间若有 AI 爆仓，则按房间
`config.buyin` 每步自动补回一个并独立广播。服务端绝不自动发送
`game.next_hand` 或 `game.show`，也不会替爆仓真人做决定。

分阶段 `game.event` 尚未实现。当前全下由引擎立即跑码结算，客户端直接收到
最终权威状态；未来 UI 动画事件应保持：

```text
action → board_runout → showdown_reveal → settlement → bust_pending
```

## 9. localhost 启动与联调

服务端依赖与桌面 EXE 隔离：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-server.txt
.venv\Scripts\python.exe -m multiplayer_server --port 8765
```

健康检查为 `http://127.0.0.1:8765/health`，WebSocket 为
`ws://127.0.0.1:8765/ws`。参考 CLI 会递归脱敏 token：

```powershell
.venv\Scripts\python.exe tools\multiplayer_cli.py --create --name 房主 --players 2 --interactive
.venv\Scripts\python.exe tools\multiplayer_cli.py --join K7P4Q2 --name 朋友 --interactive
```

服务固定监听 `127.0.0.1`，这不是手机可直接访问的公网地址。进入朋友公网局
前必须由 Cloudflare Tunnel 等外层提供 `wss://`；客户端不得关闭证书校验。

也可以使用带本地 health 与 WebSocket hello/ping 冒烟的启动器：

```powershell
.venv\Scripts\python.exe tools\run_friends_alpha.py --port 8765
```

默认路径继续是 `/ws`，因此旧客户端和上面的参考 CLI 不需要修改。临时 Tunnel
应优先使用启动器在进程内生成高熵路径。下面两个直接服务参数只供开发调试，
其值会留在本机进程命令行或 PowerShell 历史中：

```powershell
.venv\Scripts\python.exe -m multiplayer_server --path-token REPLACE_WITH_32_CHAR_URL_SAFE_TOKEN
.venv\Scripts\python.exe -m multiplayer_server --ws-path /ws/REPLACE_WITH_PRIVATE_PATH
```

`--path-token` 会映射为 `/ws/<token>`。服务只接受配置后的精确路径；默认
`/ws`、附加 query、编码别名和其他路径都返回 404。私密路径不会出现在普通
配置 `repr` 或服务启动日志中。`GET /health` 始终保留在公开的 `/health`，只返回
无房间信息的 `OK` 且禁止缓存，便于 Tunnel 健康检查。

私密路径只是降低随机扫描命中率的临时访问秘密，**不是用户认证、房间认证或
授权机制**。拿到完整 WSS URL 的人仍可尝试连接；URL 一旦泄露，应停止服务并
重新生成 Tunnel 与路径。座位身份仍只由第 5 节的房间凭据和 `resume_token`
确认，且客户端必须继续校验 TLS 证书。

注册表默认在最后一条已绑定连接断开后保留空房 `900` 秒，以允许携带有效
`resume_token` 的客户端恢复；TTL 内任一合法恢复会取消本轮回收计时。空房持续
满 900 秒后，服务关闭该房间 actor、清除内存状态并释放单房间名额。可用
`--empty-room-ttl SECONDS` 调整正有限秒数；服务重启仍会立即丢失所有房间。
该 TTL 只是全房为空后的内存回收，不是单个掉线玩家的托管或踢出机制。

Windows 本机与 Cloudflare Quick Tunnel 的公开操作边界见
[`FRIENDS_SERVER_QUICKSTART.md`](FRIENDS_SERVER_QUICKSTART.md)；v1 仅为历史，
当前 Android 信息见 [`ANDROID_CLIENT.md`](ANDROID_CLIENT.md)。

## 10. Alpha 运行限制

- 一个服务进程只允许一个活跃房间；多房间前先隔离当前全局随机源。
- 生产入口不接受 seed、scripted hole 或 scripted board。
- 房主固定 seat 0，Alpha 期间不能主动离桌；暂不做 sit-out/cash-out/房主迁移。
- 核心、注册表与 Windows 布局支持 2–9 人；Android v0.5.1 已能创建 2–9 真人
  固定目标房，但多人真机完整整手仍待验收。服务端已提供权威 AI 填位与自动行动，
  v0.5.1 的 AI 按钮尚未接线，不能把它描述成手机已经可用的功能。
- 每连接最多 30 条房间命令/秒；超限断开。actor 和传输均使用独立有界出站
  队列，慢客户端不会阻塞牌桌。
- 服务器重启会结束房间，客户端必须明确提示。
- 当前没有单个玩家断线托管、房主迁移或服务器重启恢复；全房为空时仅按默认
  900 秒 TTL 保留内存态，供有效 token 恢复，超时即回收。
- 正式服务只监听 `127.0.0.1`，公网由 WSS/Tunnel 提供；任何长期密钥不进代码。

## 11. 当前稳定错误码

解析边界当前可能产生：

```text
INVALID_MESSAGE / MESSAGE_TOO_LARGE / INVALID_UTF8 / INVALID_JSON
DUPLICATE_FIELD / INVALID_JSON_NUMBER / INVALID_ENVELOPE
CLIENT_SEAT_FORBIDDEN / UNKNOWN_FIELD / UNSUPPORTED_VERSION / UNKNOWN_TYPE
INVALID_BODY / INVALID_ROOM / ROOM_REQUIRED / UNEXPECTED_ROOM
REQUEST_ID_REQUIRED / INVALID_REQUEST_ID / STATE_REQUIRED / UNEXPECTED_STATE
INVALID_ACTION / INVALID_ACTION_FIELD / ACTION_TO_REQUIRED / INVALID_ACTION_TO
INVALID_SERVER_MESSAGE
```

房间/注册表还会返回：`AUTH_FAILED`、`AUTH_REQUIRED`、`ROOM_NOT_FOUND`、
`ROOM_FULL`、`ROOM_LIMIT`、`ROOM_NOT_FULL`、`PLAYERS_NOT_READY`、
`STALE_STATE`、`IDEMPOTENCY_CONFLICT`、`NOT_YOUR_TURN`、`ILLEGAL_ACTION`、
`PHASE_MISMATCH`、`HOST_ONLY`、`HOST_LEAVE_FORBIDDEN`、`BUSTED_PENDING`、
`PLAYER_HAS_CHIPS`、`INVALID_REBUY`、`SHOW_FORBIDDEN`、`RATE_LIMITED` 等。
服务器 AI 自动补买还可能返回 `AI_REBUY_LIMIT`。

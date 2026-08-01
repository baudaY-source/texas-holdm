# 《酒馆德州》朋友联机协议 v1（Alpha）

状态：协议门禁、RoomCore、串行 actor、单房间注册表与 localhost WebSocket
服务已经实现；Windows/Android 联机大厅、牌桌 UI、分阶段结算事件和公网 WSS
入口尚未会合。当前可用两个协议客户端联调，但现有 EXE / APK 仍只提供单机。

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
room.start     {}
room.leave     {}
game.action    见第 3 节
game.show      {}
game.next_hand {}
seat.rebuy     {"amount": 1000}
seat.leave     {}
```

`room.start` 与 `game.next_hand` 仅房主可发；所有人到齐且全部 ready 后才能
开局。爆仓玩家必须 `seat.rebuy` 或 `seat.leave`，处理完前禁止下一手。

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

## 10. Alpha 运行限制

- 一个服务进程只允许一个活跃房间；多房间前先隔离当前全局随机源。
- 生产入口不接受 seed、scripted hole 或 scripted board。
- 房主固定 seat 0，Alpha 期间不能主动离桌；暂不做 sit-out/cash-out/房主迁移。
- 核心与注册表支持 2–9 真人，当前自动端到端验收以两名真人 HU 为基线；
  联机 UI 先实现 HU，再逐步开放多人布局。AI 填位后置。
- 每连接最多 30 条房间命令/秒；超限断开。actor 和传输均使用独立有界出站
  队列，慢客户端不会阻塞牌桌。
- 服务器重启会结束房间，客户端必须明确提示。
- 当前没有断线超时托管、房主迁移或服务器重启恢复；这些是公网试用前的后续项。
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

# 酒馆德州 Texas Hold'em Tavern

单机无限注德州扑克 + GTO 分析工具箱(Windows / pygame-ce)。
当前稳定版：**v1.0.0（2026-07-28）**。
当前朋友局服务器基线：**tavern-mp-v2-alpha1（2026-08-09，可玩型稳定 Alpha）**。
你走进一间地下酒馆,可从单挑到九人桌，和一群各有脾气的动物牌手同桌较劲;桌边始终有
一位沉默的教练——翻前图表、胜率估算与离线求解器——在你每次行动前
低声给出 GTO 建议,事后还能逐手复盘、按位置看盈亏、用 drills 刷题。

> 这是经过白名单导出的公开部署仓库，包含 Windows 游戏运行/构建源码、朋友局
> v2 权威服务器与桌面客户端、公开素材和轻量推理资产。它不包含开发仓库历史、
> 完整测试、NFSP 训练源码/checkpoint、个人牌局、Android/WSL 构建树、APK、
> cloudflared 或 TexasSolver 二进制。

## 功能清单

- **牌局**:2–9 人无限注德州(pokerkit 引擎),各席可独立设置 10–1000 BB
  初始买入；8/9 人桌自动启用 UTG 目标 2BB live straddle（UTG+1 首动）；
  若 straddler 不足 2BB 则只投入剩余筹码，不会虚增最小加注；
  发牌/公共牌/弃牌入堆动画、行动提示与加注滑杆；各席桌前显示多面值
  码量筹码，下注会汇入底池并在结算时推给赢家；全下时先跑牌、亮牌和
  宣布逐池赢家，再处理爆仓，不会让配额面板遮住摊牌;
  高清 Super Index 牌面持续显示当前最大牌型,并从对子起用淡黄色边框
  标出实际组成成牌的核心牌(不把踢脚误标为牌型组成);
- **酒馆选将**:开局先选 2–9 人目标，再从十五名动物候选中自由挑选最多
  八名 AI；加入顺序就是入座顺序，每名角色可单独配置打法与买入，只有
  阵容选满且金额合法时才可发牌;
- **席位管理**:牌手被清空后可补充配额、同时更换打法或移出对局；移出后
  原座位显示“＋”，可为下一手召回指定 AI、配置 10–1000BB 与打法；好友加入
  已复用同一席位契约但当前仍为离线预留；人类席位保留;
- **人设 AI**:公牛/狐狸/犀牛/屠夫猪/看门狗/流浪猫/渡鸦/兔子/灰狼，
  以及棕熊/雄狮/猛虎/老龟/夜枭/黑豹共十五名身份，单桌最多八名 AI；
  身份可搭配 TAG、LAG、岩石、跟注站、均衡、
  疯狗、小球与真实混合八类打法，并会按收池、失池、诈唬和被诈唬说话；
  多人桌翻后若只剩
  两名未弃牌玩家,会实验性启用已训练 1,244,484 手的 HU NFSP 平均策略,
  界面显示真实来源;翻前、三人及以上或模型异常时仍用原人格 AI;
- **自愿亮牌**:未摊牌结束后可点 SHOW 公开自己的手牌；AI 成功诈唬时也可能
  主动亮牌。该契约独立于下注，已为未来好友联机保留同步边界;
- **GTO 辅助面板**(牌桌按 G 开关):翻前 RFI 混合策略 / ≤15bb 推佊表 /
  翻后胜率启发式 / HU 命中预计算库,频率条 + 胜率 + 底池赔率;
- **翻前图表**:13×13 范围矩阵查看器(五位置 RFI + 推佊 5/10/15bb);
- **训练场**:HU 场景编辑器(范围矩阵、公共牌、底池/筹码、下注尺度),
  快速胜率分析 + 后台 TexasSolver 求解与 13×13 策略矩阵下钻;
  训练/Drills/图表使用独立工作区背景,题目与手牌不再被牌桌蒙版遮挡;
- **牌手训练 Drills**:翻前 RFI / 翻前推佊 / 翻后求解(需策略库)三类
  刷题,新手(清晰局)/进阶(混合局)两档,按 GTO 频率给分
  (≥60% 满分,20–60% 半分),连对与档案持久化;
- **手牌回顾**:history.jsonl 懒加载列表(万行秒开),逐手回放,
  你的每个动作按建议频率标 √/~/× 并附点评;
- **逐池牌局信息**:右侧固定保留上一手的手数、摊牌/未摊牌、主池/边池、
  赢家、摊牌牌型与实际所得；平分和奇数筹码按真实派彩展示，未摊牌不泄牌;
- **数据统计**:总手数/净盈亏/bb·100/VPIP/PFR/AF(与 ai.arena 同口径)、
  分位置盈亏、累计盈亏走势图；右上角可经二次确认永久清除牌局记录，
  清除后统计与回顾立即归零，但训练进度、模型和策略库保持不变。

## 运行方式

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe main.py
```

打包版:双击 `dist/酒馆德州/酒馆德州.exe` 即可(构建见下)。
无头截图走查:`.venv/Scripts/python.exe main.py --headless-screenshot out_shots`。

> 桌面版请一律使用 **Windows 原生 Python**(`./.venv/Scripts/python.exe`)；
> Android 可行性支线才允许在独立 WSL2 `~/...` 工作区使用专用构建环境，
> 两边的 pygame/SDL、TexasSolver、venv 与机器人 ML 环境均不得混用。

## 本地数据

- 源码启动时，牌局日志位于 `hands/history.jsonl`；打包版位于
  `dist/酒馆德州/hands/history.jsonl`。每手牌一行，另含重新配额与移出
  座位事件；“手牌回顾”和“数据统计”都从这一份文件读取。
- 统计页的“清除当前记录”会在二次确认后删除当前运行版本的整份
  `history.jsonl`，下一手牌会自动重建它。
- Drills 训练进度位于 `training/profile.json`，训练场用户场景、NFSP 模型、
  GTO 策略库及构建备份均不属于牌局清除范围。

## 朋友联机 v2（可玩型稳定 Alpha）

离线单机入口完整保留，源码主菜单另列“朋友联机 Alpha”。Windows 端已经具备
后台 WSS 客户端、房主大厅和服务器权威牌桌 UI。当前 Windows wire 已升级为破坏性
协议 v2：token 绑定稳定成员而非座位，创建/加入后均未入座，2–9 个圆桌席位从
“＋”自由选择；房主可逐席配置服务器 AI、身份、打法和 10–1000BB 买入。真人低于
100BB 会在两手间收到提示，当前码量不高于 400BB 的目标栈申请直接批准，更深筹码
申请由房主审批；房主还可暂停全桌，服务器在未决事项处理完后自动续手。

Android v0.6.0 已按冻结 v2 契约交付，并完成当前 Windows 权威服务器下的
双 Android 人工可玩性验收。它保留离线单机，在线支持自由选座、指定座位 AI、
目标栈补码与 400BB 审批、爆仓处置、暂停和安全 transition 动画。arm64-v8a APK
大小为 30,121,709 bytes，SHA-256 为
`158bc4f32288122fcded2c131e0cfc2829d947ccea08d7f62c1c2d1c65490411`，冻结源码为
`b4cba9d392f9a55f88611635b8056af1f1855c3e`。该包使用内部测试签名，仅供朋友间
sideload，不是应用商店发行版；本仓库不上传 APK 或 Android/WSL 构建源码。
上述验收只表示本轮真实双手机对局达到可玩，不替代尚未逐项记录的全部
2/6/9 人布局、生命周期与恢复矩阵。v0.5.1 及更早版本是 v1 历史基线，不能连接
当前 v2 服务。旧 v1.0.0 EXE 尚未重新打包这些联机代码。

底层 RoomCore、逐成员投影和桌面布局统一支持 2–9 人，任一未绑定 v2 客户端可创建
房间并成为未入座房主。房主通过 `room.ai.add/remove/rebuy/style` 管理指定席位；
旧 `room.ai.fill/clear` 只作为大厅兼容便利命令。AI 没有 token/WS，只读取自己的
perspective，由 RoomActor 串行行动，每步独立推进版本；普通结算默认停留 3 秒，
AI 动作默认延迟 0.8–1.2 秒。服务仍是
单进程、单活跃房间、无持久化 Alpha，尚无多房目录、房主迁移、固定邀请链接、
分阶段 `game.event` 动画或长期公网服务。

当前协议字段、命令、补码/暂停时序、可见性和 Android v0.6.0 兼容清单见
[`docs/MULTIPLAYER_PROTOCOL_V2.md`](docs/MULTIPLAYER_PROTOCOL_V2.md)；
[`v1 文档`](docs/MULTIPLAYER_PROTOCOL_V1.md) 仅作历史。Android 端继续保留离线
单机，并由独立构建链负责 WSS 客户端、移动 UI、生命周期与真机打包；本仓库不
分发 Android 构建源码或 APK。
Android v0.6.0 的能力、兼容性、完整 SHA-256 与安装边界见
[`docs/ANDROID_CLIENT.md`](docs/ANDROID_CLIENT.md)；三种服务器启动方式与停止流程见
[`docs/FRIENDS_SERVER_QUICKSTART.md`](docs/FRIENDS_SERVER_QUICKSTART.md)。

仅做本机协议联调时，单独安装服务端依赖并使用带冒烟检查的启动器：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-server.txt
.venv\Scripts\python.exe tools\run_friends_alpha.py --port 8765
```

服务只绑定 `127.0.0.1`。Windows 图形房主与手机联机底层会合只开一个
PowerShell 窗口：

```powershell
.\tools\run_friends_desktop_public.cmd --players 6
```

它会依次启动 localhost 权威服务、Cloudflare Quick Tunnel 和可操作的 Windows
房主窗口。`--players` 接受 2–9，省略时默认 2；创建后即显示 N 个空座“＋”。
大厅只显示脱敏域名，点击“复制 WSS”取得完整邀请地址；v2 手机再填写
6 位房间码和昵称加入。房主和朋友各自点击空余“＋”选座并准备，由房主在至少两席
有效时开始；须使用 v2 Android v0.6.0，v0.5.1 不能用于这一轮。关闭 Windows
联机窗口或在启动终端按 `Ctrl+C`，
启动器会精确回收本轮桌面客户端、Tunnel 与服务。

两台手机自行建房时，只启动纯服务器入口；`cloudflared` 由用户从官方渠道另行
准备，启动器不会下载或更新它：

```powershell
.\tools\run_friends_alpha_public.cmd --phone-verifies-public `
    --cloudflared "C:\path\to\cloudflared.exe"
```

第一台 v0.6.0 手机使用弹出的 WSS 创建 2–9 人房并取得房间码，第二台手机使用
同一 WSS、房间码和自己的昵称加入；双方点击空座“＋”、准备后由房主开始。
不要与 Windows 图形房主入口同时运行。

只验证手机能否加入、不实际打牌时才使用：

```powershell
.\tools\run_friends_phone_test.cmd
```

它会托管 Cloudflare Quick Tunnel、自动创建连接验证房，并集中显示完整 WSS、
6 位房间码和第二位成员加入状态；验证成员仍须通过 v2 自由选座才能实际打牌，
且该测试房不能与图形房主入口混用。Quick Tunnel 没有 SLA，完整 WSS 泄露后必须停止并重启；随机私密路径只
降低扫描命中率，不是正式认证。

## 精简部署边界

本仓库直接携带 1,244,484 手节点导出的约 1.18MB HU NFSP 轻量运行时资产，
运行游戏不需要 torch、rlcard 或 numpy。完整训练源码、checkpoint 与生产预计算
管线只存在于私有开发工作树，不属于部署包；这里不能继续训练或重新导出模型。
多人桌翻后弃到两人的模型接管仍只是实验性 HU 池近似，不能称为多人 GTO。

其他常用:`.venv/Scripts/python.exe -m ai.arena --hands 10000 --players 6 --seed 1`
(AI 竞技场风格统计);`.venv/Scripts/python.exe -m engine.simulate --hands 200`
(随机模拟 + 历史落盘);`.venv/Scripts/python.exe -m training.drills --n 5`
(drills 样题自检)。

## 可选实时求解器

主游戏、GTO 图表、内置策略、Drills 和示例解无需 TexasSolver 即可使用。
训练场的“开始求解”需要官方 **TexasSolver v0.2.0 Windows release**：

1. 按 [`third_party/texassolver/SOURCE.md`](third_party/texassolver/SOURCE.md)
   的官方地址下载压缩包；
2. 解压为 `third_party/texassolver/TexasSolver-v0.2.0-Windows/`；
3. 确认该目录内存在 `console_solver.exe`。

该第三方目录体积较大，受自身许可证约束并被 `.gitignore` 排除。

## 打包发行

```powershell
.venv/Scripts/python.exe -m pip install pyinstaller   # 一次性
.venv/Scripts/python.exe tools/build_dist.py
```

按 `texasholdem.spec` 做 one-folder 构建(分钟级),产物
`dist/酒馆德州/`。未安装 TexasSolver 时仍可构建并运行，只缺实时求解；按下文
来源自行安装官方 release 后，构建脚本才会把必要文件打入包内。脚本构建后自动
冒烟:exe 以 `--headless-screenshot` 无显示渲染全部 19 张界面截图,
验证打包后的资源路径解析(`ui/respath.py`,`sys._MEIPASS` 兼容;
手牌历史/训练档案等可写数据落在 exe 同级目录)。约 1.18MB 的 NFSP
推理权重随 `assets/` 打包,由纯标准库运行时读取。

## 目录结构

```
engine/     牌桌引擎(pokerkit 封装)、JSONL 手牌历史、随机模拟器
multiplayer/ JSON v2、成员认证、安全公共/座位视角投影、RoomCore 与串行 actor（零网络依赖）
multiplayer_client/ Windows 后台 WSS 客户端、冻结状态与断线恢复
multiplayer_server/ 可选 localhost WebSocket、单房间注册表与服务入口
ai/         十五身份/八打法/对白/启发式机器人、最佳牌型分析、HU NFSP 轻量运行时与安全路由、竞技场
gto/        翻前图表、策略库、求解器桥、离线预计算
training/   训练场场景、快速分析、drills 引擎、回顾/统计纯逻辑
ui/         主题/控件/牌面/角色/特效、买入设置及牌桌/图表/训练/drills/回顾/统计场景
assets/     高清牌面、十五张动物酒馆肖像、版本化 NFSP 轻量推理权重
third_party/texassolver/  TexasSolver v0.2.0 来源说明（二进制自行下载）
hands/      手牌历史与竞技场统计(运行产物)
tools/      截图/打包、朋友局启动器与协议诊断工具
docs/       朋友联机协议、服务器快速启动与 Android 客户端说明
tests/fixtures/  训练页内置示例解（不含完整测试套件）
```

版本更新与已知限制见 `CHANGELOG.md`；第三方来源和许可索引见
`THIRD_PARTY_NOTICES.md`。

## 第三方致谢

- **TexasSolver**([bupticybee/TexasSolver](https://github.com/bupticybee/TexasSolver),
  AGPL-3.0):仅以子进程方式调用其**未修改的发布二进制**
  `console_solver.exe`,不包含/不修改其源码(作者 FAQ 允许此集成方式);
- **gto-poker-overlay**([hellomate2](https://github.com/hellomate2/gto-poker-overlay),
  MIT):翻前 RFI/HU 求解/推佊图表的数据来源(见 `gto/charts/SOURCE.md`);
- **Adrian Kennard Super Index Playing Cards**(CC0):当前高清牌面
  (见 `assets/cards/clarity/SOURCE.md`);
- **pokerkit**(MIT):牌局状态机;**phevaluator**(Apache-2.0):手牌评估;
- **pygame-ce**(LGPL):渲染。

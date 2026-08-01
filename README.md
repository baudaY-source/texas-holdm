# 酒馆德州 Texas Hold'em Tavern

单机无限注德州扑克 + GTO 分析工具箱(Windows / pygame-ce)。
当前稳定版：**v1.0.0（2026-07-28）**。
你走进一间地下酒馆,可从单挑到九人桌，和一群各有脾气的动物牌手同桌较劲;桌边始终有
一位沉默的教练——翻前图表、胜率估算与离线求解器——在你每次行动前
低声给出 GTO 建议,事后还能逐手复盘、按位置看盈亏、用 drills 刷题。

> 这是可直接运行和构建 Windows 游戏的精简部署仓库。它包含游戏源码、
> 高清牌面、十五名 AI 肖像、1,244,484 手 HU NFSP 轻量权重、图表与启用策略；
> 不包含开发测试、完整训练 checkpoint、torch/rlcard、Android 探针、个人存档、
> 构建缓存和第三方 TexasSolver 二进制。

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

> 桌面版请使用 **Windows 原生 Python 3.12**
> (`./.venv/Scripts/python.exe`)。

## 本地数据

- 源码启动时，牌局日志位于 `hands/history.jsonl`；打包版位于
  `dist/酒馆德州/hands/history.jsonl`。每手牌一行，另含重新配额与移出
  座位事件；“手牌回顾”和“数据统计”都从这一份文件读取。
- 统计页的“清除当前记录”会在二次确认后删除当前运行版本的整份
  `history.jsonl`，下一手牌会自动重建它。
- Drills 训练进度位于 `training/profile.json`，训练场用户场景、NFSP 模型、
  GTO 策略库及构建备份均不属于牌局清除范围。

## 朋友联机 Alpha（开发中）

本仓库已经包含首个后端会合点 `mp-v1-alpha1`：严格 JSON v1、安全个人视角
投影、2–9 人权威 `RoomCore`、串行 actor，以及仅监听 localhost 的 WebSocket
服务。真实双客户端已经跑通创建/加入、准备开局、弃牌结算、全下摊牌、命令
幂等和 token 恢复替换。

现有 Windows EXE 与 Android APK 仍保持离线单机；尚未接入联机 UI、分阶段动画
事件或公网 WSS。协议金额、可见性、幂等和当前限制见
[`docs/MULTIPLAYER_PROTOCOL_V1.md`](docs/MULTIPLAYER_PROTOCOL_V1.md)。

只做本机协议联调时，可在独立环境安装服务端依赖并启动：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-server.txt
.venv\Scripts\python.exe -m multiplayer_server --port 8765
```

服务只绑定 `127.0.0.1`。手机朋友局必须由后续部署层提供正式 `wss://`，不能
把 localhost 的明文 `ws://` 地址直接暴露到公网。服务端依赖与桌面 EXE、Android
APK 的依赖保持分离。

## 可选实时求解器

主游戏、GTO 图表、内置策略、Drills 和示例解无需 TexasSolver 即可使用。
训练场的“开始求解”需要官方 **TexasSolver v0.2.0 Windows release**：

1. 按 [`third_party/texassolver/SOURCE.md`](third_party/texassolver/SOURCE.md)
   的官方地址下载压缩包；
2. 解压为
   `third_party/texassolver/TexasSolver-v0.2.0-Windows/`；
3. 确认该目录内存在 `console_solver.exe`。

该第三方目录约 148MB，受自身许可证约束并已被 `.gitignore` 排除。

## 打包发行

```powershell
.venv/Scripts/python.exe -m pip install pyinstaller   # 一次性
.venv/Scripts/python.exe tools/build_dist.py
```

按 `texasholdem.spec` 做 one-folder 构建(分钟级),产物
`dist/酒馆德州/`。未安装 TexasSolver 时仍可构建并运行，只是实时求解不可用；
安装官方 release 后构建脚本会将其必要文件一并打包。脚本构建后自动
冒烟:exe 以 `--headless-screenshot` 无显示渲染全部 19 张界面截图,
验证打包后的资源路径解析(`ui/respath.py`,`sys._MEIPASS` 兼容;
手牌历史/训练档案等可写数据落在 exe 同级目录)。约 1.18MB 的 NFSP
推理权重随 `assets/` 打包,由纯标准库运行时读取；部署不需要
torch、rlcard 或 numpy。

## 目录结构

```
engine/     牌桌引擎(pokerkit 封装)与 JSONL 手牌历史
multiplayer/ JSON v1、认证、安全视角投影、RoomCore 与串行 actor
multiplayer_server/ 可选 localhost WebSocket 服务与房间注册
ai/         十五身份/八打法/对白/启发式机器人、最佳牌型分析、HU NFSP 轻量运行时与安全路由、竞技场
gto/        翻前图表、策略库与可选求解器桥
training/   训练场场景、快速分析、drills 引擎、回顾/统计纯逻辑
ui/         主题/控件/牌面/角色/特效、买入设置及牌桌/图表/训练/drills/回顾/统计场景
assets/     高清牌面、十五张动物酒馆肖像、版本化 NFSP 轻量推理权重
third_party/texassolver/  TexasSolver v0.2.0 下载与许可说明
hands/      手牌历史与竞技场统计(运行产物)
tools/      截图验收、Windows 打包与联机命令行客户端
docs/       朋友联机协议与当前实现边界
tests/fixtures/  训练页内置示例解（不是完整测试集）
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

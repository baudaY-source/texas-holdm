# 朋友局服务器快速开始

本说明用于少量朋友临时联机。Windows 电脑运行权威服务器，Android v0.6.0 或
Windows 图形客户端只发送操作意图，洗牌、发牌、合法行动和结算均以服务器状态为准。

当前服务进程默认只承载一个活跃房间。下面的 Cloudflare Quick Tunnel 仅适合短时
朋友测试，不是长期公网部署方案。

## 准备

1. 在 Windows 项目根目录创建并安装项目虚拟环境。
2. 安装可选服务器依赖：

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-server.txt
   ```

3. 从 Cloudflare 官方渠道自行下载 `cloudflared`，例如放在
   `C:\Tools\cloudflared.exe`。启动器不会下载、安装或更新它，也不要把该可执行文件
   提交到仓库。
4. Android 端使用协议 v2 的 v0.6.0；安装前按
   [Android 客户端说明](ANDROID_CLIENT.md) 核对完整 SHA-256。

公网启动时会要求输入以下确认短语：

```text
ALPHA-TEMPORARY-PUBLIC
```

这表示操作者已经理解临时公网入口的风险，并不代表服务获得了正式认证或访问控制。

## 两台 Android：只启动服务器

在项目根目录运行：

```powershell
.\tools\run_friends_alpha_public.cmd --phone-verifies-public --cloudflared "C:\Tools\cloudflared.exe"
```

随后：

1. 输入确认短语，保留启动窗口，不要再启动其他朋友局入口。
2. 启动器取得临时 WSS 地址后，通过私密渠道把完整地址发给两台手机，不要公开截图
   或粘贴到公开聊天。
3. 手机 A 在朋友联机页粘贴 WSS、填写昵称并创建 2–9 人房间，记下 6 位房间码。
4. 手机 B 使用相同 WSS，填写自己的昵称和房间码加入。
5. 两位玩家分别点击空座“＋”选座并准备；房主在开局条件满足后开始牌局。

`--phone-verifies-public` 让外部手机承担首次公网连通验证；服务器自身的 health 与
WebSocket 冒烟仍直接走 localhost，避免把本机检查错误地绕到公网代理。

## Windows + 一台 Android

需要 Windows 图形房主和一台手机共同对局时运行：

```powershell
.\tools\run_friends_desktop_public.cmd --players 2 --cloudflared "C:\Tools\cloudflared.exe"
```

`--players` 可设为 `2` 到 `9`，表示房间的物理座位容量。启动器会同时启动权威服务、
临时 Tunnel 和 Windows 图形房主：

1. Windows 大厅显示脱敏地址、房间码和空座位。
2. 明确点击复制后，把完整 WSS 和房间码私下交给手机玩家。
3. Windows 房主与手机玩家都需要各自点击空座“＋”选座并准备。
4. 所有在座真人准备且至少有两席有效后，由房主开始牌局。

Windows 房主不会自动占用 0 号座位。

## `phone_test` 只用于连接冒烟

以下入口会自动创建无图形验证房，只用于确认手机能够通过临时 WSS 加入且服务器能
收到基本会合信号：

```powershell
.\tools\run_friends_phone_test.cmd --cloudflared "C:\Tools\cloudflared.exe"
```

它不是可玩房间，也没有 Windows 牌桌 UI。连接冒烟完成后先按 `Ctrl+C` 停止它，
再选择“只启动服务器”或“Windows + Android”入口。三个入口不要同时运行。

## 停止与重开

- 测试结束后在启动终端按 `Ctrl+C`，等待服务器、Tunnel 和由启动器创建的子进程
  一并退出。
- Quick Tunnel 每次重启通常都会产生新地址；旧地址随进程停止而失效。
- 完整 WSS 一旦被发到公开群、截图或日志中，应立即停止并重新启动，而不是继续使用。
- 不要同时手工启动第二个相同端口的服务；默认监听端口为 `8765`。

## Quick Tunnel 的限制

- 无 SLA、无固定域名，不能作为长期服务器或稳定邀请链接。
- 拿到完整 WSS 的人都可以尝试连接；高熵路径只能降低偶然扫描，**不是身份认证**。
- 当前没有账号系统、房间目录、网页邀请页、Android deep link 或长期在线托管。
- Origin 只绑定 `127.0.0.1`，不要把明文 `ws://` 端口直接映射到公网。
- 启动器不会修改用户的 Cloudflare 配置、下载二进制或保存真实 WSS；如检测到可能
  干扰 Quick Tunnel 的用户配置，应按启动器提示处理后再运行。

如需长期、固定域名或更严格的访问控制，应另行设计正式 Tunnel、反向代理、认证、
监控和备份方案，不应继续依赖本快速开始流程。

# 第三方组件与许可说明

《酒馆德州》当前源码（稳定基线 v1.0.0）使用或集成以下第三方项目。这里给出来源和许可证索引；
公开、商业或再分发前仍应按实际发行内容完成一次独立许可复核。

| 组件 | 用途 | 许可证 / 来源 |
| --- | --- | --- |
| TexasSolver v0.2.0 | HU 离线求解器，以未修改 Windows 二进制子进程调用 | AGPL-3.0；<https://github.com/bupticybee/TexasSolver>；另见 `third_party/texassolver/SOURCE.md` |
| pygame-ce | 桌面图形、输入与音频运行时 | LGPL-2.1；<https://github.com/pygame-community/pygame-ce> |
| SDL | pygame-ce 使用的跨平台底层库 | Zlib；<https://github.com/libsdl-org/SDL> |
| PokerKit | 德州状态机 | MIT；<https://github.com/uoftcprg/pokerkit> |
| phevaluator | 手牌评估 | Apache-2.0；<https://github.com/HenryRLee/PokerHandEvaluator> |
| RLCard | NFSP 离线训练环境，不进入发行包 | MIT；<https://github.com/datamllab/rlcard> |
| websockets 16.1.1 | 可选朋友局 localhost 服务端与 Windows 源码客户端；当前不进入稳定桌面 EXE / Android APK | BSD-3-Clause；<https://websockets.readthedocs.io/en/16.1.1/> |
| gto-poker-overlay 数据 | 翻前 RFI/HU/推弃图表 | MIT；<https://github.com/hellomate2/gto-poker-overlay>；另见 `gto/charts/SOURCE.md` |
| Adrian Kennard Super Index Playing Cards | 当前高清牌面素材 | CC0；<https://www.me.uk/cards/>；生成参数与下载哈希见 `assets/cards/clarity/SOURCE.md` |
| Kenney Playing Cards | 仓库保留的旧版牌面素材 | CC0；仓库内附 `assets/cards/License.txt` |
| Python / PyInstaller | Python 运行时与冻结工具 | PSF / GPL bootloader exception；<https://www.python.org/psf/license/>、<https://pyinstaller.org/en/stable/license.html> |

TexasSolver 的 Windows 发布目录还包含其上游构建所需的 Qt 组件。若准备公开
发布或商业使用，应同时复核 TexasSolver、Qt、pygame-ce/SDL 及冻结包中实际
收录模块的完整许可义务。本项目没有修改 TexasSolver 源码。

本仓库自身目前没有声明面向公众的开源许可；除第三方组件各自授予的权利外，
未经项目所有者另行授权，不应把本稳定版视为自动获得了再分发许可。

# 翻前图表数据来源与许可证

本目录的 JSON 图表由 `tools/convert_charts.py` 从以下上游仓库的机器可读
数据转换而来:

- 仓库: <https://github.com/hellomate2/gto-poker-overlay>
- 路径: `src/core/ranges/`(`greenline-gto.ts` / `headsup-solved.ts` / `pushfold-nash.ts`)
- 许可证: **MIT License**,Copyright (c) 2026 Dev Lakhani(全文见下)

## 各文件说明

| 文件 | 内容 | 上游图表出处 |
| --- | --- | --- |
| `rfi_6max.json` | 6-max 翻前率先加注(RFI)混合策略,UTG/MP/CO/BTN/SB 五位置 × 169 牌型 | Greenline Poker《GreenCharts2024_01.pdf》第 4 页(开池尺度 2.5-3bb) |
| `hu_solved.json` | 单挑 100bb 翻前 CFR+ 纳什求解(SB 开池/BB 防守/3bet/4bet) | 上游自带离线 CFR+ 求解器(150 次迭代,NashConv -0.447bb) |
| `pushfold.json` | 短筹码(≤25bb)单挑 Nash 推佊/跟注阈值表 | Sklansky-Chubukov 排序 + HoldemResources/SnapShove 式 Nash 表(数值为仍可全下/跟注的最大有效筹码 bb,999 = 任意深度) |

未在上游图表中出现的牌型一律视为纯弃牌;混合策略频率保留三位小数。
本项目的 M5 求解器桥落地前,这些数据是 GTO 辅助面板的唯一权威翻前依据。

## MIT License 全文

```
MIT License

Copyright (c) 2026 Dev Lakhani

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

# TexasSolver(第三方组件)

- 项目: https://github.com/bupticybee/TexasSolver
- 版本: v0.2.0(Windows release 二进制)
- 许可: AGPL-3.0;作者 FAQ 明确允许在自己软件中集成其**未修改的发布二进制**。
  本项目仅以子进程方式调用 `console_solver.exe`,不包含、不修改其源码。
- 下载地址:
  https://github.com/bupticybee/TexasSolver/releases/download/v0.2.0/TexasSolver-v0.2.0-Windows.zip

用法: `console_solver.exe -i <配置文件>`,求解完成后在当前目录写出 `output_result.json`。
样例配置见 `gto/sample_config.txt`。

# 酒馆动物肖像来源

本目录十五张 PNG 是 2026-08-01 为《酒馆德州》使用 OpenAI ImageGen
按项目现有美术方向生成的原创资产，不来自第三方牌组或素材站。

统一方向：写实手绘数字插画、地下酒馆、暗褐背景、琥珀轮廓光、正面或
四分之三胸像、适合椭圆头像裁切；每个身份通过物种、服饰、神态和局部色彩
区分。生成源图随后以 pygame-ce 的高质量缩放压为 512×512 PNG，
初始九张运行资产合计约 3.2 MiB；后续六张沿用完全相同的构图、光照与压缩流程。

文件与身份映射：

- `bull.png`：公牛 Toar
- `fox.png`：狐狸 Foxy
- `rhino.png`：犀牛 Gerk
- `boar.png`：屠夫猪 Bristle
- `dog.png`：看门狗 Scubby
- `cat.png`：流浪猫 Stray
- `raven.png`：渡鸦 Corvin
- `rabbit.png`：兔子 Mallow
- `wolf.png`：灰狼 Varg
- `bear.png`：棕熊 Borin
- `lion.png`：雄狮 Aurelio
- `tiger.png`：猛虎 Raka
- `turtle.png`：老龟 Moss
- `owl.png`：夜枭 Orin
- `panther.png`：黑豹 Nyx

运行时通过 `ui.characters.Bust` 与 `ui.respath.res_path()` 加载；文件缺失或
损坏时自动退回原程序化动物胸像。

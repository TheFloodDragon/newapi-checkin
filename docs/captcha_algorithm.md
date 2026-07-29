# randomtool.cn 图形验证码生成算法（录制记录）

`captcha_ocr/` 的本地复刻依据。所有参数都是在浏览器里 hook
`CanvasRenderingContext2D` 录得的实测值，不是推测。站点改版后按本文重新录制，
再更新 `captcha_ocr/generator.py` 的常量并重建模板库。

页面：<https://randomtool.cn/random-captcha/>（纯前端生成，无后端接口）

## 一、绘制参数

| 项 | 实测值 |
|---|---|
| 画布 | 300×100，白底 `#ffffff`（`fillRect(0,0,300,100)`） |
| 字体 | `bold 40px Arial`（唯一字体，无变体） |
| textAlign | `start` |
| **textBaseline** | **`middle`**（非 canvas 默认的 `alphabetic`） |
| 字符变换 | `save → translate(x, 50) → rotate(θ) → fillText(ch, 0, 0) → restore` |
| θ 范围 | ±0.35 rad，均匀分布，每字符独立 |
| 字符 x | `max(20, (300 - 35n) // 2) + 35i` |
| 字符颜色 | RGB 各分量 ∈ [0x30, 0x95]（深色，逐字符随机） |
| 噪点 | 50 个 `arc(x, y, 1, 0, 2π)` + `fill()`，颜色 ∈ [0x96, 0xdb] |
| 干扰线 | 7 条 `moveTo/lineTo/stroke`，`lineWidth = 1`，同浅色区间 |
| 字符集 | number=10 / alpha=47 / mixed=55 / complex=63 |
| 长度 | 滑块 4–12，默认 6 |

**颜色分层是整条识别管线的前提**：字符色区间与干扰色区间不重叠，
所以单一灰度阈值（`<= 0x95`）即可干净剥离全部干扰。

## 二、两个容易踩的坑

### 1. textBaseline = "middle"，锚点到基线偏移 = 11px

`translate` 的 y=50 是字形垂直中心，**不是基线**。基线在其下方 11px。

这个值必须实测，不能用公式推。`(fontBoundingBoxAscent - fontBoundingBoxDescent)/2`
会算出 14，比实际大 3px。实测脚本：

```js
// 在页面 console 执行
const c = document.createElement('canvas'); c.width = 200; c.height = 200;
const g = c.getContext('2d');
const measure = (baseline) => {
  g.fillStyle = '#fff'; g.fillRect(0, 0, 200, 200);
  g.font = 'bold 40px Arial'; g.textBaseline = baseline; g.fillStyle = '#000';
  g.fillText('H', 40, 100);
  const d = g.getImageData(0, 0, 200, 200).data;
  let t = 1e9, b = -1;
  for (let y = 0; y < 200; y++) for (let x = 0; x < 200; x++)
    if (d[(y * 200 + x) * 4] < 128) { if (y < t) t = y; if (y > b) b = y; }
  return { top: t, bottom: b };
};
const alpha = measure('alphabetic'), mid = measure('middle');
console.log('middle→baseline 偏移 =', mid.bottom - alpha.bottom);  // 11
```

漏掉这个偏移的代价（实测，同一套模板库）：

| 偏移取值 | 真实样本单字符准确率 |
|---|---|
| 0（按基线锚点绘制） | 4.55% |
| 14（按公式推导） | 93.94% |
| **11（实测）** | **100%** |

失败模式很有辨识度：偏移过大时预测结果几乎全是带下伸部的 `g/p/j/y`。

### 2. 旋转中心是 translate 点，不是基线点

`rotate` 发生在 `translate` 之后、`fillText` 之前，所以旋转中心是 (x, 50)，
与基线点相差 11px。用错会让字形整体绕偏。

## 三、字符 x 坐标与站点自身的 bug

实测录制值：

| n | 首字符 x | 末字符 x |
|---|---|---|
| 4 | 80 | 185 |
| 5 | 62 | 202 |
| 6 | 45 | 220 |
| 8 | 20 | 265 |
| 10 | 20 | 335 |
| 12 | 20 | 405 |

公式 `start = max(20, (300 - 35n) // 2)`，步长恒为 35。

`start` 的 20px 下限导致 **n > 8 时末尾字符 x 超出 300px 画布**（n=12 时达 405），
这些字符根本画不出来。这是站点自己的缺陷，复刻时如实保留 —— 实用长度上限是 8。

## 四、字形度量交叉验证

`canvas_glyph_metrics.json` 记录 63 个字符在真实 Canvas 上的字形包围盒
（宽高、相对基线偏移、前景像素数），用于验证本地 PIL 渲染是否与浏览器一致。

对比结论（PIL Arial Bold 40px vs Canvas）：宽高偏差 ≤2px，基线偏移偏差 ≤1px，
前景像素数平均差 6.4%。差异来自抗锯齿实现，不影响识别。

重新生成该文件的脚本见文件内 `_script` 字段。

## 五、样本采集

页面一次生成 12 条文本，但只渲染 1 个 Canvas 预览，所以要循环触发重绘。
标签取自 hook `fillText` 的实参 —— 比从 DOM 文本列表里猜哪条对应当前 Canvas 可靠。

采集脚本见 `captcha_ocr/collect.py` 的 `EXTRACT_JS`，参数由
`collect.extract_js_args()` 生成（阈值与画布尺寸由 Python 侧统一给出，
避免两边漂移）。导出格式是 300×100 bitset 的 base64，全部预处理仍在 Python 侧完成。

---

# New API 签到图形验证码（jianzhile 系 fork）

与上面 randomtool.cn 那套**完全无关**，是另一个生成器。识别实现见
`captcha_ocr/newapi_bitmap.py`，接线见 `providers/profiles/newapi.py`。

## 一、接口流程（纯 HTTP，无需浏览器）

```
GET  /api/user/checkin?month=YYYY-MM   → data.captcha_enabled 标记是否需要验证码
POST /api/user/checkin/captcha         → {captcha_id, captcha_image(dataURL), expires_at}
POST /api/user/checkin                 → body {captcha_id, captcha_answer}
```

请求需带 `New-Api-User: <用户 id>` 头，否则一律 401。

实测三种失败回执（HTTP 均为 200，靠 `success=false` + `message` 区分）：

| 情况 | message |
|---|---|
| 未带验证码字段 | `请输入验证码` |
| 答案错误 | `验证码错误，请重试` |
| captcha_id 复用 | `验证码已失效，请刷新后重试` |

**captcha_id 单次有效**，所以每次重试都必须重新取图。

## 二、图像结构

| 项 | 实测值 |
|---|---|
| 画布 | 160×58 |
| 字符数 | 固定 5 |
| 调色板 | 全图仅 8 色，**无抗锯齿** |
| 背景 | `#f8fafc` |
| 干扰 | `#8ea4c5` 折线 + `#aac5ed` 散点 |
| 字符色 | 从左到右固定：`#111827` `#1d4ed8` `#047857` `#b45309` `#be123c` |
| 字体 | 6×9 点阵（部分 6×10、5×9），整数放大 2 倍 |
| 变形 | 无旋转、无缩放，仅 ±4px 位置抖动 |
| 噪声 | 随机抹掉单个像素（**只删不增**，535 个字形无一例外） |
| 字符集 | 32 个：数字 2-9 + 大写 A-Z 去 I/O |

## 三、三个可利用的确定性结构

1. **颜色即分割**：每字符一个固定深色，取色即分割。不需要投影切分或连通域
   分析，也完全不受干扰线跨字符影响。
2. **2×2 块 OR 降采样可彻底修复噪声**：字形是 2 倍整数放大，原图上每个 2×2
   块要么全亮要么全暗；噪声只抹单个像素，所以只要每块还剩 1 个像素，OR 降采样
   就 100% 还原点阵原貌。这不是「缓解噪声」，是消除。
3. **还原后是精确查表**：与 32 个字模逐位比对即可。

## 四、字模存在子集关系，必须用非对称代价

实测 6 对「小字模是大字模子集」：`8⊂B`、`S⊂8`、`S⊂B`、`C⊂Q`、`F⊂E`、`P⊂R`。

因为噪声只删不增，这类对无法靠「零多余像素」单独区分，靠代价的非对称性解决：

- 小字实例：对小/大字模都是 extra=0，用「缺失更少」决胜 → 判为小字
- 大字实例：对小字模 extra>0 直接排除 → 判为大字

残余风险：大字的差异块若 4 个像素全被抹掉，会真的退化成小字。按实测缺失率
（每像素约 5%~15%）估算，8 个差异块同时全灭的概率可忽略。

## 五、验证结果

- 107 张站点真实验证码：全部 5 位均零多余像素命中且无并列候选
- 其中 26 张人工标注（130 字符）：单字符 100%，整图 100%
  （标注过程中我把 `8` 误读成 `B` 一次，由位图逐位比对纠正——**真值以位图为准，不以肉眼为准**）
- 识别延迟约 0.6 ms/张（不含 PNG 解码）
- jianzhile.vip 真实签到：一次通过，`quota_awarded=5000000`

## 六、为什么不复用 randomtool 的模板

实测把 randomtool 的 Arial Bold 模板用于这套验证码：

| 归一化方式 | 单字符准确率 |
|---|---|
| 直接用现有 templates.npz | 5.0% |
| 同字体重渲 + 包围盒等比归一化 | 58.3% |

58% 单字符 ≈ 7% 整图，不可用。根因是字体不同（Arial 轮廓字体 vs 6×9 点阵），
不是参数没调好。字模表必须来自目标站点自己的字体。

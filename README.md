# 开源等时圈绘制工具

输入一个起点和研究范围，自动计算「从起点出发（或到达起点）开车需要多久能到达范围内各处」，
并生成一张可交互的等时圈地图（按 5 分钟一档分层着色）。**全程纯开源 Python，无需 ArcGIS Pro**，
算路使用百度批量算路 API（含真实路况）。

本工具的实现思路参考自 Keldos 的博客
[《绘制一个真实的交通等时圈》](https://blog.keldos.me/2025/11/isochrone-analysis/)，
原文使用 ArcGIS Pro 完成，本项目用开源栈（geopandas / shapely / scipy / folium）复刻其全流程，
并在算路部分沿用原文的百度批量算路 API。

## 安装

需要 Python 3.10+。在项目目录下：

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## 准备百度 AK

算路依赖百度地图开放平台的服务，需要一个服务端 AK：

1. 打开 https://lbsyun.baidu.com/apiconsole/key
2. 创建应用，应用类型选 **「服务端」**，获得 AK
3. 复制 `.env.example` 为 `.env`，填入 `BAIDU_AK=你的AK`

> 配额：批量算路默认 5000 次/天；本工具每批一次请求算 50 个点，范围大时也很省。
> 频率：免费 AK 约每 3 秒 1 次请求；若日志频繁出现 `百度并发超限 status=401`，
> 用 `--delay 5` 调大间隔即可。

## 坐标怎么填

所有坐标都用**百度坐标系 BD-09**，格式为 `[经度, 纬度]`（先经度后纬度），和百度坐标拾取器一致：

- 获取坐标：打开 https://api.map.baidu.com/lbsapi/getpoint/index.html ，在地图上点目标点，
  左下角显示的 `经度,纬度` 用百度坐标拾取器取的点本就是 BD-09，**无需任何换算**，直接填进 config 的 `origin` / `polygon` 即可。
- 多边形研究区：按顺序给出一圈顶点 `[[经度,纬度], ...]`，首尾可重复（自动闭合）。

### 坐标系与换算说明（务必读完）

本工具统一使用 **百度坐标系 BD-09**。不同来源的坐标系不一样，填错会整体偏移几百米到 1 公里：

- **高德 / 腾讯地图** = GCJ-02；**GPS / OSM** = WGS84。这两类坐标**必须先转成 BD-09** 再填进 config。
- ⚠️ 切勿把 BD-09 当作 WGS84 再转一次（双重偏移），那样会错得更离谱。
- **选点器（picker.py）产出的坐标已自动从 OSM(WGS84) 转成 BD-09**，可直接使用，
  **无需手动换算**（仅限选点器产出的坐标）。
- **百度坐标拾取器**取到的点本就是 BD-09，**无需任何换算**，直接填即可。

## 快速开始

```bash
# 用示例配置跑（不需要 AK，用模拟时间验证出图效果）
python isochrone.py --config config.example.json --demo --out demo.html

# 接百度真实路况正式出图
python isochrone.py --config config.example.json --out isochrone.html
```

打开生成的 `isochrone.html` 即可看到等时圈。

## 多任务管理（按子目录组织，推荐）

每个等时圈任务建议放在**自己的子目录**里，目录里放一份 `config.json`，
直接在该目录（或任意位置用 `--config` 指向它）运行即可：

```bash
# 在任务子目录内运行（产物自动落在同目录）
cd tasks/hangzhou_westlake
python ../../isochrone.py --config config.json

# 或从项目根目录统一调度，效果完全相同（产物仍落在 config 同目录）
python isochrone.py --config tasks/hangzhou_westlake/config.json
```

**产物位置规则**：`--out`（默认 `isochrone.html`）和 `--csv`（默认 `durations.csv`）
若不显式指定，会自动放到 **config 文件所在的目录**，不会污染别处。
所以「一个子目录 = 一个任务」，各任务的地图与缓存互不干扰。

**同目录为空** → 从头开始算路；**同目录已存在 `durations.csv`** → 先校验参数指纹：

- **指纹一致**：自动**断点续跑**，跳过已算的点、只补算剩余，不重复消耗配额。
- **指纹不一致**（origin / 研究区 / 网格 / 驾车策略中任一项变了）：
  会**交互询问**你是否确认覆盖旧数据并重新算路；回答 `y` 才继续，否则退出、不改动任何文件。
  非交互环境（如后台脚本）下默认中止。想跳过询问直接覆盖，加 `--force`（慎用）。

> 在根目录直接放一份 `config.json` 运行也没问题，只是不同任务若都用同名
> `config.json`/`durations.csv`，切换任务时会触发上面的指纹不一致提示，
> 提醒你数据将被覆盖——这正是不建议混放的原因。

## 从已有数据出图（不调百度，离线重绘）

算路结果会保存在 `durations.csv`（每批算完即落盘）。有了这份数据后，
**换配色 / 底图 / 分级间隔 / 方向重新出图，或复用他人分享的 csv，都不再消耗百度配额**，
直接用 `--from-csv` 读取即可：

```bash
# 用已存在的 durations.csv 直接出图（不需要百度 AK）
python isochrone.py --config config.example.json --from-csv --out isochrone.html

# 若 csv 中已存有 to 方向的数据（即之前用 --direction to 跑过，断点续跑会并存两种方向），
# 可离线重绘「到达该点」的等时圈
python isochrone.py --config config.example.json --from-csv --direction to --out to.html

# 指定数据源文件、改配色与分级
python isochrone.py --config config.example.json --from-csv \
    --csv my_durations.csv --cmap viridis --interval 10 --out other.html
```

注意事项：
- `--from-csv` **只是把 csv 里已算好的时长重新渲染成图，不会算返程**。csv 里存的是
  哪个方向的数据，就只能出哪个方向的图：`--direction to` 只会读取 csv 中 `to` 方向的行，
  若 csv 里根本没有 `to` 数据（例如只跑过默认 `from`），会直接报「没有任何有效采样点」。
  想要返程等时圈，得先用 `run_pipeline --direction to` 算一次把 `to` 数据写进 csv。
- csv 的 `oid` 与坐标的对应由「当前 polygon + cell_deg 确定性生成的渔网」决定，
  因此**必须用与生成 csv 时相同的 origin / polygon / cell_deg / tactics** 来出图，否则
  oid 错位、旧时长被错配到新坐标，得到错误但看起来正常的等时圈。工具写入 csv 时会
  一并保存参数指纹（含上述四项），续跑 / 离线出图时自动比对；指纹不一致会直接报错
  （可用 `--force` 强行继续，但不保证正确）。纯渲染参数（`grid_deg` / `interval` /
  `cmap` / `basemap` / `max_minutes`）不参与指纹，改这些不影响数据、可安全复用。

## 交互式选点器（picker.py，免手填坐标）

不想手动抄坐标时，用它在地图上直接画：

```bash
python picker.py                       # 默认杭州中心，生成 picker.html 并自动打开
python picker.py --center 30.25 120.21 # 指定地图初始中心 [纬度, 经度]
python picker.py --out my_picker.html  # 指定输出文件名
```

打开 `picker.html` 后：

1. 用左侧绘制工具画一个**多边形 / 矩形**（研究区），再放一个 **Marker**（起点）。
2. 画完后，页面**左下角文本框会自动填充**可直接粘进 config 的片段：
   - Marker → `"origin": [经度, 纬度]`
   - 多边形 → `"polygon": [[经度,纬度], ...]`
3. 坐标已由选点器自动从 OSM(WGS84) 转成百度 BD-09，与 config 格式一致，**无需手动换算**（仅限选点器产出的坐标）。
4. 文本框下方有 **复制配置** 按钮，一键复制到剪贴板粘进 config 即可。

> 圆形研究区无法在选点器里画，按下面「圆形研究区模式」在 config 里填 `radius_km` 即可，圆心取上面得到的 `origin`。

## 配置文件（config.json）字段说明

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `origin` | 中心坐标 `[经度, 纬度]` | 等时圈的中心点。 |
| `direction` | 等时圈方向 | `from`（默认）：算「从 origin 出发」的时间；`to`：算「到达 origin」的时间。 |
| `polygon` | 研究区多边形（**多边形模式**） | `[[经度,纬度],...]` 顶点列表，首尾可重复闭合。 |
| `aoi` | 研究区（**仅圆形模式**） | `{"type":"circle","radius_km":数字}`；多边形模式请用上面的 `polygon`。 |
| `cell_deg` | 采样密度（度） | 默认 `0.0028`≈100 米。越小越精细，但请求越多、越慢。 |
| `grid_deg` | 色面平滑度（度） | 默认 `0.0006`≈20 米。越小越平滑、文件越大。 |
| `interval` | 分级间隔（分钟） | 默认 `5`，即每 5 分钟换一档颜色。 |
| `max_minutes` | 颜色上限（分钟） | 默认 `null` 自动取最大值；可手动设（如 `30`）固定图例范围。 |
| `cmap` | 配色方案 | 默认 `YlOrRd`（浅黄→红，越远越久），可换 `viridis` 等。 |
| `basemap` | 底图样式 | 默认 `voyager`（浅底+地名/POI 标注）；`positron`（极简浅底）；`osm`（彩色完整）。 |
| `tactics` | 百度驾车策略 | 10/11/12/13，默认 11（含实时路况）。改动会使 durations.csv 指纹失效，触发整份重算。 |

除 `origin` / `polygon` / `aoi` 外，其余字段都有默认值，可只写需要改的。

命令行额外参数（`isochrone.py --help`）：
`--config`（JSON 配置文件，含 origin / aoi 或 polygon）、`--origin`（起点 `[经度 纬度]`，需配合 `--polygon`）、
`--polygon`（研究区多边形 JSON 文件）、`--direction`（覆盖 config 的 direction）、
`--from-csv`（从已有 durations.csv 直接出图，不调百度）、`--tactics`（百度驾车策略，默认 11 含路况）、
`--csv`（结果缓存文件）、`--out`（输出 HTML）、`--cell` / `--grid` / `--interval` / `--max-minutes` / `--cmap` / `--basemap`、
`--delay`（请求间隔秒，默认 3；频繁遇 401 并发超限可调大）、
`--force`（指纹校验失败时跳过确认、直接覆盖旧数据，慎用）、`--demo`（模拟数据）。
其中 `--out` / `--csv` 不指定时，默认落到 **config 文件所在目录**。

> 并发说明：当前为**串行 by design**，未开放 `--concurrency` 参数；免费 AK 即 1 QPS，
> 两次请求之间的间隔统一用 `--delay` 控制（频繁遇 401 并发超限可调大）。

> ⚠️ `--tactics` 策略提示：`13`=最短距离、忽略实时路况，背离本工具「含真实拥堵」核心卖点；无特殊需求请保持默认 `11`。

## 圆形研究区模式

不想画多边形时，以起点为圆心、给定半径生成圆形研究区：

```json
{
  "origin": [120.20979, 30.252714],
  "aoi": { "type": "circle", "radius_km": 2.5 },
  "interval": 5
}
```

```bash
python isochrone.py --config config.circle.example.json --out isochrone_circle.html
```

无 AK 想先看效果加 `--demo` 即可。

### 规模与时耗估算

圆形研究区的采样点数量近似 `π × (半径 / 格点间距)²`：`cell_deg` 越小、采样越密，点数越多、越慢。
下表以 `batch_size=50`、`delay=3s`、**串行** 估算（免费 AK 约 1 QPS）：

| 半径 | 约采样点数 | 批数(每批50) | 纯算路耗时(≈批数×delay) |
| 1 km | ~314   | 7   | ~21 s            |
| 2.5km| ~1963  | 40  | ~120 s           |
| 5 km | ~7854  | 158 | ~474 s(~8 min)   |

> 点数 ≈ π × (半径 / 格点间距)²，`cell_deg` 越小点数越多；若日志频繁出现 `百度并发超限 status=401`，
> 把 `--delay` 调大（如 `--delay 5`）即可缓解，此时耗时按「批数 × delay」线性增加。

## 等时圈方向：出发 vs 到达

默认 `direction="from"` 算「从 origin 出发」到各点的时间。
若想看「研究区内各处到达 origin 需要多久」（如配送、通勤到达某目的地），设 `"direction": "to"`：

```json
{
  "origin": [120.20979, 30.252714],
  "aoi": { "type": "circle", "radius_km": 2.5 },
  "direction": "to",
  "interval": 5
}
```

```bash
python isochrone.py --config config.circle.example.json --out isochrone_to.html
```

`to` 模式下地图中心标记为蓝色「终点」，图例标注「到达该点的时间」。
`from` 与 `to` 结果分别缓存、互不覆盖，都支持断点续跑。

## 输出文件

- `isochrone.html`：可交互等时圈地图（含中心标记、研究区、分级色面）。
- `durations.csv`：每个采样点的出行时间，可断点续跑、可二次分析。列含义：
  - `oid`：渔网采样点编号（由 polygon + cell_deg 确定性生成，与坐标一一对应）
  - `duration_min`：百度返回的驾车时间（分钟）
  - `direction`：方向，`from` / `to`
  - `origin_lng` / `origin_lat`：固定端点（config 的 `origin`）的**百度坐标**
  - `dest_lng` / `dest_lat`：该采样点的**百度坐标**
  - 用途：拿这两对百度坐标即可在百度地图/算路里核对返回时间是否正确（例如验证
    `(origin_lng,origin_lat)` → `(dest_lng,dest_lat)` 的驾车时长是否等于本行 `duration_min`）。
- `durations.meta.json`：参数指纹等元数据（oid→坐标对应的校验依据），一般无需手动查看。

## 地图样式

- **底图**：默认浅色 `voyager`（含小区/学校/公园等地名与 POI 标注）；想更干净可用 `positron`。
- **图例**：右下角自动生成颜色图例，标明「什么颜色 = 多少分钟」，与色面同一套分级。
- **配色**：默认 `YlOrRd`（浅黄=近、深红=远），可用 `--cmap` 或 config 的 `cmap` 更换。
- **断点续跑**：`durations.csv` 每批算完即落盘；中途退出或额度用尽后，再次运行同一配置会自动跳过已有点、只补算剩余，**不重复消耗配额**。

## 常见问题与排查（Troubleshooting / FAQ）

- **频繁 `百度并发超限 status=401`**：免费 AK 约 1 QPS。请先用修复后的版本（确保其支持 `--delay`），
  再调大间隔：`python isochrone.py --config xxx.json --delay 5`。若持续 401 不缓解，检查 AK 类型——
  必须是「**服务端**」类型；并确认当日配额(5000)未用尽、本机出口 IP 已在百度控制台的 **IP 白名单** 放行。
- **出图空白 / 采样点过少**：多半是百度算路失败（AK 类型不对、配额耗尽、IP 白名单未放行），
  看上方 `百度返回错误 status=...` 的 WARNING 日志定位；或先 `--demo` 验证几何/出图流程本身正常。
- **指纹不一致提示**：说明 origin / 研究区 / 网格 / 驾车策略中至少一项变了，旧时长已无法对应新坐标；
  确认无误后输入 `y` 覆盖重算，或加 `--force` 跳过确认。
- **`ModuleNotFoundError`**：venv 未激活或依赖未装。先 `.venv\Scripts\activate`（Windows），
  再 `pip install -r requirements.txt`。
- **无 AK 想先试跑**：加 `--demo`，用模拟数据验证出图效果（不需要百度 AK）。

## 参考与致谢

- 思路来源 / 原文博客：Keldos —
  [《绘制一个真实的交通等时圈》](https://blog.keldos.me/2025/11/isochrone-analysis/)
  （原文基于 ArcGIS Pro，本项目仅用开源栈复刻其流程，算路沿用其百度批量算路方案）。
- 算路服务：[百度地图开放平台 · 批量算路 API（routematrix）](https://lbsyun.baidu.com/index.php?title=webapi/route-matrix-api-v2)。
- 主要依赖：geopandas、shapely、scipy、folium、aiohttp、pandas、matplotlib。

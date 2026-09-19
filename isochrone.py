"""
isochrone.py — 开源等时圈绘制工具（无需 ArcGIS Pro）

替代原文中 arcpy 负责的每一步：
  - CreateFishnet + Clip  -> make_fishnet()         (shapely/geopandas)
  - 写入 time 属性        -> GeoDataFrame 直接操作
  - arcpy.sa.Idw 插值     -> idw_grid()             (scipy cKDTree + 反距离权重)
  - Clip 裁剪             -> mask_by_polygon()      (shapely contains_xy，支持带洞多边形)
  - 分级配色出图          -> render_map()           (folium 可交互 HTML 地图)
  - 分级面矢量导出        -> export_isochrone_geojson() (contourpy，可选)

算路部分沿用原文的「百度批量算路 API」(routematrix/v2)，含真实路况，与 ArcGIS 无关。

坐标约定（用户侧）：所有经纬度统一用百度坐标系 BD-09，格式为 [经度, 纬度]，
                    与百度坐标拾取器完全一致；内部统一转换为 WGS84 做几何与渲染。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import webbrowser
from pathlib import Path

import aiohttp
import contourpy
import folium
import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from scipy.spatial import cKDTree
from shapely import contains_xy
from shapely.geometry import Point, Polygon, mapping
from tqdm import tqdm

matplotlib.use("Agg")  # 无界面环境安全
import matplotlib.colors as mcolors  # noqa: E402

logger = logging.getLogger("isochrone")

BAIDU_URL = "https://api.map.baidu.com/routematrix/v2/driving"

# 底图瓦片源（直接指定 URL，避免依赖 folium 内置名称匹配）
_TILES = {
    "positron": (
        "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "© OpenStreetMap contributors © CARTO",
        "CartoDB Positron（浅色极简）",
    ),
    "voyager": (
        "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        "© OpenStreetMap contributors © CARTO",
        "CartoDB Voyager（浅色+地名/POI 标注）",
    ),
    "osm": (
        "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "© OpenStreetMap contributors",
        "OpenStreetMap（彩色完整）",
    ),
}

# 百度算路持续失败时的排查提示（连续失败熔断与零有效点告警共用一套文案）
_AK_TROUBLESHOOT = (
    "  1) BAIDU_AK 不是『服务端(Server)』类型（浏览器端 AK 调服务端接口会被拒）；\n"
    "  2) AK 配额(每日5000)已用尽或 IP 白名单未放行本机出口 IP；\n"
    "  3) 起点/研究区坐标不在百度服务范围（需在中国境内）。"
)


# --------------------------------------------------------------------------- #
# 密钥
# --------------------------------------------------------------------------- #
def load_ak() -> str:
    """从 .env 或环境变量读取百度服务端 AK。"""
    load_dotenv()
    ak = os.getenv("BAIDU_AK")
    if not ak:
        raise RuntimeError(
            "未找到 BAIDU_AK：请在 .env 文件或环境变量中设置百度服务端 AK。"
        )
    return ak


def _config_fingerprint(origin, polygon, cell_deg, tactics):
    """参数指纹（12 位 hex），用于校验 durations.csv 是否与当前配置同源。

    参与指纹的要素（决定 oid→坐标→时长 三者对应关系的全部参数）：
      - origin   : 百度算路的固定端点（起点/终点），变了时长含义不同
      - polygon  : 研究区几何，决定渔网 oid→坐标 的对应关系
      - cell_deg : 渔网分辨率，决定 oid 总数与排列顺序
      - tactics  : 百度驾车策略，变了同一对端点的时长不同
    以下参数「不参与」指纹（它们不改变 csv 中存储的时长数据，只影响渲染）：
      - grid_deg / interval / cmap / basemap / max_minutes
        （这些只是出图样式，复用同一 csv 安全，只是画面不同）
      - direction：作为 csv 的一列分别保存，不算在"文件级"指纹里
    csv 本身只存 oid→时长、不含经纬度，oid 与坐标的对应完全由
    make_fishnet(polygon, cell_deg) 的确定性算法决定；一旦上述参数变了，
    oid 顺序就错位，旧时长会被悄悄错配到新坐标。指纹在写入时一并保存，
    续跑 / 离线出图时比对，避免静默产出错误等时圈。
    """
    h = hashlib.md5()
    if origin is not None:
        # 用 WGS84 坐标（几何/算路的实际空间），6 位小数足够且稳定
        h.update(f"origin={origin[0]:.6f},{origin[1]:.6f}".encode("utf-8"))
    if polygon is not None:
        h.update(polygon.wkt.encode("utf-8"))
    h.update(f"|cell={cell_deg}".encode("utf-8"))
    h.update(f"|tactics={tactics}".encode("utf-8"))
    return h.hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 1) 渔网生成（替代 arcpy CreateFishnet + Clip）
# --------------------------------------------------------------------------- #
def make_fishnet(polygon: Polygon, cell_deg: float = 0.0028, offset: float = 0.001):
    """
    在研究区多边形内生成渔网中心点。
    cell_deg 默认 0.0028（约杭州纬度下 100 米），与原文一致。
    返回 GeoDataFrame，含 oid / lat / lon / geometry(Point(lon,lat))。
    """
    minx, miny, maxx, maxy = polygon.bounds  # x=lon, y=lat
    minx -= offset
    miny -= offset
    maxx += offset
    maxy += offset
    cols = max(1, int(math.ceil((maxx - minx) / cell_deg)))
    rows = max(1, int(math.ceil((maxy - miny) / cell_deg)))

    centers = []  # (lat, lon)
    for i in range(cols):
        for j in range(rows):
            x0 = minx + i * cell_deg
            y0 = miny + j * cell_deg
            cx = x0 + cell_deg / 2.0
            cy = y0 + cell_deg / 2.0
            if polygon.contains(Point(cx, cy)):
                centers.append((cy, cx))

    gdf = gpd.GeoDataFrame(
        {"oid": range(len(centers))},
        geometry=[Point(lon, lat) for lat, lon in centers],
        crs="EPSG:4326",
    )
    gdf["lat"] = [c[0] for c in centers]
    gdf["lon"] = [c[1] for c in centers]
    logger.info("渔网生成完成：%d 个采样点", len(gdf))
    return gdf


# --------------------------------------------------------------------------- #
# 2) 百度批量算路（替代原文 asyncio 请求段，支持断点续跑）
# --------------------------------------------------------------------------- #
def _confirm_or_exit(prompt: str, force: bool, cancel_msg: str) -> None:
    """危险操作（指纹不匹配时覆盖数据 / 错配出图）的统一确认入口。

    force=True 跳过询问直接放行；交互回答 y/yes/是 放行；其余（含非交互
    环境的 EOFError）视为取消，抛 SystemExit(cancel_msg)。
    """
    if force:
        return
    try:
        ans = input(prompt)
    except EOFError:
        ans = ""
    if ans.strip().lower() not in ("y", "yes", "是"):
        raise SystemExit(cancel_msg)


def _atomic_write_csv(csv_p: Path, rows: list[dict]) -> None:
    """csv 增量落盘：写临时文件后 os.replace 原子替换，进程中断不会留下半截文件。

    rows 为空时不写（避免把已有好数据覆盖成空文件）。
    """
    if not rows:
        return
    tmp = csv_p.with_name(csv_p.name + ".tmp")
    pd.DataFrame(rows).to_csv(tmp, index=False)
    os.replace(tmp, csv_p)


def _atomic_write_meta(csv_p: Path, meta: dict) -> None:
    tmp = csv_p.with_suffix(".meta.json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, csv_p.with_suffix(".meta.json"))


async def _fetch_batch(session, sem, origin, dests, ak, tactics, direction="from"):
    """一次请求算 origin <-> 多个 dest 的驾驶时间（批量，省配额）。坐标统一 WGS84。

    direction="from"：origin 作为唯一起点，dests 作为多个终点（默认）——算「从原点出发」。
    direction="to"  ：origin 作为唯一终点，dests 作为多个起点——算「到达原点」。

    注意：百度 routematrix 的多个起点/终点之间用竖线 '|' 分隔（不是分号），
    错误使用 ';' 会导致百度只解析第一个点、返回结果数≠请求数（status 仍为 0）。
    """
    # 百度要求多个点用 '|' 分隔；单点格式为 "纬度,经度"
    dest_str = "|".join(f"{lat},{lon}" for lat, lon in dests)
    if direction == "to":
        # origin 视为唯一终点，dests 视为多个起点
        origins_param = dest_str
        destinations_param = f"{origin[0]},{origin[1]}"
    else:
        # 默认：origin 为唯一起点，dests 为多个终点
        origins_param = f"{origin[0]},{origin[1]}"
        destinations_param = dest_str
    params = {
        "output": "json",
        "origins": origins_param,
        "destinations": destinations_param,
        "tactics": tactics,
        "coord_type": "wgs84",
        "ak": ak,
    }
    async with sem:
        try:
            async with session.get(
                BAIDU_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
        except Exception as e:  # 网络异常：整批返回 None，稍后重试
            logger.warning("请求异常: %s", e)
            return [None] * len(dests), -1, str(e)

    if data.get("status") != 0:
        status = data.get("status")
        msg = data.get("message", "")
        if "并发" in str(msg):
            # 瞬时并发/频率超限（如 401「当前并发量已经超过约定并发配额」）：
            # 非额度耗尽，退避后重试即可，不影响最终结果。
            logger.warning(
                "百度并发超限 status=%s：%s（瞬时频率超限，将退避后重试）",
                status, msg,
            )
        else:
            # 真错误：AK 类型不对 / 当日配额用尽 / IP 白名单未放行等，需人工排查。
            logger.warning(
                "百度返回错误 status=%s msg=%s（非并发类：检查 AK 类型/配额/IP白名单）",
                status, msg,
            )
        return [None] * len(dests), status, msg

    result = data.get("result", [])
    # 关键防护：返回条数 != 请求目的点数时，位置映射不可信，
    # 整批标记失败（返回全 None），绝不按末尾补 None 后写盘，避免 oid↔时长错位。
    if len(result) != len(dests):
        logger.error(
            "百度返回结果数 %d != 请求目的点数 %d，疑似中间缺失，"
            "整批标记失败，跳过写盘以避免错位（status=%s）",
            len(result), len(dests), data.get("status"),
        )
        return [None] * len(dests), -2, "result_count_mismatch"
    out = []
    for r in result:
        try:
            out.append(r["duration"]["value"] / 60.0)  # 秒 -> 分钟
        except Exception:
            out.append(None)
    return out, 0, ""


async def batch_route(
    origin,
    gdf,
    ak,
    tactics: int = 11,
    batch_size: int = 50,
    concurrency: int = 1,
    delay: float = 3.0,
    csv_path: str = "durations.csv",
    max_retry: int = 3,
    direction: str = "from",
    polygon=None,
    cell_deg: float = 0.0028,
    origin_bd=None,
    force: bool = False,
    max_failed_batches: int = 3,
):
    """
    异步批量调用百度 routematrix，返回 {oid: duration_min}。
    - 支持断点续跑：已存在于 csv 的点不会重复请求。
    - 每批 batch_size 个终点一次请求（远少于原文的 1 点 1 请求，省配额）。
    - tactics 默认 11（常规路线，考虑实时路况）；注意 13=距离较短(不考虑路况)，
      本项目刻意不用 13，以保证等时圈反映真实拥堵。
    - 即时原子落盘 csv（写临时文件后 os.replace），失败可续。并发默认 1、
      间隔默认 3.0s，对齐百度免费版 1/3 QPS（每约 3 秒 1 次）；若 AK 配额更高
      可下调 delay，频繁出现 401 并发超限则应上调。
    - 连续失败熔断：连续 max_failed_batches 批「整批全失败」（每批已重试
      max_retry 次）则中止并给出排查建议——AK 配置错误时不必空跑完全部批次；
      已落盘数据不受影响，修复后重跑同一命令即可续算。
    - direction="from"（默认）：算「从 origin 出发」；direction="to"：算「到达 origin」。
      二者在百度驾车模型下结果不同（受单行/转向限制），故 csv 缓存按 direction 分别
      存储，方向切换不会互相覆盖或误用。
    - 写入的 csv 额外带每行两端点的百度坐标（origin_lng/lat, dest_lng/lat），
      方便核对百度返回数据的正确性。
    - force=True 时，指纹不匹配不再交互询问，直接覆盖旧数据重新算路。
    """
    done: dict[int, float] = {}
    # all_done 跨方向保存，确保不同 direction 的缓存互不覆盖
    all_done: dict[tuple[int, str], float] = {}
    csv_p = Path(csv_path)
    if csv_p.exists() and polygon is not None:
        # 校验参数指纹：防止用错配的 origin/polygon/cell_deg/tactics 续跑（oid 错位）
        fp = _config_fingerprint(origin, polygon, cell_deg, tactics)
        meta_p = csv_p.with_suffix(".meta.json")
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            if meta.get("fingerprint") != fp:
                _confirm_or_exit(
                    f"\n[确认] 已有 {csv_p.name} 的参数指纹与当前配置不一致"
                    f"（旧={meta.get('fingerprint')} 当前={fp}）。\n"
                    f"  说明：origin / 研究区 / 网格 / 驾车策略 中至少有一项变了，"
                    f"旧时长已无法对应到正确坐标。\n"
                    f"  继续将【覆盖】该 csv 并重新算路（旧数据不可恢复）。确认覆盖？(y/N): ",
                    force=force,
                    cancel_msg="已取消运行：未修改任何数据。用 --force 可跳过确认直接覆盖。",
                )
                logger.warning("指纹不匹配，按指示覆盖旧数据并重新算路。")
                csv_p.unlink(missing_ok=True)
                meta_p.unlink(missing_ok=True)
        else:
            logger.warning(
                "durations.csv 无指纹信息（旧格式），无法校验参数是否一致；"
                "若期间改过 origin/研究区/网格/策略，oid 可能已错配。"
            )
    if csv_p.exists():
        df = pd.read_csv(csv_p)
        has_dir = "direction" in df.columns
        for _, row in df.iterrows():
            if pd.notna(row.get("duration_min")):
                rd = row["direction"] if has_dir else "from"
                oid = int(row["oid"])
                all_done[(oid, rd)] = float(row["duration_min"])
                if rd == direction:
                    done[oid] = float(row["duration_min"])

    # 预计算每个网格点的百度坐标（供 csv 记录，方便核对百度返回数据）
    grid_bd = {}
    for r in gdf.itertuples():
        bd_lat, bd_lng = wgs84_to_bd09(r.lat, r.lon)  # 返回 (bd_lat, bd_lng)
        grid_bd[int(r.oid)] = (round(bd_lng, 6), round(bd_lat, 6))

    # 参数指纹元数据：内容在整个算路过程中不变，先算好、每批随 csv 一并落盘
    meta_out = None
    if polygon is not None:
        meta_out = {
            "fingerprint": _config_fingerprint(origin, polygon, cell_deg, tactics),
            "cell_deg": cell_deg,
            "tactics": tactics,
            "origin_bd": list(origin_bd) if origin_bd else None,
            "polygon_wkt": polygon.wkt,
        }

    pending = [
        (int(r.oid), (r.lat, r.lon))
        for r in gdf.itertuples()
        if int(r.oid) not in done
    ]
    logger.info("方向=%s：已有 %d 个点结果，待计算 %d 个", direction, len(done), len(pending))
    if not pending:
        return done

    chunks = [pending[s : s + batch_size] for s in range(0, len(pending), batch_size)]
    failed_streak = 0
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        bar = tqdm(total=len(chunks), desc=f"批量算路({direction})", unit="批")
        for i_chunk, chunk in enumerate(chunks):
            oids = [o for o, _ in chunk]
            dests = [d for _, d in chunk]
            durations = None
            last_status = 0
            for _ in range(max_retry):
                durations, last_status, _ = await _fetch_batch(
                    session, sem, origin, dests, ak, tactics, direction
                )
                if all(d is not None for d in durations):
                    break
                # 并发类错误(401)退避更久，其余 1 秒后重试
                await asyncio.sleep(5.0 if last_status == 401 else 1.0)
            # 「整批全失败」才计入熔断：部分缺失只丢个别点，不阻断整体
            if all(d is None for d in durations):
                failed_streak += 1
            else:
                failed_streak = 0
            for oid, dur in zip(oids, durations):
                if dur is not None:
                    done[oid] = round(dur, 1)
                    all_done[(oid, direction)] = round(dur, 1)
            # 增量原子落盘（保留其它方向的缓存），并记录两端百度坐标便于核对
            rows = []
            for (k, d), v in sorted(all_done.items()):
                glng, glat = grid_bd.get(k, (None, None))
                rows.append({
                    "oid": k,
                    "duration_min": v,
                    "direction": d,
                    "origin_lng": origin_bd[0] if origin_bd else None,
                    "origin_lat": origin_bd[1] if origin_bd else None,
                    "dest_lng": glng,
                    "dest_lat": glat,
                })
            _atomic_write_csv(csv_p, rows)
            if meta_out is not None:
                _atomic_write_meta(csv_p, meta_out)
            bar.update(1)
            bar.set_postfix(有效点=len(done))
            if failed_streak >= max_failed_batches:
                bar.close()
                raise RuntimeError(
                    f"连续 {failed_streak} 批算路整批失败（每批已重试 {max_retry} 次），已熔断中止。\n"
                    f"  当前累计有效采样点 {len(done)} 个；已算好的数据仍保存在 {csv_p.name}，"
                    f"排查并修复后重跑同一命令即可断点续算。\n"
                    f"  常见原因（按上方 '百度返回错误 status=...' 的 WARNING 日志定位）：\n"
                    f"{_AK_TROUBLESHOOT}"
                )
            if i_chunk < len(chunks) - 1:  # 最后一批之后不必再等
                await asyncio.sleep(delay)
        bar.close()
    return done


# --------------------------------------------------------------------------- #
# 3) IDW 插值（替代 arcpy.sa.Idw）
# --------------------------------------------------------------------------- #
def idw_grid(gdf, grid_deg: float = 0.0006, power: float = 2.0, k: int = 12):
    """
    用 K 近邻反距离权重插值成规则栅格（约原文 power=2, VARIABLE 12）。
    返回 XI, YI, zz（zz 为分钟，NaN 表示无数据）。
    """
    pts = np.column_stack([gdf.geometry.x.values, gdf.geometry.y.values])  # lon,lat
    z = gdf["duration_min"].values.astype(float)
    n = len(pts)
    # 近邻数不能超过有效点数，否则 cKDTree 会用 n 填充缺失下标导致越界
    if n < 3:
        logger.warning("有效采样点仅 %d 个，不足以做 IDW 插值，输出空栅格。", n)
        minx, miny, maxx, maxy = gdf.total_bounds
        nx = int((maxx - minx) / grid_deg) + 1
        ny = int((maxy - miny) / grid_deg) + 1
        XI, YI = np.meshgrid(
            np.linspace(minx, maxx, max(nx, 1)), np.linspace(miny, maxy, max(ny, 1))
        )
        return XI, YI, np.full(XI.shape, np.nan)
    k_eff = min(k, n)

    minx, miny, maxx, maxy = gdf.total_bounds
    nx = int((maxx - minx) / grid_deg) + 1
    ny = int((maxy - miny) / grid_deg) + 1
    xi = np.linspace(minx, maxx, nx)
    yi = np.linspace(miny, maxy, ny)
    XI, YI = np.meshgrid(xi, yi)

    tree = cKDTree(pts)
    grid_flat = np.column_stack([XI.ravel(), YI.ravel()])
    d, idx = tree.query(grid_flat, k=k_eff)
    if k_eff == 1:  # query 返回 1D，统一成 2D 以便按列聚合
        d = d.reshape(-1, 1)
        idx = idx.reshape(-1, 1)
    d = np.maximum(d, 1e-9)
    w = 1.0 / d ** power
    zz = np.sum(w * z[idx], axis=1) / np.sum(w, axis=1)
    return XI, YI, zz.reshape(XI.shape)


def mask_by_polygon(XI, YI, zz, polygon):
    """用研究区多边形把栅格裁剪到区内（替代 arcpy Clip）。

    用 shapely 的向量化 contains_xy 做点判定：与 make_fishnet 的
    polygon.contains 同一语义，多边形带洞时洞内一并置为 NaN。
    """
    inside = contains_xy(polygon, XI.ravel(), YI.ravel()).reshape(XI.shape)
    return np.where(inside, zz, np.nan)


# --------------------------------------------------------------------------- #
# 4) 渲染可交互等时圈地图（替代 ArcGIS 符号系统分级配色）
# --------------------------------------------------------------------------- #
def render_map(
    origin,
    polygon,
    XI,
    YI,
    zz,
    out_html: str = "isochrone.html",
    interval: float = 5.0,
    cmap_name: str = "YlOrRd",
    max_minutes=None,
    basemap: str = "voyager",
    direction: str = "from",
):
    """
    在 folium 地图上叠加等时圈面（按 interval 分钟分级着色），并标出起点、研究区、
    右下角色块图例（颜色 ↔ 分钟）。底图可切换：
      - voyager  : CartoDB Voyager，浅底 + 地名/POI 标注（小区/学校/公园等），推荐
      - positron : CartoDB Positron，极简浅底、基本无 POI 标注
      - osm      : OpenStreetMap，彩色完整底图（含绿地/水域填色，可能抢色）
    默认配色 YlOrRd：短时长浅黄、长时长红，符合"越远越久"直觉（可改用 viridis 等）。
    叠加图像的地理范围取 XI/YI 的实际栅格范围（而非研究区 bounds）：有效采样点
    不足研究区边缘时二者不同，若错用研究区 bounds 会把整幅色面拉伸错位。
    打开地图时自动缩放（fit_bounds）到研究区范围，不再依赖固定 zoom。
    """
    # 栅格数组的真实地理范围（ImageOverlay 必须用同一范围映射，否则色面错位）
    minx, miny = float(XI.min()), float(YI.min())
    maxx, maxy = float(XI.max()), float(YI.max())
    center = [origin[0], origin[1]]

    url, attr, name = _TILES.get(basemap, _TILES["voyager"])
    m = folium.Map(location=center, tiles=None)
    folium.TileLayer(tiles=url, attr=attr, name=name, control=False).add_to(m)
    m.fit_bounds([[polygon.bounds[1], polygon.bounds[0]],
                  [polygon.bounds[3], polygon.bounds[2]]])

    valid = zz[~np.isnan(zz)]
    bands_meta = []  # (label, overlay_js_name, hex_color) 每个时间档一层，便于按档显隐
    if valid.size == 0:
        logger.warning("没有有效数据，仅输出底图。")
    else:
        vmax = max_minutes if max_minutes is not None else float(np.nanmax(zz))
        vmin = 0.0
        bounds = np.arange(vmin, vmax + interval, interval)
        try:
            cmap = matplotlib.colormaps[cmap_name]
        except KeyError:
            logger.warning("未知配色方案 %r，回退默认 YlOrRd", cmap_name)
            cmap = matplotlib.colormaps["YlOrRd"]
        norm = mcolors.BoundaryNorm(bounds, cmap.N)
        color = cmap(norm(zz))  # float rgba, 含 NaN 行
        for i in range(len(bounds) - 1):
            lo, hi = bounds[i], bounds[i + 1]
            if i == len(bounds) - 2:
                mask = (~np.isnan(zz)) & (zz >= lo)
                lab = f"≥ {lo:.0f} 分钟"
            else:
                mask = (~np.isnan(zz)) & (zz >= lo) & (zz < hi)
                lab = f"{lo:.0f}–{hi:.0f} 分钟"
            rgba = np.zeros((*zz.shape, 4), dtype=np.uint8)
            rgba[..., :3] = (color[..., :3] * 255).astype(np.uint8)
            rgba[..., 3] = np.where(mask, 204, 0).astype(np.uint8)  # α≈0.8，仅本档不透明
            ov = folium.raster_layers.ImageOverlay(
                image=rgba,
                bounds=[[miny, minx], [maxy, maxx]],
                origin="lower",
                opacity=1.0,
                name=lab,
                control=False,
            ).add_to(m)
            rep = lo + 1e-9  # 该档代表值，取色与插值同一套色表
            r, g, b, _ = cmap(norm(rep))
            hexc = "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
            bands_meta.append((lab, ov.get_name(), hexc))

        _add_legend(
            m,
            "到达该点的时间（分钟）" if direction == "to" else "从该点出发的时间（分钟）",
            bands_meta,
        )

    # 研究区轮廓
    folium.GeoJson(
        polygon.__geo_interface__,
        name="研究区",
        style_function=lambda f: {"color": "#333333", "weight": 2, "fill": False},
    ).add_to(m)

    # 中心标记：from=起点(红)，to=终点(蓝)
    if direction == "to":
        marker_label = "终点"
        marker_icon = folium.Icon(color="blue", icon="stop")
    else:
        marker_label = "起点"
        marker_icon = folium.Icon(color="red", icon="play")
    folium.Marker(
        location=center,
        popup=f"{marker_label} {origin[0]:.5f}, {origin[1]:.5f}",
        icon=marker_icon,
    ).add_to(m)

    m.save(out_html)
    logger.info("地图已保存: %s", out_html)
    return m


def _add_legend(m, title, bands):
    """右下角颜色图例 + 交互筛选：每个时间档一个复选框，可单独显隐；
    顶部『全选 / 取消全选』一键控制。取消全选后点选某一档，即只显示该档区域。

    bands: list of (label, overlay_js_name, hex_color)，每个时间档对应一个独立叠加层。
    通过 JS 直接 addTo / removeLayer 控制各层可见性，无需引入额外依赖。
    """
    if not bands:
        note = (
            '<div id="isochrone-legend" style="position:fixed;bottom:30px;right:18px;'
            'z-index:9999;background:rgba(255,255,255,0.94);border:1px solid #ccc;'
            'border-radius:6px;padding:9px 11px;font-family:Arial,Helvetica,sans-serif;'
            f'font-size:12px;color:#666;">{title}<br>（无有效数据）</div>'
        )
        m.get_root().html.add_child(folium.Element(note))
        return

    rows = []
    for idx, (label, ov_name, hexc) in enumerate(bands):
        rows.append(
            f'<tr><td style="padding:2px 5px 2px 0;vertical-align:middle;">'
            f'<input type="checkbox" checked onclick="isoSetBand({idx}, this.checked)"></td>'
            f'<td style="background:{hexc};width:16px;height:16px;vertical-align:middle;'
            f'border:1px solid #999;border-radius:3px;"></td>'
            f'<td style="padding-left:6px;font-size:12px;color:#222;white-space:nowrap;'
            f'vertical-align:middle;">{label}</td></tr>'
        )
    rows_html = "".join(rows)
    legend_html = (
        '<div id="isochrone-legend" style="position:fixed;bottom:30px;right:18px;'
        'z-index:9999;background:rgba(255,255,255,0.94);border:1px solid #ccc;'
        'border-radius:6px;padding:9px 11px;font-family:Arial,Helvetica,sans-serif;'
        'box-shadow:0 1px 4px rgba(0,0,0,0.25);max-height:72%;overflow:auto;">'
        f'<div style="font-weight:bold;margin-bottom:6px;font-size:13px;color:#222;">{title}</div>'
        '<div style="margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid #ddd;">'
        '<label style="font-size:12px;color:#222;cursor:pointer;">'
        '<input type="checkbox" checked onclick="isoSetAll(this.checked)"> '
        '<b>全选 / 取消全选</b></label></div>'
        f'<table style="border-collapse:separate;border-spacing:0 3px;">{rows_html}</table>'
        "</div>"
    )
    m.get_root().html.add_child(folium.Element(legend_html))

    # 注册各叠加层引用 + 显隐切换函数，供图例复选框调用。
    # 关键：folium 会把地图与所有叠加层的 `var xxx = L.imageOverlay(...)` 集中写在页面
    # 末尾的『地图总脚本』里，而本注入脚本在它之前执行。若此处直接用变量名引用
    # image_overlay_xxx，会因尚未声明而取到 undefined，导致筛选失效（只剩首档偶有反应）。
    # 故改为：先把图层变量【名】以字符串数组保存，等到 window.load（总脚本已执行、
    # 各 var 已成为 window 全局属性）后再用 window[name] 解析成真实 Leaflet 图层对象。
    names_js = "[" + ",".join(f"'{ov_name}'" for (_, ov_name, _) in bands) + "]"
    map_name = m.get_name()
    js = (
        # 切换函数定义为全局，点击即时可用（layers 未就绪时安全 no-op）
        "window.isoSetBand = function(i, on){\n"
        "  var iso = window.__iso;\n"
        "  if(!iso || !iso.layers[i]) return;\n"
        "  var b = iso.layers[i];\n"
        "  if(!b.layer) return;\n"
        "  if(on){ b.layer.addTo(iso.map); } else { iso.map.removeLayer(b.layer); }\n"
        "};\n"
        "window.isoSetAll = function(on){\n"
        "  var cbs = document.querySelectorAll('#isochrone-legend table input');\n"
        "  for(var i=0;i<cbs.length;i++){ cbs[i].checked = on; window.isoSetBand(i, on); }\n"
        "};\n"
        # 总脚本执行完后再收集各层真实引用
        "window.addEventListener('load', function(){\n"
        "  var mp = " + map_name + ";\n"
        "  var names = " + names_js + ";\n"
        "  window.__iso = { map: mp, layers: [] };\n"
        "  for(var i=0;i<names.length;i++){\n"
        "    var lyr = window[names[i]];\n"
        "    if(lyr){ window.__iso.layers.push({ layer: lyr, idx: i }); }\n"
        "  }\n"
        "});\n"
    )
    m.get_root().html.add_child(folium.Element(f"<script>{js}</script>"))


# --------------------------------------------------------------------------- #
# 流程组装
# --------------------------------------------------------------------------- #
def build_isochrone(
    origin,
    polygon,
    gdf=None,
    cell_deg: float = 0.0028,
    grid_deg: float = 0.0006,
    out_html: str = "isochrone.html",
    interval: float = 5.0,
    cmap_name: str = "YlOrRd",
    max_minutes=None,
    basemap: str = "voyager",
    direction: str = "from",
):
    """用已有含 duration_min 的 gdf 直接插值出图。"""
    if gdf is None:
        gdf = make_fishnet(polygon, cell_deg=cell_deg)
    XI, YI, zz = idw_grid(gdf, grid_deg=grid_deg)
    zz = mask_by_polygon(XI, YI, zz, polygon)
    render_map(origin, polygon, XI, YI, zz, out_html=out_html,
               interval=interval, cmap_name=cmap_name, max_minutes=max_minutes,
               basemap=basemap, direction=direction)
    return gdf


def run_pipeline(
    origin,
    polygon,
    baidu_ak=None,
    cell_deg: float = 0.0028,
    grid_deg: float = 0.0006,
    tactics: int = 11,
    csv_path: str = "durations.csv",
    out_html: str = "isochrone.html",
    interval: float = 5.0,
    cmap_name: str = "YlOrRd",
    max_minutes=None,
    basemap: str = "voyager",
    direction: str = "from",
    origin_bd=None,
    force: bool = False,
    delay: float = 3.0,
):
    """完整管线：渔网 -> 百度批量算路 -> 插值 -> 裁剪 -> 出图。"""
    ak = baidu_ak or load_ak()
    gdf_raw = make_fishnet(polygon, cell_deg=cell_deg)
    n_total = len(gdf_raw)
    done = asyncio.run(
        batch_route(origin, gdf_raw, ak, tactics=tactics,
                    csv_path=csv_path, direction=direction,
                    polygon=polygon, cell_deg=cell_deg,
                    origin_bd=origin_bd, force=force, delay=delay)
    )
    gdf = gdf_raw.copy()
    gdf["duration_min"] = gdf["oid"].map(done)
    gdf = gdf.dropna(subset=["duration_min"]).reset_index(drop=True)
    n_valid = len(gdf)
    logger.info("有效采样点 %d / %d（成功率 %.1f%%）",
                n_valid, n_total, 100.0 * n_valid / max(n_total, 1))
    if n_valid == 0:
        logger.error(
            "没有任何有效采样点！百度批量算路几乎全部失败，常见原因：\n"
            "  1) BAIDU_AK 不是『服务端(Server)』类型（浏览器端 AK 调服务端接口会被拒）；\n"
            "  2) AK 配额(每日5000)已用尽或 IP 白名单未放行本机出口 IP；\n"
            "  3) 起点/研究区坐标不在百度服务范围（需在中国境内）。\n"
            "请查看上方 '百度返回错误 status=...' 的 WARNING 日志定位具体原因。"
        )
    elif n_valid < 3:
        logger.warning(
            "有效采样点过少(%d)，等时圈将不可靠。请同样检查上述百度 AK 原因。", n_valid
        )
    build_isochrone(
        origin, polygon, gdf=gdf, cell_deg=cell_deg, grid_deg=grid_deg,
        out_html=out_html, interval=interval, cmap_name=cmap_name,
        max_minutes=max_minutes, basemap=basemap, direction=direction,
    )
    return gdf


def build_from_csv(
    origin,
    polygon,
    csv_path: str = "durations.csv",
    direction: str = "from",
    cell_deg: float = 0.0028,
    grid_deg: float = 0.0006,
    out_html: str = "isochrone.html",
    interval: float = 5.0,
    cmap_name: str = "YlOrRd",
    max_minutes=None,
    basemap: str = "voyager",
    force: bool = False,
):
    """从已有的 durations.csv 直接出图，不再调用百度算路。

    适合这些场景：
      - 已跑过一次（durations.csv 已存在），想换配色 / 底图 / 分级间隔 / 方向重新出图；
      - 复用他人分享的 csv 数据，离线出图（不需要百度 AK）；
      - 做参数对比实验（同一份算路结果，只改渲染参数反复出图）。

    原理：用当前配置的 polygon + cell_deg 重新生成渔网（oid 完全确定），
    再把 csv 里同一方向的 duration_min 按 oid 关联回每个采样点，交回
    build_isochrone 做插值 / 裁剪 / 出图。

    注意：csv 的 oid 必须与「当前 polygon + cell_deg 生成的渔网」一致才能对齐——
    durations.csv 本就是用同一套参数生成的，故默认一致；若换了研究区或 cell_deg
    则 oid 不匹配，会大量丢点。方向写反（拿 from 的 csv 用 to 出图）也会丢点。
    """
    gdf_raw = make_fishnet(polygon, cell_deg=cell_deg)
    n_total = len(gdf_raw)
    csv_p = Path(csv_path)
    if not csv_p.exists():
        raise FileNotFoundError(
            f"找不到数据源文件：{csv_path}。请先跑一次正式模式生成，或确认路径正确。"
        )
    # 校验参数指纹：离线出图同样要求 oid 与坐标对应，错配会静默产出错误地图。
    # 用 csv 自身记录的指纹（含生成时的 tactics）做校验，渲染参数(tactics)不改变已存数据。
    meta_p = csv_p.with_suffix(".meta.json")
    meta = {}
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    fp_tactics = meta.get("tactics", 11)
    fp = _config_fingerprint(origin, polygon, cell_deg, fp_tactics)
    if "fingerprint" in meta and meta.get("fingerprint") != fp:
        if not force:
            raise RuntimeError(
                f"durations.csv 的参数指纹与当前配置不一致"
                f"（旧={meta.get('fingerprint')} 当前={fp}）。\n"
                f"  该 csv 是用另一套 origin/研究区/网格/策略生成的，oid 无法对齐；"
                f"强行出图会把旧时长错配到新坐标。\n"
                f"  解决：使用生成该 csv 时的同一 config，或换 --csv 指向正确的数据文件；"
                f"若确认要冒险出图，可加 --force。"
            )
        logger.warning("指纹不匹配，但 --force 已指定，仍按当前配置出图（结果可能不准确）。")
    elif not meta:
        logger.warning(
            "durations.csv 无指纹信息（旧格式），无法校验研究区是否一致；"
            "若当前 polygon/cell_deg 与生成 csv 时不同，oid 可能错配。"
        )
    df = pd.read_csv(csv_p)
    has_dir = "direction" in df.columns
    # 选取目标方向的数据；旧格式 csv 无 direction 列则按 from 处理
    if has_dir:
        df = df[df["direction"] == direction]
    elif direction == "to":
        raise ValueError(
            "csv 中不含 direction 列（旧格式），无法按 'to' 方向提取；"
            "请改用默认 from 方向，或重新跑一次正式模式生成带方向列的数据。"
        )
    done = {
        int(r["oid"]): float(r["duration_min"])
        for _, r in df.iterrows()
        if pd.notna(r.get("duration_min"))
    }
    gdf = gdf_raw.copy()
    gdf["duration_min"] = gdf["oid"].map(done)
    gdf = gdf.dropna(subset=["duration_min"]).reset_index(drop=True)
    n_valid = len(gdf)
    logger.info(
        "从数据源 %s（方向=%s）载入：有效采样点 %d / %d（成功率 %.1f%%）",
        csv_path, direction, n_valid, n_total,
        100.0 * n_valid / max(n_total, 1),
    )
    if n_valid == 0:
        logger.error(
            "csv 中没有任何有效采样点！请确认：\n"
            "  1) csv 路径正确；\n"
            "  2) 当前配置的 polygon + cell_deg 与生成该 csv 时一致（oid 才能对齐）；\n"
            "  3) 方向(direction)选择正确（from/to 未混用）。"
        )
    elif n_valid < 3:
        logger.warning("有效采样点过少(%d)，等时圈将不可靠。", n_valid)
    build_isochrone(
        origin, polygon, gdf=gdf, cell_deg=cell_deg, grid_deg=grid_deg,
        out_html=out_html, interval=interval, cmap_name=cmap_name,
        max_minutes=max_minutes, basemap=basemap, direction=direction,
    )
    return gdf


# --------------------------------------------------------------------------- #
# 模拟数据（无 AK 也能演示，验证几何/插值/出图）
# --------------------------------------------------------------------------- #
def synthetic_durations(gdf, origin, seed=42):
    from math import radians, sin, cos, asin, sqrt

    rng = np.random.default_rng(seed)

    def haversine_km(lat1, lon1, lat2, lon2):
        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = (
            sin(dlat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        )
        return 2 * R * asin(sqrt(a))

    olat, olon = origin
    times = []
    for r in gdf.itertuples():
        d = haversine_km(olat, olon, r.lat, r.lon)
        # 路网绕行系数 ~2.2，加随机扰动模拟路况
        t = d * 2.2 * (1.0 + 0.3 * rng.random())
        times.append(round(t, 1))
    out = gdf.copy()
    out["duration_min"] = times
    return out


# --------------------------------------------------------------------------- #
# 配置 / 多边形解析
# --------------------------------------------------------------------------- #
def polygon_from_coords(coords):
    """coords: [[lng, lat], ...] (百度 BD-09 拾取器格式) -> shapely Polygon(WGS84)。

    每个顶点先做 BD-09 -> WGS84 纠偏，再构造成 (lon, lat) 几何点。
    """
    ring = []
    for lng, lat in coords:
        wgs_lat, wgs_lng = bd09_to_wgs84(lat, lng)
        ring.append((wgs_lng, wgs_lat))
    return Polygon(ring)


# --------------------------------------------------------------------------- #
# 圆形研究区（新增模式）：以 origin 为圆心、radius_km 为半径
# --------------------------------------------------------------------------- #
def circle_polygon(origin, radius_km: float, n: int = 128):
    """
    以 origin=(lat,lon) 为圆心，生成半径 radius_km 公里的近似正圆多边形。
    按纬度方向 / 经度方向分别换算成度数（经度按纬度压缩），
    使在 folium 的 Web Mercator 底图上呈正圆。
    返回 shapely Polygon(lon, lat)，可直接复用现有渔网/裁剪/渲染流程。
    """
    olat, olon = origin
    dlat = radius_km / 110.574  # 1°纬度 ≈ 110.574 km
    dlon = radius_km / (111.320 * math.cos(math.radians(olat)))  # 经度方向随纬度压缩
    ring = []
    for i in range(n):
        theta = 2.0 * math.pi * i / n
        lat = olat + dlat * math.sin(theta)
        lon = olon + dlon * math.cos(theta)
        ring.append((lon, lat))
    return Polygon(ring)


# --------------------------------------------------------------------------- #
# 坐标系转换：百度 BD-09 -> WGS84
# 百度地图坐标拾取器返回的是 BD-09（百度加密坐标），与 OSM 底图差几百米~1km。
# 设 coord_system="bd09ll" 后，工具自动把填入的 BD-09 转成 WGS84，底图对齐。
# --------------------------------------------------------------------------- #
def _transform_lat(lng, lat):
    ret = (-100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat
           + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng)))
    ret += (20.0 * math.sin(6.0 * lng * math.pi)
            + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi)
            + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi)
            + 320.0 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(lng, lat):
    ret = (300.0 + lng + 2.0 * lat + 0.1 * lng * lng
           + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng)))
    ret += (20.0 * math.sin(6.0 * lng * math.pi)
            + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi)
            + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi)
            + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _bd09_to_wgs84_raw(lng, lat):
    """wandergis/coordtransform 标准实现 BD-09 -> WGS84，入参 (lng, lat)。"""
    # 1) BD-09 -> GCJ-02
    x = lng - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi)
    gg_lng = z * math.cos(theta)
    gg_lat = z * math.sin(theta)
    # 2) GCJ-02 -> WGS84
    if not (73.66 < gg_lng < 135.05 and 3.86 < gg_lat < 53.55):
        return gg_lng, gg_lat  # 境外不纠偏
    dlat = _transform_lat(gg_lng - 105.0, gg_lat - 35.0)
    dlng = _transform_lng(gg_lng - 105.0, gg_lat - 35.0)
    radlat = gg_lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - 0.00669342162296594323 * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((6378245.0 * (1 - 0.00669342162296594323))
                             / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (6378245.0 / sqrtmagic
                             * math.cos(radlat) * math.pi)
    return gg_lng - dlng, gg_lat - dlat


def bd09_to_wgs84(lat, lon):
    """百度 BD-09 坐标 -> WGS84（GPS 标准）。入参/返回均为 (lat, lon)。"""
    wgs_lng, wgs_lat = _bd09_to_wgs84_raw(lon, lat)
    return wgs_lat, wgs_lng


def wgs84_to_bd09(lat, lng):
    """WGS84 -> 百度 BD-09。入参/返回均为 (lat, lng)。供 picker 等将 OSM 坐标转百度格式。"""
    # 1) WGS84 -> GCJ-02
    if not (3.86 < lat < 53.55 and 73.66 < lng < 135.05):
        return lat, lng  # 境外不纠偏
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - 0.00669342162296594323 * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((6378245.0 * (1 - 0.00669342162296594323))
                             / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (6378245.0 / sqrtmagic
                             * math.cos(radlat) * math.pi)
    gcj_lat = lat + dlat
    gcj_lng = lng + dlng
    # 2) GCJ-02 -> BD-09
    x = gcj_lng
    y = gcj_lat
    z = math.sqrt(x * x + y * y) + 0.00002 * math.sin(y * math.pi)
    theta = math.atan2(y, x) + 0.000003 * math.cos(x * math.pi)
    bd_lat = z * math.sin(theta) + 0.006
    bd_lng = z * math.cos(theta) + 0.0065
    return bd_lat, bd_lng


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 配置统一用百度 BD-09 格式：origin=[经度,纬度]，多边形顶点=[[经度,纬度],...]
    lng, lat = cfg["origin"]
    origin = bd09_to_wgs84(lat, lng)  # (wgs_lat, wgs_lng)，供渲染/几何/算路统一使用

    if "aoi" in cfg:
        aoi = cfg["aoi"]
        if aoi.get("type") == "circle":
            # 圆形模式：以 origin 为圆心
            polygon = circle_polygon(origin, float(aoi["radius_km"]))
        else:
            polygon = polygon_from_coords(aoi["coords"])
    elif "polygon" in cfg:  # 向后兼容旧配置（纯多边形）
        polygon = polygon_from_coords(cfg["polygon"])
    else:
        raise KeyError("配置缺少 'aoi' 或 'polygon' 字段")

    return origin, polygon, cfg


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="开源等时圈绘制工具（无需 ArcGIS Pro）")
    p.add_argument("--config", help="JSON 配置：含 origin / aoi(或 polygon)")
    p.add_argument("--origin", nargs=2, type=float, metavar=("LON", "LAT"),
                   help="起点经度 纬度（百度坐标拾取器格式）")
    p.add_argument("--polygon", help="研究区多边形 JSON 文件：[[经度,纬度], ...]")
    p.add_argument("--cell", type=float, default=0.0028, help="渔网边长(度)，默认0.0028≈100m")
    p.add_argument("--grid", type=float, default=0.0006, help="插值栅格边长(度)")
    p.add_argument("--out", default=None, help="输出 HTML 路径（默认与 config 同目录）")
    p.add_argument("--interval", type=float, default=5.0, help="等时圈分级间隔(分钟)")
    p.add_argument("--max-minutes", type=float, default=None, help="颜色上限分钟数")
    p.add_argument("--tactics", type=int, default=11, help="百度驾车策略 10/11/12/13")
    p.add_argument("--cmap", default=None, help="配色方案(matplotlib名称)，默认 YlOrRd")
    p.add_argument("--basemap", default=None,
                   choices=["voyager", "positron", "osm"],
                   help="底图：voyager(浅底+地名/POI标注，推荐) / "
                        "positron(极简浅底) / osm(彩色完整)")
    p.add_argument("--direction", default=None, choices=["from", "to"],
                   help="等时圈方向：from=从原点出发(默认) / to=到达原点(原点作为终点)")
    p.add_argument("--csv", default=None, help="批量算路结果缓存（默认与 config 同目录）")
    p.add_argument("--from-csv", action="store_true",
                   help="从已有的 durations.csv 直接出图，不再调用百度算路（离线/换参数重绘）")
    p.add_argument("--force", action="store_true",
                   help="指纹校验失败时跳过确认，直接覆盖已有数据继续（谨慎使用）")
    p.add_argument("--delay", type=float, default=3.0,
                   help="两次批量请求之间的间隔秒数(默认3.0)；频繁遇401并发超限可调大")
    p.add_argument("--demo", action="store_true",
                   help="用模拟数据演示（不需要百度 AK）")
    args = p.parse_args()

    # 解析 origin / polygon
    direction = "from"
    origin_bd = None
    if args.config and (args.origin or args.polygon):
        logger.warning("同时提供 --config 与 --origin/--polygon，命令行坐标被忽略，以 config 为准")
    if args.config:
        origin, polygon, cfg = load_config(args.config)
        args.cell = cfg.get("cell_deg", args.cell)
        args.grid = cfg.get("grid_deg", args.grid)
        args.interval = cfg.get("interval", args.interval)
        args.max_minutes = cfg.get("max_minutes", args.max_minutes)
        args.cmap = cfg.get("cmap", args.cmap)
        args.basemap = cfg.get("basemap", args.basemap)
        # 百度驾车策略：10/11/12/13，影响同一对端点的时长，已纳入指纹校验。
        # 命令行 --tactics 可覆盖 config 中的设置。
        args.tactics = cfg.get("tactics", args.tactics)
        # config 中的 direction 优先于默认，命令行 --direction 可覆盖
        direction = cfg.get("direction", "from")
        origin_bd = tuple(cfg["origin"])  # 百度 BD-09 [经度, 纬度]
    elif args.origin and args.polygon:
        lng, lat = args.origin  # 命令行按百度拾取器顺序：经度 纬度
        origin = bd09_to_wgs84(lat, lng)
        with open(args.polygon, "r", encoding="utf-8") as f:
            coords = json.load(f)  # [[经度,纬度], ...]
        polygon = polygon_from_coords(coords)
        bl, ba = wgs84_to_bd09(lat, lng)  # (bd_lat, bd_lng)
        origin_bd = (ba, bl)
    else:
        p.error("请提供 --config 或同时提供 --origin 与 --polygon（或用 --demo）")

    # 默认输出位置：与 config 文件同目录，避免不同任务产物混在一起
    base_dir = os.path.dirname(os.path.abspath(args.config)) if args.config else None
    if args.out is None:
        args.out = os.path.join(base_dir, "isochrone.html") if base_dir else "isochrone.html"
    if args.csv is None:
        args.csv = os.path.join(base_dir, "durations.csv") if base_dir else "durations.csv"

    # 命令行 --direction 可覆盖 config 中的设置
    if args.direction:
        direction = args.direction

    cmap = args.cmap or "YlOrRd"
    basemap = args.basemap or "voyager"

    if args.from_csv:
        build_from_csv(
            origin, polygon, csv_path=args.csv, direction=direction,
            cell_deg=args.cell, grid_deg=args.grid, out_html=args.out,
            interval=args.interval, cmap_name=cmap, max_minutes=args.max_minutes,
            basemap=basemap, force=args.force,
        )
        logger.info("已从 %s 出图（方向=%s），打开 %s 查看", args.csv, direction, args.out)
        return

    if args.demo:
        gdf = make_fishnet(polygon, cell_deg=args.cell)
        gdf = synthetic_durations(gdf, origin)
        build_isochrone(
            origin, polygon, gdf=gdf, cell_deg=args.cell, grid_deg=args.grid,
            out_html=args.out, interval=args.interval, cmap_name=cmap,
            max_minutes=args.max_minutes, basemap=basemap, direction=direction,
        )
        logger.info("演示完成（模拟数据，方向=%s），打开 %s 查看", direction, args.out)
        return

    run_pipeline(
        origin, polygon, cell_deg=args.cell, grid_deg=args.grid,
        tactics=args.tactics, csv_path=args.csv, out_html=args.out,
        interval=args.interval, cmap_name=cmap, max_minutes=args.max_minutes,
        basemap=basemap, direction=direction,
        origin_bd=origin_bd, force=args.force, delay=args.delay,
    )


if __name__ == "__main__":
    main()

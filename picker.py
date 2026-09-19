"""
picker.py — 交互式选点器（生成 picker.html）

用法：
    python picker.py                       # 默认杭州中心，生成 picker.html 并打开
    python picker.py --center 30.25 120.21 # 指定地图初始中心 [纬度, 经度]
    python picker.py --out my_picker.html  # 指定输出文件名

打开生成的 HTML 后：
  1) 用绘制工具画一个多边形/矩形（研究区），再放一个 Marker（起点）。
  2) 绘制结束后页面底部文本框会显示可直接粘贴进 config.json 的片段：
       - Marker  ->  "origin": [经度, 纬度]
       - 多边形  ->  "polygon": [[经度,纬度], ...]   （与 config.example.json 同一格式）
  3) 坐标已自动从 OSM(WGS84) 转成百度 BD-09，与 config 格式完全一致，无需手动换算（仅限选点器产出）。

说明：本工具统一使用百度坐标系，故 picker 在导出时把 OSM 拾取到的 WGS84 转成 BD-09。
（圆形研究区仍需在 config 里手动写 "aoi":{"type":"circle","radius_km":...}，picker 只产出多边形。）
"""
import os
import webbrowser

import folium
from folium.plugins import Draw

CENTER = [30.2527, 120.2098]  # 默认杭州，可改

JS = """
<div style="position:fixed;bottom:10px;left:10px;z-index:9999;font-family:Arial,Helvetica,sans-serif;">
  <div style="background:#fff;border:1px solid #999;border-radius:6px;padding:6px 8px;max-width:470px;box-shadow:0 1px 4px rgba(0,0,0,0.25);">
    <div style="font-weight:bold;font-size:13px;color:#222;margin-bottom:4px;">
      配置片段（坐标已自动转百度 BD-09，直接复制到 config.json）：
    </div>
    <textarea id="coords" readonly
      style="width:450px;height:170px;font-size:12px;box-sizing:border-box;"></textarea>
    <div style="margin-top:6px;">
      <button id="copybtn" type="button" style="font-size:12px;padding:4px 10px;cursor:pointer;">复制配置</button>
      <button id="dlbtn" type="button" style="font-size:12px;padding:4px 10px;cursor:pointer;margin-left:6px;">下载 BD-09 GeoJSON</button>
    </div>
  </div>
</div>
<script>
function tLat(lng,lat){var r=-100+2*lng+3*lat+0.2*lat*lat+0.1*lng*lat+0.2*Math.sqrt(Math.abs(lng));r+=(20*Math.sin(6*lng*Math.PI)+20*Math.sin(2*lng*Math.PI))*2/3;r+=(20*Math.sin(lat*Math.PI)+40*Math.sin(lat/3*Math.PI))*2/3;r+=(160*Math.sin(lat/12*Math.PI)+320*Math.sin(lat*Math.PI/30))*2/3;return r;}
function tLng(lng,lat){var r=300+lng+2*lat+0.1*lng*lng+0.1*lng*lat+0.1*Math.sqrt(Math.abs(lng));r+=(20*Math.sin(6*lng*Math.PI)+20*Math.sin(2*lng*Math.PI))*2/3;r+=(20*Math.sin(lng*Math.PI)+40*Math.sin(lng/3*Math.PI))*2/3;r+=(150*Math.sin(lng/12*Math.PI)+300*Math.sin(lng/30*Math.PI))*2/3;return r;}
function wgs2bd(lng,lat){var dlat=tLat(lng-105,lat-35),dlng=tLng(lng-105,lat-35);var rl=lat/180*Math.PI;var m=Math.sin(rl);m=1-0.00669342162296594323*m*m;var sm=Math.sqrt(m);dlat=(dlat*180)/((6378245*(1-0.00669342162296594323))/(m*sm)*Math.PI);dlng=(dlng*180)/(6378245/sm*Math.cos(rl)*Math.PI);var gl=lat+dlat,gn=lng+dlng;var x=gn,y=gl;var z=Math.sqrt(x*x+y*y)+0.00002*Math.sin(y*Math.PI);var th=Math.atan2(y,x)+0.000003*Math.cos(x*Math.PI);return [z*Math.cos(th)+0.0065, z*Math.sin(th)+0.006];}
function fmt(c){var b=wgs2bd(c[0],c[1]);return [+b[0].toFixed(6), +b[1].toFixed(6)];}
var _picker_group=null;
function tGeo(g){
  function conv(p){var b=wgs2bd(p[0],p[1]);return [b[0],b[1]];}
  if(g.type==='Point'){g.coordinates=conv(g.coordinates);}
  else if(g.type==='Polygon'){g.coordinates=g.coordinates.map(function(ring){return ring.map(conv);});}
  return g;
}
function refresh(){
  if(!_picker_group) return;
  var pt=null, poly=null;
  _picker_group.eachLayer(function(layer){
    var g=tGeo(layer.toGeoJSON().geometry);
    if(g.type==='Point'){pt=g.coordinates;}
    else if(g.type==='Polygon'){poly=g.coordinates[0];}
  });
  var s='';
  if(pt){ s+='"origin": ['+pt[0]+', '+pt[1]+']\\n'; }
  if(poly){
    s+='"polygon": [\\n';
    poly.forEach(function(c,i){ s+='  ['+c[0]+', '+c[1]+']'+(i<poly.length-1?',':'')+'\\n'; });
    s+=']';
  }
  document.getElementById('coords').value=s;
}
function getFoliumMap(){
  for(var k in window){
    var o=window[k];
    if(o && o._leaflet_id!==undefined && typeof o.on==='function'
       && typeof o.addLayer==='function' && typeof o.getZoom==='function'){ return o; }
  }
  return null;
}
function attach(){
  var map=getFoliumMap();
  if(!map){ console.warn('picker: map not found'); return; }
  _picker_group=L.featureGroup().addTo(map);
  map.on('draw:created', function(e){
    _picker_group.addLayer(e.layer);
    refresh();
  });
}
document.getElementById('copybtn').onclick=function(){
  var t=document.getElementById('coords'); t.select();
  if(navigator.clipboard){ navigator.clipboard.writeText(t.value).catch(function(){document.execCommand('copy');}); }
  else { document.execCommand('copy'); }
};
document.getElementById('dlbtn').onclick=function(){
  if(!_picker_group || _picker_group.getLayers().length===0){ alert('请先在地图上绘制起点和研究区'); return; }
  var feats=[];
  _picker_group.eachLayer(function(layer){ feats.push({type:'Feature',properties:{},geometry:tGeo(layer.toGeoJSON().geometry)}); });
  var fc={type:'FeatureCollection',features:feats};
  var blob=new Blob([JSON.stringify(fc,null,2)],{type:'application/json'});
  var a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='picker_bd09.geojson'; a.click();
};
if(document.readyState==='complete'||document.readyState==='interactive'){ attach(); }
else { window.addEventListener('load', attach); }
</script>
"""


def build_picker(center=CENTER, out="picker.html"):
    m = folium.Map(location=center, zoom_start=13, tiles="OpenStreetMap")
    Draw(
        export=False,
        draw_options={
            "polyline": False,
            "rectangle": True,
            "circle": False,
            "marker": True,
            "circlemarker": False,
            "polygon": True,
        },
        edit_options={"edit": True},
    ).add_to(m)
    from folium import Element

    m.get_root().html.add_child(Element(JS))
    m.save(out)
    print(f"已生成 {out}。用浏览器打开后：画一个多边形/矩形(研究区)+放一个 Marker(起点)，"
          f"左下角文本框会自动显示「已转百度 BD-09」的 config 片段，可点「复制配置」或「下载 BD-09 GeoJSON」。")
    return out


def main():
    """CLI 入口：解析参数、生成 picker.html 并尝试自动打开浏览器。"""
    import argparse

    p = argparse.ArgumentParser(description="交互式选点器：绘制研究区与起点，导出 config 片段")
    p.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"),
                   help="地图初始中心 [纬度, 经度]，默认杭州 [30.2527, 120.2098]")
    p.add_argument("--out", default="picker.html", help="生成的 HTML 文件名（默认 picker.html）")
    args = p.parse_args()

    center = args.center if args.center else CENTER
    f = build_picker(center=center, out=args.out)
    try:
        webbrowser.open("file://" + os.path.abspath(f))
    except Exception:
        abs_path = os.path.abspath(f)
        print(f"[提示] 无法自动打开浏览器，请手动打开文件：{abs_path}")


if __name__ == "__main__":
    main()

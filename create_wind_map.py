import math
import datetime
import requests
import urllib3
import pandas as pd
import folium
import fitdecode
import fitdecode.utils

# 1. CRC計算処理をダミー化して計算時間をゼロ化
fitdecode.utils.compute_crc = lambda chunk, crc=0: crc

# 2. どんなイベントハンドラ（on_header, on_data_message等）が呼ばれても空関数を返して即座に無視する万能プロセッサ
class FastProcessor:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None

# SSL検証無効化時の警告表示を抑止
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1. FITファイルのストリーミング読み込み＆超高速抽出 ---
def load_fit_data(file_path):
    records = []
    count = 0
    sample_rate = 50  # 50レコード毎に1件抽出し、全体で処理量を1/50に削減

    with fitdecode.FitReader(file_path, processor=FastProcessor()) as fit:
        for frame in fit:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == 'record':
                count += 1
                if count % sample_rate != 0:
                    continue

                lat = frame.get_value('position_lat', fallback=None)
                lon = frame.get_value('position_long', fallback=None)
                ts = frame.get_value('timestamp', fallback=None)

                if lat is not None and lon is not None and ts is not None:
                    records.append({
                        'timestamp': ts,
                        'lat': lat * (180 / 2**31),
                        'lon': lon * (180 / 2**31)
                    })

    df = pd.DataFrame(records)
    if not df.empty:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        if df['timestamp'].dt.tz is None:
            df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
        df['timestamp'] = df['timestamp'].dt.tz_convert('Asia/Tokyo')

    return df

# --- 2. 過去の気象データ（m/s単位）を取得 ---
def fetch_wind_data(lat, lon, target_datetime):
    date_str = target_datetime.strftime('%Y-%m-%d')
    today = datetime.datetime.now(target_datetime.tzinfo).date()
    days_diff = (today - target_datetime.date()).days

    if days_diff <= 90:
        base_url = "https://api.open-meteo.com/v1/forecast"
    else:
        base_url = "https://archive-api.open-meteo.com/v1/archive"

    url = (
        f"{base_url}?"
        f"latitude={lat}&longitude={lon}&start_date={date_str}&end_date={date_str}"
        f"&hourly=wind_speed_10m,wind_direction_10m"
        f"&wind_speed_unit=ms"
        f"&timezone=Asia%2FTokyo"
    )

    # timeout=10 を設定してAPI応答遅延を防止
    response = requests.get(url, verify=False, timeout=10).json()
    hourly = response.get('hourly', {})

    times = pd.to_datetime(hourly.get('time', [])).tz_localize('Asia/Tokyo')
    wind_speeds = hourly.get('wind_speed_10m', [])
    wind_dirs = hourly.get('wind_direction_10m', [])

    wind_df = pd.DataFrame({
        'time': times,
        'wind_speed': wind_speeds,
        'wind_dir': wind_dirs
    })

    return wind_df

# --- 3. 2点間の進行方位角（Bearing）を計算 ---
def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lon_rad = math.radians(lon2 - lon1)

    y = math.sin(delta_lon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon_rad)

    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360) % 360

# --- 4. 向かい風/追い風の判定（6段階区分） ---
def get_relative_wind_info(wind_dir, wind_speed_ms, heading):
    angle_diff = math.radians(wind_dir - heading)
    effective_headwind = wind_speed_ms * math.cos(angle_diff)

    if effective_headwind >= 2.0:
        segment_color = "#D32F2F"   # 強向かい風（赤）
        arrow_fill = "#D32F2F"
        arrow_stroke = "#FFFFFF"
        label = "強向かい風"
    elif effective_headwind >= 0.5:
        segment_color = "#FB8C00"   # 弱向かい風（オレンジ）
        arrow_fill = "#FB8C00"
        arrow_stroke = "#FFFFFF"
        label = "弱向かい風"
    elif effective_headwind > -0.5:
        segment_color = "#FBC02D"   # 横風・無風（黄）
        arrow_fill = "none"         # 矢印枠のみ
        arrow_stroke = "#333333"
        label = "横風/微風"
    elif effective_headwind > -2.0:
        segment_color = "#8BC34A"   # 弱追い風（黄緑）
        arrow_fill = "#8BC34A"
        arrow_stroke = "#FFFFFF"
        label = "弱追い風"
    elif effective_headwind > -3.5:
        segment_color = "#4CAF50"   # 追い風（緑）
        arrow_fill = "#4CAF50"
        arrow_stroke = "#FFFFFF"
        label = "追い風"
    else:
        segment_color = "#1976D2"   # 強追い風（青）
        arrow_fill = "#1976D2"
        arrow_stroke = "#FFFFFF"
        label = "強追い風"

    return segment_color, arrow_fill, arrow_stroke, effective_headwind, label

# --- 5. メイン処理とマップ描画 ---
def create_wind_map(fit_path, output_html="wind_map.html"):
    df = load_fit_data(fit_path)
    if df.empty:
        raise ValueError("有効な位置データが見つかりませんでした。")

    # 【高速化】データポイントを約5分（300秒）感覚相当に間引く (例: 1秒毎データなら300行置き)
    # これにより描画速度とメモリ使用量を劇的に削減します
    step = max(1, len(df) // 100)  # 全体で約100ポイント前後に最適化
    sample_df = df.iloc[::step].copy().reset_index(drop=True)

    start_time_jst = df['timestamp'].iloc[0]
    end_time_jst = df['timestamp'].iloc[-1]
    mid_lat = df['lat'].mean()
    mid_lon = df['lon'].mean()
    wind_df = fetch_wind_data(mid_lat, mid_lon, start_time_jst)

    sample_df['timestamp'] = sample_df['timestamp'].astype('datetime64[s, Asia/Tokyo]')
    wind_df['time'] = wind_df['time'].astype('datetime64[s, Asia/Tokyo]')

    sample_df = pd.merge_asof(
        sample_df.sort_values('timestamp'),
        wind_df.sort_values('time'),
        left_on='timestamp',
        right_on='time',
        direction='nearest'
    )

    m = folium.Map(
        location=[mid_lat, mid_lon],
        zoom_start=12,
        tiles="https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png",
        attr="国土地理院 淡色地図",
        name="国土地理院 (淡色)"
    )

    folium.TileLayer(
        tiles="https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
        attr="国土地理院 標準地図",
        name="国土地理院 (標準)"
    ).add_to(m)

    folium.TileLayer(
        tiles="https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg",
        attr="国土地理院 写真",
        name="国土地理院 (写真)"
    ).add_to(m)

    folium.LayerControl().add_to(m)

    eff_headwinds = []
    headwind_count = 0
    crosswind_count = 0
    tailwind_count = 0

    sample_list = sample_df.to_dict('records')
    total_segments = max(1, len(sample_list) - 1)

    for i in range(len(sample_list) - 1):
        p1 = sample_list[i]
        p2 = sample_list[i+1]

        heading = calculate_bearing(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
        wind_dir = p1['wind_dir'] if not pd.isna(p1['wind_dir']) else 0
        wind_speed_ms = float(p1['wind_speed']) if not pd.isna(p1['wind_speed']) else 0.0

        segment_color, _, _, eff_headwind, wind_label = get_relative_wind_info(wind_dir, wind_speed_ms, heading)
        eff_headwinds.append(eff_headwind)

        if eff_headwind >= 0.5:
            headwind_count += 1
        elif eff_headwind <= -0.5:
            tailwind_count += 1
        else:
            crosswind_count += 1

        seg_coords = [(p1['lat'], p1['lon']), (p2['lat'], p2['lon'])]
        folium.PolyLine(
            seg_coords,
            color=segment_color,
            weight=6,
            opacity=0.85,
            popup=f"区間状況: {wind_label}<br>実効向かい風: {eff_headwind:+.1f} m/s<br>進行方向: {heading:.0f}°"
        ).add_to(m)

    for i, row in sample_df.iterrows():
        wind_dir = row['wind_dir'] if not pd.isna(row['wind_dir']) else 0
        wind_speed_ms = float(row['wind_speed']) if not pd.isna(row['wind_speed']) else 0.0

        if i < len(sample_df) - 1:
            next_row = sample_df.iloc[i+1]
            heading = calculate_bearing(row['lat'], row['lon'], next_row['lat'], next_row['lon'])
            _, arrow_fill, arrow_stroke, eff_headwind, _ = get_relative_wind_info(wind_dir, wind_speed_ms, heading)
        else:
            arrow_fill = "none"
            arrow_stroke = "#333333"
            eff_headwind = 0.0

        size = max(20, min(48, int(20 + wind_speed_ms * 3.5)))
        anchor = size // 2
        arrow_angle = (wind_dir + 180) % 360

        svg_icon = f"""
		<div style="width: {size}px; height: {size}px; background: transparent; display: flex; align-items: center; justify-content: center;">
            <div style="transform: rotate({arrow_angle}deg); width: {size}px; height: {size}px; display: flex; align-items: center; justify-content: center;">
                <svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="filter: drop-shadow(0px 1px 2px rgba(0,0,0,0.5));">
                    <path d="M12 2L4 21L12 17L20 21L12 2Z" fill="{arrow_fill}" stroke="{arrow_stroke}" stroke-width="1" stroke-linejoin="round"/>
                </svg>
            </div>
        </div>
        """
        icon = folium.DivIcon(html=svg_icon, icon_size=(size, size), icon_anchor=(anchor, anchor))

        time_str = row['timestamp'].strftime('%H:%M')
        popup_text = (
            f"時刻: {time_str} (JST)<br>"
            f"風向: {wind_dir:.0f}° / 風速: {wind_speed_ms:.1f} m/s<br>"
            f"体感向かい風: {eff_headwind:+.1f} m/s"
        )

        folium.Marker(
            location=[row['lat'], row['lon']],
            icon=icon,
            popup=popup_text
        ).add_to(m)

    start_pos = [df['lat'].iloc[0], df['lon'].iloc[0]]
    goal_pos = [df['lat'].iloc[-1], df['lon'].iloc[-1]]

    start_icon_html = """
    <div style="background-color: #2E7D32; color: white; border: 2px solid white; border-radius: 50%; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 13px; box-shadow: 0 2px 6px rgba(0,0,0,0.5); font-family: sans-serif;">
        S
    </div>
    """
    goal_icon_html = """
    <div style="background-color: #C62828; color: white; border: 2px solid white; border-radius: 50%; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 13px; box-shadow: 0 2px 6px rgba(0,0,0,0.5); font-family: sans-serif;">
        G
    </div>
    """

    folium.Marker(
        location=start_pos,
        icon=folium.DivIcon(html=start_icon_html, icon_size=(28, 28), icon_anchor=(14, 14)),
        popup=f"START<br>{start_time_jst.strftime('%Y-%m-%d %H:%M')}"
    ).add_to(m)

    folium.Marker(
        location=goal_pos,
        icon=folium.DivIcon(html=goal_icon_html, icon_size=(28, 28), icon_anchor=(14, 14)),
        popup=f"GOAL<br>{end_time_jst.strftime('%Y-%m-%d %H:%M')}"
    ).add_to(m)

    avg_wind = sample_df['wind_speed'].mean() if not sample_df['wind_speed'].empty else 0
    max_wind = sample_df['wind_speed'].max() if not sample_df['wind_speed'].empty else 0
    max_headwind = max(eff_headwinds) if eff_headwinds else 0

    head_pct = (headwind_count / total_segments) * 100
    cross_pct = (crosswind_count / total_segments) * 100
    tail_pct = (tailwind_count / total_segments) * 100

    summary_html = f"""
    <div style="
        position: fixed;
        top: 20px; right: 20px; width: 200px;
        background-color: rgba(255, 255, 255, 0.95);
        border: 2px solid #555555; z-index: 9999; font-size: 12px;
        padding: 10px; border-radius: 8px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
        font-family: sans-serif; line-height: 1.5;
    ">
        <b style="font-size: 13px; color: #333; display: block; border-bottom: 1px solid #ccc; padding-bottom: 4px; margin-bottom: 6px;">
            🚩 RIDE SUMMARY
        </b>
        <b>日付:</b> {start_time_jst.strftime('%Y-%m-%d')}<br>
        <b>平均風速:</b> {avg_wind:.1f} m/s<br>
        <b>最大風速:</b> {max_wind:.1f} m/s<br>
        <b>最大体感向かい風:</b> <span style="color:#D32F2F; font-weight:bold;">+{max_headwind:.1f} m/s</span>
        <hr style="border:0; border-top:1px solid #eee; margin:6px 0;">
        <b>風向の内訳:</b><br>
        <div style="display:flex; justify-content:space-between; margin-top:2px;">
            <span><i style="color:#D32F2F;">■</i> 向かい風:</span> <b>{head_pct:.0f}%</b>
        </div>
        <div style="display:flex; justify-content:space-between;">
            <span><i style="color:#FBC02D;">■</i> 横風/微風:</span> <b>{cross_pct:.0f}%</b>
        </div>
        <div style="display:flex; justify-content:space-between;">
            <span><i style="color:#1976D2;">■</i> 追い風:</span> <b>{tail_pct:.0f}%</b>
        </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(summary_html))

    legend_html = """
    <div style="
        position: fixed;
        bottom: 20px; left: 20px; width: 160px; height: 160px;
        background-color: rgba(255, 255, 255, 0.95);
        border: 2px solid grey; z-index: 9999; font-size: 11px;
        padding: 8px; border-radius: 6px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3);
        font-family: sans-serif;
    ">
        <b>相対風向凡例 (実効風)</b><br>
        <i style="background: #D32F2F; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 強向かい風 (+2m/s〜)<br>
        <i style="background: #FB8C00; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 弱向かい風 (+0.5m/s)<br>
        <i style="background: transparent; border: 1.5px solid #333333; width: 10px; height: 10px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 横風 / 微風<br>
        <i style="background: #8BC34A; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 弱追い風 (-0.5m/s)<br>
        <i style="background: #4CAF50; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 追い風 (-2.0m/s)<br>
        <i style="background: #1976D2; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 強追い風 (-3.5m/s〜)
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    m.save(output_html)
import math
import struct
import datetime
import requests
import urllib3
import xml.etree.ElementTree as ET
import pandas as pd
import folium

# SSL検証無効化時の警告表示を抑止
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1-A. TCX (XML) ファイルのダイレクト抽出 ---
def parse_tcx_data(file_path):
    records = []
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        ns = {'ns': 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'}

        trackpoints = root.findall('.//ns:Trackpoint', ns)
        if not trackpoints:
            trackpoints = root.findall('.//Trackpoint')

        for tp in trackpoints:
            time_node = tp.find('ns:Time', ns) if tp.find('ns:Time', ns) is not None else tp.find('Time')
            pos_node = tp.find('ns:Position', ns) if tp.find('ns:Position', ns) is not None else tp.find('Position')

            if time_node is not None and pos_node is not None and time_node.text:
                lat_node = pos_node.find('ns:LatitudeDegrees', ns) if pos_node.find('ns:LatitudeDegrees', ns) is not None else pos_node.find('LatitudeDegrees')
                lon_node = pos_node.find('ns:LongitudeDegrees', ns) if pos_node.find('ns:LongitudeDegrees', ns) is not None else pos_node.find('LongitudeDegrees')

                if lat_node is not None and lon_node is not None and lat_node.text and lon_node.text:
                    try:
                        lat = float(lat_node.text)
                        lon = float(lon_node.text)
                        ts_str = time_node.text.replace('Z', '+00:00')
                        dt_utc = datetime.datetime.fromisoformat(ts_str)
                        if dt_utc.tzinfo is None:
                            dt_utc = dt_utc.replace(tzinfo=datetime.timezone.utc)

                        records.append({
                            'timestamp': dt_utc,
                            'lat': lat,
                            'lon': lon
                        })
                    except Exception:
                        continue
    except Exception as e:
        print(f"[TCX Parse Error] {e}")

    return records

# --- 1-B. FIT (バイナリ) ファイルのダイレクト抽出 ---
def parse_record_bytes(msg_bytes, fields, last_ts_ref, records):
    lat, lon, ts = None, None, None

    if 253 in fields:
        f_off, f_sz, _, end = fields[253]
        if f_off + f_sz <= len(msg_bytes) and f_sz == 4:
            ts = struct.unpack(end + 'I', msg_bytes[f_off:f_off+4])[0]

    if ts is not None and ts > 100000:
        last_ts_ref[0] = ts
    else:
        if last_ts_ref[0] is None:
            last_ts_ref[0] = 1135987200
        else:
            last_ts_ref[0] += 1
        ts = last_ts_ref[0]

    if 0 in fields and 1 in fields:
        f_off0, f_sz0, _, end0 = fields[0]
        f_off1, f_sz1, _, end1 = fields[1]
        if f_sz0 == 4 and f_sz1 == 4 and f_off0 + 4 <= len(msg_bytes) and f_off1 + 4 <= len(msg_bytes):
            lat_raw = struct.unpack(end0 + 'i', msg_bytes[f_off0:f_off0+4])[0]
            lon_raw = struct.unpack(end1 + 'i', msg_bytes[f_off1:f_off1+4])[0]
            if lat_raw != 0x7FFFFFFF and lat_raw != 0 and lon_raw != 0x7FFFFFFF and lon_raw != 0:
                lat = lat_raw * (180 / 2**31)
                lon = lon_raw * (180 / 2**31)

    if (lat is None or lon is None) and 1 in fields and 2 in fields:
        f_off1, f_sz1, _, end1 = fields[1]
        f_off2, f_sz2, _, end2 = fields[2]
        if f_sz1 == 4 and f_sz2 == 4 and f_off1 + 4 <= len(msg_bytes) and f_off2 + 4 <= len(msg_bytes):
            lat_raw = struct.unpack(end1 + 'i', msg_bytes[f_off1:f_off1+4])[0]
            lon_raw = struct.unpack(end2 + 'i', msg_bytes[f_off2:f_off2+4])[0]
            if lat_raw != 0x7FFFFFFF and lat_raw != 0 and lon_raw != 0x7FFFFFFF and lon_raw != 0:
                lat = lat_raw * (180 / 2**31)
                lon = lon_raw * (180 / 2**31)

    if lat is not None and lon is not None:
        dt_utc = datetime.datetime.fromtimestamp(ts + 631065600, tz=datetime.timezone.utc)
        records.append({
            'timestamp': dt_utc,
            'lat': lat,
            'lon': lon
        })

def parse_fit_data(file_path):
    with open(file_path, 'rb') as f:
        data = f.read()

    if len(data) < 12:
        return []

    records = []
    file_offset = 0
    last_ts_ref = [None]

    while file_offset + 12 <= len(data):
        header_size = data[file_offset]
        if header_size < 12 or header_size > 32:
            file_offset += 1
            continue

        if header_size >= 14 and file_offset + 12 <= len(data):
            if data[file_offset+8 : file_offset+12] != b'.FIT':
                file_offset += 1
                continue

        data_size = struct.unpack('<I', data[file_offset+4 : file_offset+8])[0]
        block_end = min(len(data), file_offset + header_size + data_size)

        if block_end <= file_offset + header_size:
            file_offset += 1
            continue

        offset = file_offset + header_size
        definitions = {}

        while offset < block_end:
            header = data[offset]

            if header & 0x80:
                local_id = (header >> 5) & 0x03
                time_offset = header & 0x1F
                if last_ts_ref[0] is not None:
                    last_ts_ref[0] = (last_ts_ref[0] & ~0x1F) + time_offset

                offset += 1
                if local_id in definitions:
                    def_info = definitions[local_id]
                    mesg_size = def_info['size']
                    if offset + mesg_size <= block_end:
                        if def_info['is_record']:
                            msg_bytes = data[offset : offset + mesg_size]
                            parse_record_bytes(msg_bytes, def_info['fields'], last_ts_ref, records)
                        offset += mesg_size
                    else:
                        offset += 1
                else:
                    offset += 1
                continue

            is_def = bool(header & 0x40)
            has_dev = bool(header & 0x20)
            local_id = header & 0x0F

            if is_def:
                if offset + 6 > block_end:
                    break
                architecture = data[offset + 2]
                endian = '<' if architecture == 0 else '>'
                global_mesg_num = struct.unpack(endian + 'H', data[offset+3:offset+5])[0]
                num_fields = data[offset + 5]

                curr = offset + 6
                field_offsets = {}
                mesg_size = 0

                for _ in range(num_fields):
                    if curr + 3 > block_end:
                        break
                    f_num, f_size, f_type = struct.unpack('BBB', data[curr:curr+3])
                    field_offsets[f_num] = (mesg_size, f_size, f_type, endian)
                    mesg_size += f_size
                    curr += 3

                if has_dev and curr < block_end:
                    num_dev_fields = data[curr]
                    curr += 1
                    for _ in range(num_dev_fields):
                        if curr + 3 > block_end:
                            break
                        dev_f_num, dev_f_size, dev_idx = struct.unpack('BBB', data[curr:curr+3])
                        mesg_size += dev_f_size
                        curr += 3

                definitions[local_id] = {
                    'size': mesg_size,
                    'is_record': (global_mesg_num == 20) or (0 in field_offsets and 1 in field_offsets) or (1 in field_offsets and 2 in field_offsets),
                    'fields': field_offsets
                }
                offset = curr
            else:
                offset += 1
                if local_id in definitions:
                    def_info = definitions[local_id]
                    mesg_size = def_info['size']
                    if offset + mesg_size <= block_end:
                        if def_info['is_record']:
                            msg_bytes = data[offset : offset + mesg_size]
                            parse_record_bytes(msg_bytes, def_info['fields'], last_ts_ref, records)
                        offset += mesg_size
                    else:
                        offset += 1
                else:
                    offset += 1

        file_offset = block_end + 2

    return records

# --- 1-C. ファイル種別の自動判別 ---
def load_activity_data(file_path):
    is_tcx = file_path.lower().endswith('.tcx')
    if not is_tcx:
        try:
            with open(file_path, 'rb') as f:
                head = f.read(100).strip()
                if head.startswith(b'<') or b'xml' in head.lower() or b'trainingcenterdatabase' in head.lower():
                    is_tcx = True
        except Exception:
            pass

    if is_tcx:
        records = parse_tcx_data(file_path)
    else:
        records = parse_fit_data(file_path)

    df = pd.DataFrame(records)
    if not df.empty:
        df['timestamp'] = df['timestamp'].dt.tz_convert('Asia/Tokyo')

        total_count = len(df)
        if total_count <= 500:
            target_arrows = 60
        elif total_count <= 2000:
            target_arrows = 100
        else:
            target_arrows = 180

        step = max(1, total_count // target_arrows)
        df = df.iloc[::step].copy().reset_index(drop=True)

    return df

# --- 2. 気象データ取得 ---
def fetch_wind_data(lat, lon, target_datetime):
    date_str = target_datetime.strftime('%Y-%m-%d')
    today = datetime.datetime.now(target_datetime.tzinfo).date()
    days_diff = (today - target_datetime.date()).days

    if 0 <= days_diff <= 7:
        base_url = "https://api.open-meteo.com/v1/forecast"
    else:
        base_url = "https://archive-api.open-meteo.com/v1/archive"

    url = (
        f"{base_url}?"
        f"latitude={lat:.4f}&longitude={lon:.4f}&start_date={date_str}&end_date={date_str}"
        f"&hourly=wind_speed_10m,wind_direction_10m"
        f"&wind_speed_unit=ms"
        f"&timezone=Asia%2FTokyo"
    )

    try:
        response = requests.get(url, verify=False, timeout=10)
        res_json = response.json()
        hourly = res_json.get('hourly', {})

        raw_times = hourly.get('time', [])
        raw_speeds = hourly.get('wind_speed_10m', [])
        raw_dirs = hourly.get('wind_direction_10m', [])

        if not raw_times or len(raw_times) == 0:
            archive_url = (
                f"https://archive-api.open-meteo.com/v1/archive?"
                f"latitude={lat:.4f}&longitude={lon:.4f}&start_date={date_str}&end_date={date_str}"
                f"&hourly=wind_speed_10m,wind_direction_10m"
                f"&wind_speed_unit=ms&timezone=Asia%2FTokyo"
            )
            res_json = requests.get(archive_url, verify=False, timeout=10).json()
            hourly = res_json.get('hourly', {})
            raw_times = hourly.get('time', [])
            raw_speeds = hourly.get('wind_speed_10m', [])
            raw_dirs = hourly.get('wind_direction_10m', [])

        times = pd.to_datetime(raw_times).tz_localize('Asia/Tokyo')
        wind_speeds = raw_speeds
        wind_dirs = raw_dirs

    except Exception as e:
        print(f"[Error] Failed to fetch wind data: {e}")
        times = pd.date_range(start=f"{date_str} 00:00", periods=24, freq='h', tz='Asia/Tokyo')
        wind_speeds = [0.0] * 24
        wind_dirs = [0] * 24

    wind_df = pd.DataFrame({
        'time': times,
        'wind_speed': wind_speeds,
        'wind_dir': wind_dirs
    })

    return wind_df

# --- 3. 方位角計算 ---
def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lon_rad = math.radians(lon2 - lon1)

    y = math.sin(delta_lon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon_rad)

    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360) % 360

# --- 4. 風向判定 ---
def get_relative_wind_info(wind_dir, wind_speed_ms, heading):
    angle_diff = math.radians(wind_dir - heading)
    effective_headwind = wind_speed_ms * math.cos(angle_diff)

    if effective_headwind >= 2.0:
        return "#D32F2F", "#D32F2F", "#FFFFFF", effective_headwind, "強向かい風"
    elif effective_headwind >= 0.5:
        return "#FB8C00", "#FB8C00", "#FFFFFF", effective_headwind, "弱向かい風"
    elif effective_headwind > -0.5:
        return "#FFFFFF", "none", "#333333", effective_headwind, "横風/微風"
    elif effective_headwind > -2.0:
        return "#9CCC65", "#9CCC65", "#FFFFFF", effective_headwind, "弱追い風"
    elif effective_headwind > -3.5:
        return "#2E7D32", "#2E7D32", "#FFFFFF", effective_headwind, "追い風"
    else:
        return "#1976D2", "#1976D2", "#FFFFFF", effective_headwind, "強追い風"

# --- 5. メイン描画処理 ---
def create_wind_map(fit_path, output_html="wind_map.html"):
    df = load_activity_data(fit_path)
    if df.empty:
        raise ValueError("有効な位置データが見つかりませんでした。")

    start_time_jst = df['timestamp'].iloc[0]
    end_time_jst = df['timestamp'].iloc[-1]
    mid_lat = df['lat'].mean()
    mid_lon = df['lon'].mean()
    wind_df = fetch_wind_data(mid_lat, mid_lon, start_time_jst)

    df['timestamp'] = pd.to_datetime(df['timestamp']).astype('datetime64[s, Asia/Tokyo]')
    wind_df['time'] = pd.to_datetime(wind_df['time']).astype('datetime64[s, Asia/Tokyo]')

    sample_df = pd.merge_asof(
        df.sort_values('timestamp'),
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
        name="国土地理院 (淡色)",
        zoom_snap=0.25,
        zoom_delta=0.25,
        wheelPxPerZoomLevel=200
    )

    # 全体が収まるよう自動ズーム範囲を設定
    min_lat, max_lat = df['lat'].min(), df['lat'].max()
    min_lon, max_lon = df['lon'].min(), df['lon'].max()
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=[30, 30])

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
            _, arrow_fill, arrow_stroke, eff_headwind, wind_label = get_relative_wind_info(wind_dir, wind_speed_ms, heading)
        else:
            arrow_fill = "none"
            arrow_stroke = "#333333"
            eff_headwind = 0.0
            wind_label = "横風/微風"

        size = max(20, min(48, int(20 + wind_speed_ms * 3.5)))

        if wind_label in ["横風/微風", "弱追い風"]:
            size = max(12, int(size * 0.5))

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
        popup_html = f"""
        <div style="white-space: nowrap; font-size: 11px; line-height: 1.5; padding: 2px;">
            <b>時刻:</b> {time_str} (JST)<br>
            <b>風向:</b> {wind_dir:.0f}° / <b>風速:</b> {wind_speed_ms:.1f} m/s<br>
            <b>体感向かい風:</b> <span style="font-weight:bold;">{eff_headwind:+.1f} m/s</span>
        </div>
        """

        folium.Marker(
            location=[row['lat'], row['lon']],
            icon=icon,
            popup=folium.Popup(popup_html, max_width=300)
        ).add_to(m)

    start_pos = [df['lat'].iloc[0], df['lon'].iloc[0]]
    goal_pos = [df['lat'].iloc[-1], df['lon'].iloc[-1]]

    start_icon_html = """
    <div style="background-color: #2E7D32; color: white; border: 2px solid white; border-radius: 50%; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 13px; box-shadow: 0 2px 6px rgba(0,0,0,0.5); font-family: sans-serif;">S</div>
    """
    goal_icon_html = """
    <div style="background-color: #C62828; color: white; border: 2px solid white; border-radius: 50%; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 13px; box-shadow: 0 2px 6px rgba(0,0,0,0.5); font-family: sans-serif;">G</div>
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

    clean_speeds = sample_df['wind_speed'].dropna()
    avg_wind = clean_speeds.mean() if not clean_speeds.empty else 0.0
    max_wind = clean_speeds.max() if not clean_speeds.empty else 0.0
    max_headwind = max(eff_headwinds) if eff_headwinds else 0.0

    head_pct = (headwind_count / total_segments) * 100
    cross_pct = (crosswind_count / total_segments) * 100
    tail_pct = (tailwind_count / total_segments) * 100

    # --- 風恩恵得点 (0〜100点) の計算 ---
    wind_score = int(round(head_pct * 0.125 + cross_pct * 0.5 + tail_pct * 1.05))
    wind_score = max(0, min(100, wind_score))

    if wind_score >= 80:
        score_color = "#2E7D32"
    elif wind_score >= 50:
        score_color = "#1976D2"
    else:
        score_color = "#D32F2F"

	# --- レイアウト切り替え用 CSS と HTML / JS ---
    ui_layout_html = f"""
    <style>
        /* モード 0: デフォルト (標準: 右上 & 左下) */
        body.mode-0 #summary-panel {{
            position: fixed; top: 80px; right: 10px; width: 150px;
            background: rgba(255,255,255,0.95); border: 2px solid #555; z-index: 9999;
            font-size: 11px; padding: 8px; border-radius: 8px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.4;
        }}
        body.mode-0 #legend-panel {{
            position: fixed; bottom: 20px; left: 20px; width: 160px; height: auto;
            background: rgba(255,255,255,0.95); border: 2px solid grey; z-index: 9999;
            font-size: 11px; padding: 8px; border-radius: 6px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.4;
        }}
        body.mode-0 .view-vertical {{ display: block; }}
        body.mode-0 .view-horizontal {{ display: none; }}

        /* モード 1: 右側一括（縦並び） */
        body.mode-1 #summary-panel {{
            position: fixed; top: 80px; right: 10px; width: 150px;
            background: rgba(255,255,255,0.95); border: 2px solid #555; z-index: 9999;
            font-size: 11px; padding: 8px; border-radius: 8px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.4;
        }}
        body.mode-1 #legend-panel {{
            position: fixed; top: 325px; right: 10px; left: auto; bottom: auto; width: 150px; height: auto;
            background: rgba(255,255,255,0.95); border: 2px solid grey; z-index: 9999;
            font-size: 11px; padding: 8px; border-radius: 6px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.4;
        }}
        body.mode-1 .view-vertical {{ display: block; }}
        body.mode-1 .view-horizontal {{ display: none; }}

        /* モード 2: 上下配置（2行以内コンパクト横長）- right: 250px に調整 */
        body.mode-2 #summary-panel {{
            position: fixed; top: 10px; left: 50px; right: 250px; width: auto;
            background: rgba(255,255,255,0.95); border: 2px solid #555; z-index: 9999;
            font-size: 11px; padding: 4px 10px; border-radius: 6px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.3;
        }}
        body.mode-2 #legend-panel {{
            position: fixed; bottom: 10px; left: 10px; right: 10px; width: auto; height: auto;
            background: rgba(255,255,255,0.95); border: 2px solid grey; z-index: 9999;
            font-size: 11px; padding: 4px 10px; border-radius: 6px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.3;
        }}
        body.mode-2 .view-vertical {{ display: none; }}
        body.mode-2 .view-horizontal {{ display: block; }}
    </style>

    <!-- RIDE SUMMARY パネル -->
    <div id="summary-panel">
        <div class="view-vertical">
            <b style="font-size: 12px; color: #333; display: block; border-bottom: 1px solid #ccc; padding-bottom: 4px; margin-bottom: 6px;">🐓 RIDE SUMMARY</b>
            <b>日付:</b> {start_time_jst.strftime('%Y-%m-%d')}<br>
            <b>平均風速:</b> {avg_wind:.1f} m/s<br>
            <b>最大風速:</b> {max_wind:.1f} m/s<br>
            <b>最大体感<br>向かい風:</b> <span style="color:#D32F2F; font-weight:bold;">+{max_headwind:.1f} m/s</span>
            <hr style="border:0; border-top:1px solid #eee; margin:5px 0;">
            <b>風向の内訳:</b><br>
            <div style="display:flex; justify-content:space-between; margin-top:2px;"><span><i style="color:#D32F2F;">■</i> 向かい風:</span> <b>{head_pct:.0f}%</b></div>
            <div style="display:flex; justify-content:space-between;"><span><i style="color:#FFFFFF; -webkit-text-stroke: 1px #888;">■</i> 横風/微風:</span> <b>{cross_pct:.0f}%</b></div>
            <div style="display:flex; justify-content:space-between;"><span><i style="color:#1976D2;">■</i> 追い風:</span> <b>{tail_pct:.0f}%</b></div>
            <hr style="border:0; border-top:1px solid #eee; margin:5px 0;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <b>風恩恵得点:</b>
                <span style="font-size: 14px; font-weight: bold; color: {score_color};">{wind_score}点</span>
            </div>
        </div>

        <div class="view-horizontal">
            <div><b>🐓 RIDE SUMMARY</b> ({start_time_jst.strftime('%Y-%m-%d')}) &nbsp;|&nbsp; <b>平均:</b> {avg_wind:.1f}m/s &nbsp;|&nbsp; <b>最大:</b> {max_wind:.1f}m/s &nbsp;|&nbsp; <b>最大体感向かい風:</b> <span style="color:#D32F2F; font-weight:bold;">+{max_headwind:.1f}m/s</span> &nbsp;|&nbsp; <b>得点:</b> <span style="font-size:12px; font-weight:bold; color:{score_color};">{wind_score}点</span></div>
            <div style="margin-top:2px;"><b>風向内訳:</b> <span style="color:#D32F2F;">■</span>向かい風: <b>{head_pct:.0f}%</b> &nbsp;|&nbsp; <span style="color:#FFFFFF; -webkit-text-stroke:1px #888;">■</span>横風/微風: <b>{cross_pct:.0f}%</b> &nbsp;|&nbsp; <span style="color:#1976D2;">■</span>追い風: <b>{tail_pct:.0f}%</b></div>
        </div>
    </div>

    <!-- 相対風向凡例 パネル -->
    <div id="legend-panel">
        <div class="view-vertical">
            <b>相対風向凡例 (実効風)</b><br>
            <i style="background: #D32F2F; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 強向かい風 (+2m/s〜)<br>
            <i style="background: #FB8C00; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 弱向かい風 (+0.5m/s)<br>
            <i style="background: #FFFFFF; border: 1.5px solid #333333; width: 10px; height: 10px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 横風 / 微風<br>
            <i style="background: #9CCC65; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 弱追い風 (-0.5m/s)<br>
            <i style="background: #2E7D32; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 追い風 (-2.0m/s)<br>
            <i style="background: #1976D2; width: 12px; height: 12px; display: inline-block; margin-right: 5px; border-radius: 2px;"></i> 強追い風 (-3.5m/s〜)
        </div>

        <!-- 横表示モード (基本1行で配置) -->
        <div class="view-horizontal" style="white-space: nowrap; overflow-x: auto;">
            <b>凡例(実効風):</b> &nbsp;
            <i style="background:#D32F2F; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>強向かい風(+2m/s〜) &nbsp;
            <i style="background:#FB8C00; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>弱向かい風(+0.5m/s) &nbsp;
            <i style="background:#FFFFFF; border:1px solid #333; width:9px; height:9px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>横風/微風 &nbsp;
            <i style="background:#9CCC65; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>弱追い風(-0.5m/s) &nbsp;
            <i style="background:#2E7D32; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>追い風(-2.0m/s) &nbsp;
            <i style="background:#1976D2; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>強追い風(-3.5m/s〜)
        </div>
    </div>

    <!-- PNG保存 ＆ レイアウト切替ボタン -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html-to-image/1.11.11/html-to-image.min.js"></script>
    <div style="position: fixed; top: 12px; right: 60px; z-index: 9999; display: flex; flex-direction: column; align-items: flex-end; gap: 5px;">
        <button id="layout-toggle-btn" onclick="toggleLayout()" style="background-color: #1a252f; color: white; border: 2px solid white; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 11px; cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.4); font-family: sans-serif; min-width: 110px; text-align: center;">📐 レイアウト: 標準</button>
        <button id="save-png-btn" onclick="saveAsPNG()" style="background-color: #1a252f; color: white; border: 2px solid white; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 11px; cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.4); font-family: sans-serif; min-width: 110px; text-align: center;">📷 PNG保存</button>
    </div>

    <script>
    document.body.classList.add('mode-0');
    let currentMode = 0;
    const modeNames = ['標準', '右側集約', '上下コンパクト'];

    function toggleLayout() {{
        document.body.classList.remove('mode-' + currentMode);
        currentMode = (currentMode + 1) % 3;
        document.body.classList.add('mode-' + currentMode);
        document.getElementById('layout-toggle-btn').innerText = '📐 レイアウト: ' + modeNames[currentMode];
    }}

    function saveAsPNG() {{
        const btn = document.getElementById('save-png-btn');
        const originalText = btn.innerText;
        btn.innerText = '画像生成中...';
        btn.disabled = true;

        htmlToImage.toPng(document.body, {{ pixelRatio: 2 }})
            .then(function (dataUrl) {{
                const link = document.createElement('a');
                link.download = 'kazamidori_wind_map.png';
                link.href = dataUrl;
                link.click();
                btn.innerText = originalText;
                btn.disabled = false;
            }})
            .catch(function (error) {{
                alert('画像の生成に失敗しました: ' + error);
                btn.innerText = originalText;
                btn.disabled = false;
            }});
    }}
    </script>
    """
    m.get_root().html.add_child(folium.Element(ui_layout_html))

    m.save(output_html)
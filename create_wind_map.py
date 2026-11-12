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


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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
                        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                            continue

                        ts_str = time_node.text.replace('Z', '+00:00')
                        dt_utc = datetime.datetime.fromisoformat(ts_str)
                        if dt_utc.tzinfo is None:
                            dt_utc = dt_utc.replace(tzinfo=datetime.timezone.utc)

                        alt_node = tp.find('ns:AltitudeMeters', ns) if tp.find('ns:AltitudeMeters', ns) is not None else tp.find('AltitudeMeters')
                        alt = float(alt_node.text) if (alt_node is not None and alt_node.text) else None

                        dist_node = tp.find('ns:DistanceMeters', ns) if tp.find('ns:DistanceMeters', ns) is not None else tp.find('DistanceMeters')
                        dist = float(dist_node.text) if (dist_node is not None and dist_node.text) else None

                        power = None
                        for elem in tp.iter():
                            if elem.tag.endswith('Watts') and elem.text:
                                try:
                                    power = float(elem.text)
                                    break
                                except ValueError:
                                    pass

                        records.append({
                            'timestamp': dt_utc,
                            'lat': lat,
                            'lon': lon,
                            'altitude': alt,
                            'distance': dist,
                            'power': power
                        })
                    except Exception:
                        continue
    except Exception as e:
        print(f"[TCX Parse Error] {e}")

    return records


# --- 1-B. FIT (バイナリ) ファイルのダイレクト抽出 ---
def parse_record_bytes(msg_bytes, fields, last_ts_ref, records, is_strict_record=True):
    lat, lon, ts = None, None, None
    alt, dist, power = None, None, None

    if 253 in fields:
        f_off, f_sz, _, end = fields[253]
        if f_off + f_sz <= len(msg_bytes) and f_sz == 4:
            ts = struct.unpack(end + 'I', msg_bytes[f_off:f_off+4])[0]

    if ts is not None and ts > 100000:
        last_ts_ref[0] = ts
    else:
        if last_ts_ref[0] is None:
            return
        last_ts_ref[0] += 1
        ts = last_ts_ref[0]

    if 0 in fields and 1 in fields:
        f_off0, f_sz0, _, end0 = fields[0]
        f_off1, f_sz1, _, end1 = fields[1]
        if f_sz0 == 4 and f_sz1 == 4 and f_off0 + 4 <= len(msg_bytes) and f_off1 + 4 <= len(msg_bytes):
            lat_raw = struct.unpack(end0 + 'i', msg_bytes[f_off0:f_off0+4])[0]
            lon_raw = struct.unpack(end1 + 'i', msg_bytes[f_off1:f_off1+4])[0]
            if lat_raw != 0x7FFFFFFF and lat_raw != 0 and lon_raw != 0x7FFFFFFF and lon_raw != 0:
                lat = lat_raw * (180.0 / 2**31)
                lon = lon_raw * (180.0 / 2**31)

    if (lat is None or lon is None) and 1 in fields and 2 in fields:
        f_off1, f_sz1, _, end1 = fields[1]
        f_off2, f_sz2, _, end2 = fields[2]
        if f_sz1 == 4 and f_sz2 == 4 and f_off1 + 4 <= len(msg_bytes) and f_off2 + 4 <= len(msg_bytes):
            lat_raw = struct.unpack(end1 + 'i', msg_bytes[f_off1:f_off1+4])[0]
            lon_raw = struct.unpack(end2 + 'i', msg_bytes[f_off2:f_off2+4])[0]
            if lat_raw != 0x7FFFFFFF and lat_raw != 0 and lon_raw != 0x7FFFFFFF and lon_raw != 0:
                lat = lat_raw * (180.0 / 2**31)
                lon = lon_raw * (180.0 / 2**31)

    if lat is None or lon is None:
        return
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return

    # 標高・距離・パワーは正規 Record メッセージ (global_mesg_num == 20) のみ抽出
    if is_strict_record:
        # 標高 (field 78: enhanced_altitude, field 2: altitude)
        if 78 in fields:
            f_off, f_sz, _, end = fields[78]
            if f_sz == 4 and f_off + 4 <= len(msg_bytes):
                raw_alt = struct.unpack(end + 'I', msg_bytes[f_off:f_off+4])[0]
                if raw_alt != 0xFFFFFFFF:
                    alt = raw_alt / 5.0 - 500.0
        elif 2 in fields:
            f_off, f_sz, _, end = fields[2]
            if f_sz == 2 and f_off + 2 <= len(msg_bytes):
                raw_alt = struct.unpack(end + 'H', msg_bytes[f_off:f_off+2])[0]
                if raw_alt != 0xFFFF:
                    alt = raw_alt / 5.0 - 500.0

        # 距離 (field 5: distance, 単位: cm -> m)
        if 5 in fields:
            f_off, f_sz, _, end = fields[5]
            if f_sz == 4 and f_off + 4 <= len(msg_bytes):
                raw_dist = struct.unpack(end + 'I', msg_bytes[f_off:f_off+4])[0]
                if raw_dist != 0xFFFFFFFF:
                    dist = raw_dist / 100.0

        # パワー (field 7: power, 単位: W)
        if 7 in fields:
            f_off, f_sz, _, end = fields[7]
            if f_sz == 2 and f_off + 2 <= len(msg_bytes):
                raw_pwr = struct.unpack(end + 'H', msg_bytes[f_off:f_off+2])[0]
                if raw_pwr != 0xFFFF and raw_pwr < 3000:
                    power = float(raw_pwr)

    dt_utc = datetime.datetime.fromtimestamp(ts + 631065600, tz=datetime.timezone.utc)
    if not (2000 <= dt_utc.year <= 2035):
        return

    records.append({
        'timestamp': dt_utc,
        'lat': lat,
        'lon': lon,
        'altitude': alt,
        'distance': dist,
        'power': power
    })


def parse_fit_data(file_path):
    with open(file_path, 'rb') as f:
        data = f.read()

    if len(data) < 12:
        return []

    strict_records = []
    fallback_records = []
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
                        msg_bytes = data[offset : offset + mesg_size]
                        if def_info['is_strict_record']:
                            parse_record_bytes(msg_bytes, def_info['fields'], last_ts_ref, strict_records, True)
                        elif def_info['is_fallback_record']:
                            parse_record_bytes(msg_bytes, def_info['fields'], last_ts_ref, fallback_records, False)
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

                is_strict = (global_mesg_num == 20)
                is_fallback = (not is_strict) and (global_mesg_num not in (0, 18, 19, 21, 23, 34, 49, 72)) and (
                    (0 in field_offsets and 1 in field_offsets) or (1 in field_offsets and 2 in field_offsets)
                )

                definitions[local_id] = {
                    'size': mesg_size,
                    'is_strict_record': is_strict,
                    'is_fallback_record': is_fallback,
                    'fields': field_offsets
                }
                offset = curr
            else:
                offset += 1
                if local_id in definitions:
                    def_info = definitions[local_id]
                    mesg_size = def_info['size']
                    if offset + mesg_size <= block_end:
                        msg_bytes = data[offset : offset + mesg_size]
                        if def_info['is_strict_record']:
                            parse_record_bytes(msg_bytes, def_info['fields'], last_ts_ref, strict_records, True)
                        elif def_info['is_fallback_record']:
                            parse_record_bytes(msg_bytes, def_info['fields'], last_ts_ref, fallback_records, False)
                        offset += mesg_size
                    else:
                        offset += 1
                else:
                    offset += 1

        file_offset = block_end + 2

    return strict_records if strict_records else fallback_records


# --- 1-C. ファイル種別の自動判別と全データ統計算出 ---
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

    full_df = pd.DataFrame(records)
    stats = {
        'total_dist_km': 0.0,
        'elevation_gain': None,
        'np_watts': None,
        'mid_lat': None,
        'mid_lon': None,
        'mid_time': None
    }

    if not full_df.empty:
        full_df['timestamp'] = full_df['timestamp'].dt.tz_convert('Asia/Tokyo')
        full_df = full_df.sort_values('timestamp').reset_index(drop=True)

        # 1. 中間時刻地点（総経過時間の半分）の特定
        start_ts = full_df['timestamp'].iloc[0]
        end_ts = full_df['timestamp'].iloc[-1]
        target_mid_ts = start_ts + (end_ts - start_ts) / 2
        mid_idx = (full_df['timestamp'] - target_mid_ts).abs().idxmin()
        stats['mid_lat'] = float(full_df.loc[mid_idx, 'lat'])
        stats['mid_lon'] = float(full_df.loc[mid_idx, 'lon'])
        stats['mid_time'] = full_df.loc[mid_idx, 'timestamp']

        # 2. 総距離の計算 (km)
        valid_dist = full_df['distance'].dropna()
        dist_km_from_field = 0.0
        if len(valid_dist) >= 2:
            dist_diffs = valid_dist.diff()
            valid_increments = dist_diffs[(dist_diffs > 0) & (dist_diffs < 2000.0)]
            dist_km_from_field = float(valid_increments.sum()) / 1000.0

        if dist_km_from_field > 0.1:
            stats['total_dist_km'] = dist_km_from_field
        else:
            lats = full_df['lat'].values
            lons = full_df['lon'].values
            dist_sum = 0.0
            for idx in range(len(lats) - 1):
                step_km = haversine_km(lats[idx], lons[idx], lats[idx + 1], lons[idx + 1])
                if step_km < 2.0:
                    dist_sum += step_km
            stats['total_dist_km'] = dist_sum

            # 3. 獲得標高の計算 (m) - Strava準拠のスムージング(window=5)＋デッドバンド(1.2m)補正
            valid_alt = full_df['altitude'].dropna()
            if len(valid_alt) >= 10:
                smooth_alt = valid_alt.rolling(window=5, min_periods=1, center=True).mean().values
                gain = 0.0
                ref_alt = smooth_alt[0]
                threshold = 1.2  # サイコン実測値と微調整したい場合は 0.8〜1.5 の範囲で変更
                for alt_val in smooth_alt[1:]:
                    diff = alt_val - ref_alt
                    if diff >= threshold:
                        gain += diff
                        ref_alt = alt_val
                    elif diff <= -threshold:
                        ref_alt = alt_val
                stats['elevation_gain'] = int(round(gain))

        # 4. NP (Normalized Power) の計算 (W) - ゼロパワー補完・オートポーズ除外によるStrava準拠計算
        valid_pwr_raw = full_df['power'].dropna()
        if len(valid_pwr_raw) >= 30 and valid_pwr_raw.mean() > 0:
            pwr_df = full_df[['timestamp', 'power']].copy()
            # 走行中のパワー未記録(NaN)はゼロパワー(0W)として補完
            pwr_df['power'] = pwr_df['power'].fillna(0.0)
            pwr_df = pwr_df.groupby('timestamp', as_index=False)['power'].mean().sort_values('timestamp')

            ts_sec = pwr_df['timestamp'].astype('int64') // 10**9
            pwrs = pwr_df['power'].values

            expanded_pwrs = []
            for idx in range(len(pwrs)):
                expanded_pwrs.append(pwrs[idx])
                if idx < len(pwrs) - 1:
                    dt = int(ts_sec.iloc[idx + 1] - ts_sec.iloc[idx])
                    if 1 < dt <= 5 and pwrs[idx] > 0 and pwrs[idx + 1] > 0:
                        # スマートレコーディング短時間ギャップは前値を維持
                        expanded_pwrs.extend([pwrs[idx]] * (dt - 1))
                    elif 1 < dt <= 15:
                        # 惰性走行・減速中の空白秒は0Wとして補完（15秒超はオートポーズとして除外）
                        expanded_pwrs.extend([0.0] * (dt - 1))

            if len(expanded_pwrs) >= 30:
                pwr_series = pd.Series(expanded_pwrs)
                rolling_30s = pwr_series.rolling(window=30, min_periods=1).mean()
                np_val = float(((rolling_30s ** 4).mean()) ** 0.25)
                if not pd.isna(np_val) and np_val > 0:
                    stats['np_watts'] = int(round(np_val))

        # 矢印描画用のダウンサンプリング
        total_count = len(full_df)
        if total_count <= 500:
            target_arrows = 60
        elif total_count <= 2000:
            target_arrows = 100
        else:
            target_arrows = 180

        step = max(1, total_count // target_arrows)
        df = full_df.iloc[::step].copy().reset_index(drop=True)
    else:
        df = full_df

    return df, stats


# --- 2. 気象データ取得 ---
def fetch_wind_data(lat, lon, start_datetime, end_datetime=None):
    if end_datetime is None:
        end_datetime = start_datetime

    start_str = start_datetime.strftime('%Y-%m-%d')
    end_str = end_datetime.strftime('%Y-%m-%d')
    today = datetime.datetime.now(start_datetime.tzinfo).date()
    days_diff = (today - end_datetime.date()).days

    if 0 <= days_diff <= 7:
        base_url = "https://api.open-meteo.com/v1/forecast"
    else:
        base_url = "https://archive-api.open-meteo.com/v1/archive"

    url = (
        f"{base_url}?"
        f"latitude={lat:.4f}&longitude={lon:.4f}&start_date={start_str}&end_date={end_str}"
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
                f"latitude={lat:.4f}&longitude={lon:.4f}&start_date={start_str}&end_date={end_str}"
                f"&hourly=wind_speed_10m,wind_direction_10m"
                f"&wind_speed_unit=ms&timezone=Asia%2FTokyo"
            )
            res_json = requests.get(archive_url, verify=False, timeout=10).json()
            hourly = res_json.get('hourly', {})
            raw_times = hourly.get('time', [])
            raw_speeds = hourly.get('wind_speed_10m', [])
            raw_dirs = hourly.get('wind_direction_10m', [])

        if not raw_times:
            raise ValueError(f"Empty wind response for {start_str}..{end_str}")

        times = pd.to_datetime(raw_times)
        if times.tz is None:
            times = times.tz_localize('Asia/Tokyo')
        else:
            times = times.tz_convert('Asia/Tokyo')

        wind_speeds = raw_speeds
        wind_dirs = raw_dirs

    except Exception as e:
        print(f"[Error] Failed to fetch wind data ({start_str}): {e}")
        times = pd.date_range(start=f"{start_str} 00:00", periods=24, freq='h', tz='Asia/Tokyo')
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
    df, stats = load_activity_data(fit_path)
    if df.empty:
        raise ValueError("有効な位置データが見つかりませんでした。")

    start_time_jst = df['timestamp'].iloc[0]
    end_time_jst = df['timestamp'].iloc[-1]
    mid_lat = df['lat'].mean()
    mid_lon = df['lon'].mean()
    wind_df = fetch_wind_data(mid_lat, mid_lon, start_time_jst, end_time_jst)

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

    # --- スタート・中間（経過時間半分）・ゴールの3箇所に時刻吹き出し付きマーカー配置 ---
    start_pos = [df['lat'].iloc[0], df['lon'].iloc[0]]
    goal_pos = [df['lat'].iloc[-1], df['lon'].iloc[-1]]

    start_icon_html = """
    <div style="background-color: #2E7D32; color: white; border: 2px solid white; border-radius: 50%; width: 26px; height: 26px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.5); font-family: sans-serif;">S</div>
    """
    mid_icon_html = """
    <div style="background-color: #EF6C00; color: white; border: 2px solid white; border-radius: 50%; width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 11px; box-shadow: 0 2px 6px rgba(0,0,0,0.5); font-family: sans-serif;">M</div>
    """
    goal_icon_html = """
    <div style="background-color: #C62828; color: white; border: 2px solid white; border-radius: 50%; width: 26px; height: 26px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.5); font-family: sans-serif;">G</div>
    """

    folium.Marker(
        location=start_pos,
        icon=folium.DivIcon(html=start_icon_html, icon_size=(26, 26), icon_anchor=(13, 13)),
        tooltip=folium.Tooltip(
            f"START {start_time_jst.strftime('%H:%M')}",
            permanent=True,
            direction="top",
            offset=[0, -14],
            class_name="time-bubble"
        )
    ).add_to(m)

    if stats['mid_lat'] is not None and stats['mid_lon'] is not None and stats['mid_time'] is not None:
        folium.Marker(
            location=[stats['mid_lat'], stats['mid_lon']],
            icon=folium.DivIcon(html=mid_icon_html, icon_size=(22, 22), icon_anchor=(11, 11)),
            tooltip=folium.Tooltip(
                f"中間 {stats['mid_time'].strftime('%H:%M')}",
                permanent=True,
                direction="top",
                offset=[0, -12],
                class_name="time-bubble"
            )
        ).add_to(m)

    folium.Marker(
        location=goal_pos,
        icon=folium.DivIcon(html=goal_icon_html, icon_size=(26, 26), icon_anchor=(13, 13)),
        tooltip=folium.Tooltip(
            f"GOAL {end_time_jst.strftime('%H:%M')}",
            permanent=True,
            direction="top",
            offset=[0, -14],
            class_name="time-bubble"
        )
    ).add_to(m)

    clean_speeds = sample_df['wind_speed'].dropna()
    avg_wind = clean_speeds.mean() if not clean_speeds.empty else 0.0
    max_wind = clean_speeds.max() if not clean_speeds.empty else 0.0
    max_headwind = max(eff_headwinds) if eff_headwinds else 0.0

    head_pct = (headwind_count / total_segments) * 100
    cross_pct = (crosswind_count / total_segments) * 100
    tail_pct = (tailwind_count / total_segments) * 100

    wind_score = int(round(head_pct * 0.125 + cross_pct * 0.5 + tail_pct * 1.05))
    wind_score = max(0, min(100, wind_score))

    if wind_score >= 80:
        score_color = "#2E7D32"
    elif wind_score >= 50:
        score_color = "#1976D2"
    else:
        score_color = "#D32F2F"

    dist_str = f"{stats['total_dist_km']:.1f} km"
    elev_str = f"{stats['elevation_gain']:,} m" if stats['elevation_gain'] is not None else "-- m"
    np_str = f"{stats['np_watts']} W" if stats['np_watts'] is not None else "-- W"

    ui_layout_html = f"""
    <style>
        .leaflet-tooltip.time-bubble {{
            background-color: rgba(255, 255, 255, 0.96);
            border: 1.5px solid #333;
            border-radius: 5px;
            color: #111;
            font-weight: bold;
            font-size: 11px;
            padding: 2px 6px;
            box-shadow: 0 2px 5px rgba(0, 0, 0, 0.35);
            font-family: sans-serif;
            white-space: nowrap;
        }}
        .leaflet-tooltip-top.time-bubble::before {{
            border-top-color: #333;
        }}

        .leaflet-top.leaflet-right {{
            top: 10px !important;
            z-index: 10000 !important;
        }}

        body:has(.leaflet-control-layers:hover) #control-buttons {{
            opacity: 0;
            pointer-events: none;
        }}

        #control-buttons {{
            transition: opacity 0.2s ease;
        }}

        /* 標準レイアウト(mode-0)の横幅を right: 185px まで拡張し折り返しを防止 */
        body.mode-0 #summary-panel {{
            position: fixed; top: 10px; left: 50px; right: 185px; width: auto;
            background: rgba(255,255,255,0.95); border: 2px solid #555; z-index: 9999;
            font-size: 11px; padding: 4px 8px; border-radius: 6px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.3;
        }}
        body.mode-0 #legend-panel {{
            position: fixed; bottom: 10px; left: 10px; right: 10px; width: auto; height: auto;
            background: rgba(255,255,255,0.95); border: 2px solid grey; z-index: 9999;
            font-size: 11px; padding: 4px 10px; border-radius: 6px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.3;
        }}
        body.mode-0 .view-vertical {{ display: none; }}
        body.mode-0 .view-horizontal {{ display: block; }}

        body.mode-1 #summary-panel {{
            position: fixed; top: 80px; right: 10px; width: 155px;
            background: rgba(255,255,255,0.95); border: 2px solid #555; z-index: 9999;
            font-size: 11px; padding: 8px; border-radius: 8px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.4;
        }}
        body.mode-1 #legend-panel {{
            position: fixed; top: 385px; right: 10px; left: auto; bottom: auto; width: 155px; height: auto;
            background: rgba(255,255,255,0.95); border: 2px solid grey; z-index: 9999;
            font-size: 11px; padding: 8px; border-radius: 6px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.4;
        }}
        body.mode-1 .view-vertical {{ display: block; }}
        body.mode-1 .view-horizontal {{ display: none; }}

        body.mode-2 #summary-panel,
        body.mode-2 #legend-panel {{
            display: none !important;
        }}

        @media (max-width: 600px) {{
            body.mode-0 #summary-panel {{
                left: 10px; right: 180px; font-size: 9px; padding: 3px 6px;
            }}
            body.mode-0 #legend-panel {{
                font-size: 9px; padding: 3px 6px;
            }}
        }}
    </style>

    <div id="summary-panel">
        <div class="view-vertical">
            <b style="font-size: 12px; color: #333; display: block; border-bottom: 1px solid #ccc; padding-bottom: 4px; margin-bottom: 6px;">🐓 RIDE SUMMARY</b>
            <b>日付:</b> {start_time_jst.strftime('%Y-%m-%d')}<br>
            <b>距離:</b> {dist_str}<br>
            <b>獲得標高:</b> {elev_str}<br>
            <b>NP:</b> {np_str}<br>
            <hr style="border:0; border-top:1px solid #eee; margin:4px 0;">
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
            <div><b>🐓 RIDE SUMMARY</b> ({start_time_jst.strftime('%Y-%m-%d')}) | <b>距離:</b> {dist_str} | <b>獲得:</b> {elev_str} | <b>NP:</b> {np_str} | <b>得点:</b> <span style="font-size:12px; font-weight:bold; color:{score_color};">{wind_score}点</span></div>
            <div style="margin-top:2px;"><b>平均風速:</b> {avg_wind:.1f}m/s | <b>最大:</b> {max_wind:.1f}m/s | <b>最大体感向かい風:</b> <span style="color:#D32F2F; font-weight:bold;">+{max_headwind:.1f}m/s</span> | <b>内訳:</b> <span style="color:#D32F2F;">■</span>向かい風:<b>{head_pct:.0f}%</b> <span style="color:#FFFFFF; -webkit-text-stroke:1px #888;">■</span>横風/微風:<b>{cross_pct:.0f}%</b> <span style="color:#1976D2;">■</span>追い風:<b>{tail_pct:.0f}%</b></div>
        </div>
    </div>

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

        <div class="view-horizontal" style="white-space: nowrap;">
            <b>凡例(実効風):</b> &nbsp;
            <i style="background:#D32F2F; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>強向かい風(+2m/s〜) &nbsp;
            <i style="background:#FB8C00; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>弱向かい風(+0.5m/s) &nbsp;
            <i style="background:#FFFFFF; border:1px solid #333; width:9px; height:9px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>横風/微風 &nbsp;
            <i style="background:#9CCC65; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>弱追い風(-0.5m/s) &nbsp;
            <i style="background:#2E7D32; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>追い風(-2.0m/s) &nbsp;
            <i style="background:#1976D2; width:10px; height:10px; display:inline-block; margin-right:2px; vertical-align:middle;"></i>強追い風(-3.5m/s〜)
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/html-to-image/1.11.11/html-to-image.min.js"></script>
    <div id="control-buttons" style="position: fixed; top: 12px; right: 60px; z-index: 9999; display: flex; flex-direction: column; align-items: flex-end; gap: 5px;">
        <button id="layout-toggle-btn" onclick="toggleLayout()" style="background-color: #1a252f; color: white; border: 2px solid white; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 11px; cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.4); font-family: sans-serif; min-width: 110px; text-align: center;">📐 レイアウト: 標準</button>
        <button id="save-png-btn" onclick="saveAsPNG()" style="background-color: #1a252f; color: white; border: 2px solid white; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 11px; cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.4); font-family: sans-serif; min-width: 110px; text-align: center;">📷 PNG保存</button>
    </div>

    <script>
    document.body.classList.add('mode-0');
    let currentMode = 0;
    const modeNames = ['標準', '右側集約', '表示なし'];

    function toggleLayout() {{
        document.body.classList.remove('mode-' + currentMode);
        currentMode = (currentMode + 1) % 3;
        document.body.classList.add('mode-' + currentMode);
        document.getElementById('layout-toggle-btn').innerText = '📐 レイアウト: ' + modeNames[currentMode];
    }}

    function saveAsPNG() {{
        const btnContainer = document.getElementById('control-buttons');
        const btn = document.getElementById('save-png-btn');
        const originalText = btn.innerText;

        btnContainer.style.display = 'none';

        htmlToImage.toPng(document.body, {{
            pixelRatio: 2,
            filter: (node) => node.id !== 'control-buttons'
        }})
            .then(function (dataUrl) {{
                const link = document.createElement('a');
                link.download = 'kazamidori_wind_map.png';
                link.href = dataUrl;
                link.click();
            }})
            .catch(function (error) {{
                alert('画像の生成に失敗しました: ' + error);
            }})
            .finally(function () {{
                btnContainer.style.display = 'flex';
                btn.innerText = originalText;
                btn.disabled = false;
            }});
    }}
    </script>
    """
    m.get_root().html.add_child(folium.Element(ui_layout_html))

    m.save(output_html)
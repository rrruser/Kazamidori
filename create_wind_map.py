import json
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

    # 標高 (field 78: enhanced_altitude を優先し、無効値なら field 2: altitude を参照)
    if 78 in fields:
        f_off, f_sz, _, end = fields[78]
        if f_sz == 4 and f_off + 4 <= len(msg_bytes):
            raw_alt = struct.unpack(end + 'I', msg_bytes[f_off:f_off+4])[0]
            if raw_alt != 0xFFFFFFFF:
                cand_alt = raw_alt / 5.0 - 500.0
                if -500.0 <= cand_alt <= 9000.0:
                    alt = cand_alt
    if alt is None and 2 in fields:
        f_off, f_sz, _, end = fields[2]
        if f_sz == 2 and f_off + 2 <= len(msg_bytes):
            raw_alt = struct.unpack(end + 'H', msg_bytes[f_off:f_off+2])[0]
            if raw_alt != 0xFFFF:
                cand_alt = raw_alt / 5.0 - 500.0
                if -500.0 <= cand_alt <= 9000.0:
                    alt = cand_alt

    # パワー (field 7: power, 単位: W)
    if 7 in fields:
        f_off, f_sz, _, end = fields[7]
        if f_sz == 2 and f_off + 2 <= len(msg_bytes):
            raw_pwr = struct.unpack(end + 'H', msg_bytes[f_off:f_off+2])[0]
            if raw_pwr != 0xFFFF and raw_pwr < 3000:
                power = float(raw_pwr)

    # 距離 (field 5: distance, 単位: cm -> m) は正規 Record メッセージのみ抽出
    if is_strict_record and 5 in fields:
        f_off, f_sz, _, end = fields[5]
        if f_sz == 4 and f_off + 4 <= len(msg_bytes):
            raw_dist = struct.unpack(end + 'I', msg_bytes[f_off:f_off+4])[0]
            if raw_dist != 0xFFFFFFFF:
                dist = raw_dist / 100.0

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

        # 3. 獲得標高の計算 (m) - スムージング(window=5)＋デッドバンド(0.7m)補正
        valid_alt = full_df['altitude'].dropna()
        if len(valid_alt) >= 10:
            smooth_alt = valid_alt.rolling(window=5, min_periods=1, center=True).mean().values
            gain = 0.0
            ref_alt = smooth_alt[0]
            threshold = 0.7
            for alt_val in smooth_alt[1:]:
                diff = alt_val - ref_alt
                if diff >= threshold:
                    gain += diff
                    ref_alt = alt_val
                elif diff <= -threshold:
                    ref_alt = alt_val
            stats['elevation_gain'] = int(round(gain))

        # 4. NP (Normalized Power) の計算 (W) - ゼロパワー補完・オートポーズ除外
        valid_pwr_raw = full_df['power'].dropna()
        if len(valid_pwr_raw) >= 30 and valid_pwr_raw.mean() > 0:
            pwr_df = full_df[['timestamp', 'power']].copy()
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
                        expanded_pwrs.extend([pwrs[idx]] * (dt - 1))
                    elif 1 < dt <= 15:
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
def _extract_valid_wind_hourly(res_json):
    hourly = res_json.get('hourly', {})
    raw_times = hourly.get('time', [])
    raw_speeds = hourly.get('wind_speed_10m', [])
    raw_dirs = hourly.get('wind_direction_10m', [])
    if not raw_times or not raw_speeds:
        return None
    if not any(s is not None for s in raw_speeds):
        return None
    return raw_times, raw_speeds, raw_dirs


def fetch_wind_data(lat, lon, start_datetime, end_datetime=None):
    if end_datetime is None:
        end_datetime = start_datetime

    start_str = start_datetime.strftime('%Y-%m-%d')
    end_str = end_datetime.strftime('%Y-%m-%d')
    today = datetime.datetime.now(start_datetime.tzinfo).date()
    days_diff = (today - start_datetime.date()).days

    candidate_urls = []
    if 0 <= days_diff <= 6:
        candidate_urls.append(
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat:.4f}&longitude={lon:.4f}&past_days=7&forecast_days=2"
            f"&hourly=wind_speed_10m,wind_direction_10m"
            f"&wind_speed_unit=ms&timezone=Asia%2FTokyo"
        )
        candidate_urls.append(
            f"https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat:.4f}&longitude={lon:.4f}&start_date={start_str}&end_date={end_str}"
            f"&hourly=wind_speed_10m,wind_direction_10m"
            f"&wind_speed_unit=ms&timezone=Asia%2FTokyo"
        )
    else:
        candidate_urls.append(
            f"https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat:.4f}&longitude={lon:.4f}&start_date={start_str}&end_date={end_str}"
            f"&hourly=wind_speed_10m,wind_direction_10m"
            f"&wind_speed_unit=ms&timezone=Asia%2FTokyo"
        )
        candidate_urls.append(
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat:.4f}&longitude={lon:.4f}&past_days=14&forecast_days=2"
            f"&hourly=wind_speed_10m,wind_direction_10m"
            f"&wind_speed_unit=ms&timezone=Asia%2FTokyo"
        )

    extracted = None
    for url in candidate_urls:
        try:
            response = requests.get(url, verify=False, timeout=20)
            if response.status_code == 200:
                extracted = _extract_valid_wind_hourly(response.json())
                if extracted is not None:
                    break
        except Exception as e:
            print(f"[Warn] Wind API request failed ({url}): {e}")

    if extracted is not None:
        raw_times, raw_speeds, raw_dirs = extracted
        times = pd.to_datetime(raw_times)
        if times.tz is None:
            times = times.tz_localize('Asia/Tokyo')
        else:
            times = times.tz_convert('Asia/Tokyo')

        wind_df = pd.DataFrame({
            'time': times,
            'wind_speed': pd.to_numeric(pd.Series(raw_speeds), errors='coerce'),
            'wind_dir': pd.to_numeric(pd.Series(raw_dirs), errors='coerce')
        })
        wind_df['wind_speed'] = wind_df['wind_speed'].ffill().bfill().fillna(0.0)
        wind_df['wind_dir'] = wind_df['wind_dir'].ffill().bfill().fillna(0.0)
    else:
        print(f"[Error] All wind data sources returned empty for {start_str}..{end_str}")
        times = pd.date_range(start=f"{start_str} 00:00", periods=24, freq='h', tz='Asia/Tokyo')
        wind_df = pd.DataFrame({
            'time': times,
            'wind_speed': [0.0] * 24,
            'wind_dir': [0.0] * 24
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


# --- 4-B. 6種類の風区分円グラフSVG生成 ---
def make_wind_pie_svg(category_specs, total_segments, size):
    cx = size / 2.0
    cy = size / 2.0
    r = (size / 2.0) - 1.5
    paths = []

    active_items = [(label, color, count) for label, color, count in category_specs if count > 0]
    if not active_items or total_segments <= 0:
        paths.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="#EEEEEE" stroke="#666666" stroke-width="0.8"/>')
    elif len(active_items) == 1:
        label, color, count = active_items[0]
        paths.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{color}" stroke="#555555" stroke-width="0.8">'
            f'<title>{label}: 100%</title></circle>'
        )
    else:
        current_angle = -90.0
        for label, color, count in active_items:
            ratio = count / total_segments
            sweep = ratio * 360.0
            end_angle = current_angle + sweep

            rad1 = math.radians(current_angle)
            rad2 = math.radians(end_angle)
            x1 = cx + r * math.cos(rad1)
            y1 = cy + r * math.sin(rad1)
            x2 = cx + r * math.cos(rad2)
            y2 = cy + r * math.sin(rad2)
            large_arc = 1 if sweep > 180.0 else 0
            pct = int(round(ratio * 100))

            d = f"M {cx:.2f} {cy:.2f} L {x1:.2f} {y1:.2f} A {r:.2f} {r:.2f} 0 {large_arc} 1 {x2:.2f} {y2:.2f} Z"
            paths.append(
                f'<path d="{d}" fill="{color}" stroke="#555555" stroke-width="0.7" stroke-linejoin="round">'
                f'<title>{label}: {pct}%</title></path>'
            )
            current_angle = end_angle

    inner = "".join(paths)
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'style="display:block; flex-shrink:0; filter: drop-shadow(0 1px 2px rgba(0,0,0,0.2));">{inner}</svg>'
    )


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
    cat_counts = {
        "強向かい風": 0,
        "弱向かい風": 0,
        "横風/微風": 0,
        "弱追い風": 0,
        "追い風": 0,
        "強追い風": 0,
    }

    sample_list = sample_df.to_dict('records')
    total_segments = max(1, len(sample_list) - 1)

    # ルートの道 (PolyLine) は全区間そのまま描画
    for i in range(len(sample_list) - 1):
        p1 = sample_list[i]
        p2 = sample_list[i+1]

        heading = calculate_bearing(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
        wind_dir = p1['wind_dir'] if not pd.isna(p1['wind_dir']) else 0
        wind_speed_ms = float(p1['wind_speed']) if not pd.isna(p1['wind_speed']) else 0.0

        segment_color, _, _, eff_headwind, wind_label = get_relative_wind_info(wind_dir, wind_speed_ms, heading)
        eff_headwinds.append(eff_headwind)
        cat_counts[wind_label] += 1

        seg_coords = [(p1['lat'], p1['lon']), (p2['lat'], p2['lon'])]
        folium.PolyLine(
            seg_coords,
            color=segment_color,
            weight=6,
            opacity=0.85,
            popup=f"区間状況: {wind_label}<br>実効向かい風: {eff_headwind:+.1f} m/s<br>進行方向: {heading:.0f}°"
        ).add_to(m)

    # 風向矢印データを収集し、拡大率に応じた動的グループ化用のJSONを生成
    arrow_points = []
    for i, row in sample_df.iterrows():
        wind_dir = float(row['wind_dir']) if not pd.isna(row['wind_dir']) else 0.0
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

        arrow_angle = (wind_dir + 180) % 360

        arrow_points.append({
            'lat': float(row['lat']),
            'lon': float(row['lon']),
            'wind_dir': round(wind_dir, 1),
            'wind_speed': round(wind_speed_ms, 1),
            'wind_label': wind_label,
            'arrow_fill': arrow_fill,
            'arrow_stroke': arrow_stroke,
            'eff_headwind': round(float(eff_headwind), 1),
            'time_str': row['timestamp'].strftime('%H:%M'),
            'size': int(size),
            'arrow_angle': round(float(arrow_angle), 1)
        })

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

    head_pct = ((cat_counts["強向かい風"] + cat_counts["弱向かい風"]) / total_segments) * 100
    cross_pct = (cat_counts["横風/微風"] / total_segments) * 100
    tail_pct = ((cat_counts["弱追い風"] + cat_counts["追い風"] + cat_counts["強追い風"]) / total_segments) * 100

    wind_score = int(round(head_pct * 0.125 + cross_pct * 0.5 + tail_pct * 1.05))
    wind_score = max(0, min(100, wind_score))

    if wind_score >= 80:
        score_color = "#2E7D32"
    elif wind_score >= 50:
        score_color = "#1976D2"
    else:
        score_color = "#D32F2F"

    category_specs = [
        ("強向かい風", "#D32F2F", cat_counts["強向かい風"]),
        ("弱向かい風", "#FB8C00", cat_counts["弱向かい風"]),
        ("横風/微風", "#FFFFFF", cat_counts["横風/微風"]),
        ("弱追い風", "#9CCC65", cat_counts["弱追い風"]),
        ("追い風", "#2E7D32", cat_counts["追い風"]),
        ("強追い風", "#1976D2", cat_counts["強追い風"]),
    ]
    pie_svg_vertical = make_wind_pie_svg(category_specs, total_segments, 76)
    pie_svg_horizontal = make_wind_pie_svg(category_specs, total_segments, 38)

    pie_legend_vertical_items = []
    for label, color, count in category_specs:
        pct = int(round((count / total_segments) * 100))
        border_css = "border: 1px solid #555;" if color == "#FFFFFF" else ""
        pie_legend_vertical_items.append(
            f'<div style="display:flex; align-items:center; justify-content:space-between; font-size:9px; line-height:1.25;">'
            f'<span><i style="background:{color}; {border_css} width:7px; height:7px; display:inline-block; margin-right:3px; border-radius:1px;"></i>{label}</span>'
            f'<b>{pct}%</b></div>'
        )
    pie_legend_vertical_html = "".join(pie_legend_vertical_items)

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

        body.mode-0 #summary-panel {{
            position: fixed; top: 10px; left: 50px; right: 228px; width: auto;
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
        body.mode-0 .view-horizontal {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
        body.mode-0 #legend-panel .view-horizontal {{ display: block; }}

        body.mode-1 #summary-panel {{
            position: fixed; top: 80px; right: 10px; width: 155px;
            background: rgba(255,255,255,0.95); border: 2px solid #555; z-index: 9999;
            font-size: 11px; padding: 8px; border-radius: 8px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
            font-family: sans-serif; line-height: 1.4;
        }}
        body.mode-1 #legend-panel {{
            position: fixed; top: 430px; right: 10px; left: auto; bottom: auto; width: 155px; height: auto;
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
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <b>風恩恵得点:</b>
                <span style="font-size: 14px; font-weight: bold; color: {score_color};">{wind_score}点</span>
            </div>
            <hr style="border:0; border-top:1px solid #eee; margin:5px 0;">
            <div style="display:flex; flex-direction:column; align-items:center; gap:5px;">
                {pie_svg_vertical}
                <div style="width:100%; display:flex; flex-direction:column; gap:1px;">
                    {pie_legend_vertical_html}
                </div>
            </div>
        </div>

        <div class="view-horizontal">
            <div style="min-width:0;">
                <div><b>🐓 RIDE SUMMARY</b> ({start_time_jst.strftime('%Y-%m-%d')}) | <b>距離:</b> {dist_str} | <b>獲得:</b> {elev_str} | <b>NP:</b> {np_str} | <b>風恩恵得点:</b> <span style="font-size:12px; font-weight:bold; color:{score_color};">{wind_score}点</span></div>
                <div style="margin-top:2px;"><b>平均風速:</b> {avg_wind:.1f}m/s | <b>最大:</b> {max_wind:.1f}m/s | <b>最大体感向かい風:</b> <span style="color:#D32F2F; font-weight:bold;">+{max_headwind:.1f}m/s</span></div>
            </div>
            {pie_svg_horizontal}
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

    # 拡大率に応じて連続する同じ風量の矢印(2つ以上)を1つに統合するスクリプト（バッジなし）
    arrow_cluster_js = """
    <script>
    (function() {
        const arrowData = __ARROW_DATA_JSON__;

        function isSameArrow(a, b) {
            return a.wind_speed === b.wind_speed &&
                   a.wind_dir === b.wind_dir &&
                   a.wind_label === b.wind_label;
        }

        function createArrowMarker(pt, mergedCount, startTime, endTime) {
            const size = pt.size;
            const anchor = Math.floor(size / 2);

            const svgIcon = `
                <div style="width: ${size}px; height: ${size}px; background: transparent; display: flex; align-items: center; justify-content: center;">
                    <div style="transform: rotate(${pt.arrow_angle}deg); width: ${size}px; height: ${size}px; display: flex; align-items: center; justify-content: center;">
                        <svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="filter: drop-shadow(0px 1px 2px rgba(0,0,0,0.5));">
                            <path d="M12 2L4 21L12 17L20 21L12 2Z" fill="${pt.arrow_fill}" stroke="${pt.arrow_stroke}" stroke-width="1" stroke-linejoin="round"/>
                        </svg>
                    </div>
                </div>
            `;
            const icon = L.divIcon({
                html: svgIcon,
                className: '',
                iconSize: [size, size],
                iconAnchor: [anchor, anchor]
            });

            const timeLabel = mergedCount >= 2
                ? `${startTime}〜${endTime} (JST) [${mergedCount}点統合]`
                : `${pt.time_str} (JST)`;
            const signStr = pt.eff_headwind >= 0 ? `+${pt.eff_headwind.toFixed(1)}` : pt.eff_headwind.toFixed(1);

            const popupHtml = `
                <div style="white-space: nowrap; font-size: 11px; line-height: 1.5; padding: 2px;">
                    <b>時刻:</b> ${timeLabel}<br>
                    <b>風向:</b> ${Math.round(pt.wind_dir)}° / <b>風速:</b> ${pt.wind_speed.toFixed(1)} m/s<br>
                    <b>体感向かい風:</b> <span style="font-weight:bold;">${signStr} m/s</span>
                </div>
            `;

            return L.marker([pt.lat, pt.lon], { icon: icon }).bindPopup(popupHtml, { maxWidth: 300 });
        }

        function initWindArrows() {
            if (typeof __MAP_VAR_NAME__ === 'undefined' || !__MAP_VAR_NAME__) {
                setTimeout(initWindArrows, 50);
                return;
            }

            const mapObj = __MAP_VAR_NAME__;
            const arrowLayerGroup = L.layerGroup().addTo(mapObj);

            function renderWindArrows() {
                arrowLayerGroup.clearLayers();
                if (!arrowData || arrowData.length === 0) return;

                const screenPts = arrowData.map(pt => mapObj.latLngToContainerPoint([pt.lat, pt.lon]));

                const maxStepPx = 90;
                const maxSpanPx = 240;

                let i = 0;
                while (i < arrowData.length) {
                    let j = i;
                    let accumulatedSpan = 0;

                    while (j + 1 < arrowData.length && isSameArrow(arrowData[i], arrowData[j + 1])) {
                        const stepPx = screenPts[j].distanceTo(screenPts[j + 1]);
                        if (stepPx >= maxStepPx) {
                            break;
                        }
                        if (accumulatedSpan + stepPx > maxSpanPx && (j - i + 1) >= 2) {
                            break;
                        }
                        accumulatedSpan += stepPx;
                        j++;
                    }

                    const runLength = j - i + 1;
                    if (runLength >= 2) {
                        const midIdx = Math.floor((i + j) / 2);
                        const marker = createArrowMarker(
                            arrowData[midIdx],
                            runLength,
                            arrowData[i].time_str,
                            arrowData[j].time_str
                        );
                        arrowLayerGroup.addLayer(marker);
                        i = j + 1;
                    } else {
                        const marker = createArrowMarker(arrowData[i], 1, arrowData[i].time_str, arrowData[i].time_str);
                        arrowLayerGroup.addLayer(marker);
                        i++;
                    }
                }
            }

            renderWindArrows();
            mapObj.on('zoomend', renderWindArrows);
            mapObj.on('resize', renderWindArrows);
        }

        initWindArrows();
    })();
    </script>
    """
    arrow_cluster_js = arrow_cluster_js.replace('__ARROW_DATA_JSON__', json.dumps(arrow_points, ensure_ascii=False))
    arrow_cluster_js = arrow_cluster_js.replace('__MAP_VAR_NAME__', m.get_name())
    m.get_root().html.add_child(folium.Element(arrow_cluster_js))

    m.save(output_html)
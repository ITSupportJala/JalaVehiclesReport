from flask import Flask, request, render_template
import requests
import time
import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import pandas as pd
from dotenv import load_dotenv
import pytz
import io
from flask import send_file

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

logging.getLogger('werkzeug').disabled = True

# =========================== KONFIGURASI & KONSTANTA ===========================
load_dotenv()
EXCEL_FILE = 'data_kendaraan.xlsx'
EFFICIENCY_KM_PER_LITER = 15
GWP_CH4 = 29.8
GWP_N2O = 273

gps_config = {
    'username': os.getenv('GPS_USERNAME'),
    'password': os.getenv('GPS_PASSWORD')
}

_token_cache = {"token": None, "expires_at": 0}
app = Flask(__name__, static_folder='static', template_folder='templates')


@app.after_request
def custom_log(response):
    jakarta = pytz.timezone("Asia/Jakarta")
    now = datetime.now(jakarta).strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[{now}] {request.remote_addr} {request.method} {request.path} {response.status_code}"
    )
    return response


# =========================== TOKEN HANDLER ===========================
def get_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    try:
        response = requests.post(
            "https://portal.gps.id/backend/seen/public/login",
            json={
                "username": gps_config['username'],
                "password": gps_config['password']
            },
            headers={"Content-Type": "application/json"})
        response.raise_for_status()
        token = response.json().get("message", {}).get("data", {}).get("token")
        if token:
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + (55 * 60)
            return token
    except requests.RequestException as e:
        print(f"Error getting token: {e}")
        return None


cached_token = None
token_expiry = None


def get_token_cached():
    global cached_token, token_expiry
    now = datetime.utcnow()
    if cached_token and token_expiry and now < token_expiry:
        return cached_token
    # Ambil token baru
    token = get_token()
    if token:
        cached_token = token
        token_expiry = now + timedelta(minutes=10)
    return token

# =========================== GPS API FUNCTIONS ===========================
def get_vehicle_data(token):
    try:
        res = requests.get("https://portal.gps.id/backend/seen/public/vehicle",
                           headers={"Authorization": f"Bearer {token}"})
        res.raise_for_status()
        return res.json().get("message", {}).get("data", [])
    except requests.RequestException as e:
        print(f"Error getting vehicle data: {e}")
        return []


LAST_REQUEST = 0
RATE_LIMIT_DELAY = 2  # jeda minimal 2 detik antar request

def safe_request(*args, **kwargs):
    global LAST_REQUEST
    now = time.time()
    if now - LAST_REQUEST < RATE_LIMIT_DELAY:
        time.sleep(RATE_LIMIT_DELAY - (now - LAST_REQUEST))
    res = requests.get(*args, **kwargs)
    LAST_REQUEST = time.time()
    return res

def get_history_data(token, imei, start_date, end_date):
    all_data = []
    per_page = 10000

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    current_start = start_dt
    while current_start <= end_dt:
        current_end = min(current_start + timedelta(days=3), end_dt)
        page = 1
        chunk_data = []

        while True:
            try:
                res = safe_request(
                    "https://portal.gps.id/backend/seen/public/report/history",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "device": imei,
                        "start": current_start.strftime("%Y-%m-%d 00:00:00"),
                        "end": current_end.strftime("%Y-%m-%d 23:59:59"),
                        "page": page,
                        "per_page": per_page
                    },
                    timeout=30
                )

                # Kalau API balas 429 -> tunggu lalu ulangi request
                if res.status_code == 429:
                    wait_time = int(res.headers.get("Retry-After", 60))
                    print(f"⚠️ Rate limit! tunggu {wait_time} detik...")
                    time.sleep(wait_time)
                    continue

                res.raise_for_status()
                json_data = res.json()
                message = json_data.get("message", {})
                data = message.get("data", [])

                if not data:
                    break

                chunk_data.extend(data)
                all_data.extend(data)

                if page >= message.get("last_page", page):
                    break
                page += 1

                # jeda kecil antar halaman biar ga overload
                time.sleep(1)

            except Exception as e:
                print(
                    f"❌ Error page {page} ({current_start.date()} - {current_end.date()}): {e}"
                )
                # kasih jeda sebelum lanjut biar ga langsung fail total
                time.sleep(5)
                break

        print(f"  📆 {current_start.date()} - {current_end.date()} → {len(chunk_data)} data")

        # jeda antar range tanggal biar lebih aman
        time.sleep(2)

        current_start = current_end + timedelta(days=1)

    return all_data



# =========================== HELPER FUNCTION ===========================
def load_active_vehicles():
    if not os.path.exists(EXCEL_FILE):
        raise FileNotFoundError("File Excel tidak ditemukan.")
    df = pd.read_excel(EXCEL_FILE)
    return df[['imei', 'plate', 'device_name']].dropna()

# def fetch_history_range(token, imei, start, end):
#     all_data = []
#     current_start = start
#     while current_start <= end:
#         current_end = min(current_start + timedelta(days=6), end)
#         data = get_history_data(token, imei,
#                                 current_start.strftime("%Y-%m-%d"),
#                                 current_end.strftime("%Y-%m-%d"))
#         all_data.extend(data)
#         current_start = current_end + timedelta(days=1)
#     return all_data

# UNTUK EMISI 

FUEL_DEFAULTS = {
    "gasoline": {"density": 0.74, "ncv": 44.3, "ef_co2": 69300, "ef_ch4": 33, "ef_n2o": 3.2},
    "diesel": {"density": 0.85, "ncv": 43.0, "ef_co2": 74100, "ef_ch4": 3.9, "ef_n2o": 0.6},
}

def hitung_emisi(volume_liter, fuel_type="diesel"):
    f = FUEL_DEFAULTS[fuel_type]
    energi_tj = (volume_liter * f["density"] * f["ncv"]) / 1_000_000

    co2 = f["ef_co2"] * energi_tj
    ch4 = f["ef_ch4"] * energi_tj
    n2o = f["ef_n2o"] * energi_tj

    ch4_co2e = ch4 * GWP_CH4
    n2o_co2e = n2o * GWP_N2O
    total_co2e = co2 + ch4_co2e + n2o_co2e

    return {
        "CO2_kg": co2,
        "CH4_kg": ch4,
        "N2O_kg": n2o,
        "CH4_CO2e": ch4_co2e,
        "N2O_CO2e": n2o_co2e,
        "Total_CO2e_kg": total_co2e,
        "Total_CO2e_ton": total_co2e / 1000
    }

#==================== DASHBOARD ======================================

@app.route('/', methods=['GET'])
def dashboard():
    try:
        token = get_token_cached()
        if not token:
            return "Gagal mendapatkan token", 500

        # Ambil filter dari query
        search_plate = request.args.get('plate', '') or ''
        start_time = request.args.get('start_time')
        end_time = request.args.get('end_time')

        # Default tanggal kemarin
        if not start_time or not end_time:
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            start_time = end_time = yesterday

        start_dt = datetime.strptime(start_time, '%Y-%m-%d')
        end_dt = datetime.strptime(end_time, '%Y-%m-%d')
        delta_days = (end_dt - start_dt).days + 1

        # Ambil mapping plate -> imei
        df_active = load_active_vehicles()
        df_active['imei'] = df_active['imei'].astype(str)
        plate_to_imei = dict(zip(df_active['plate'], df_active['imei']))
        all_plates = list(plate_to_imei.keys())
        target_plates = [search_plate] if search_plate else all_plates

        # Ambil mapping plate -> fuel_type
        df_kendaraan = pd.read_excel("data_kendaraan.xlsx")
        plate_to_fueltype = {}
        for _, row in df_kendaraan.iterrows():
            plate = str(row.get("plate","")).strip()
            device_name = str(row.get("device_name","")).lower()
            plate_to_fueltype[plate] = "gasoline" if "innova" in device_name else "diesel"

        # Siapkan summary & chart
        summary_data = []
        total_diesel = total_gasoline = total_emisi_gasoline = total_emisi_diesel = 0

        chart_labels = [(start_dt + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(delta_days)]
        chart_datasets = []

        for plate in target_plates:
            imei = plate_to_imei.get(plate)
            if not imei:
                continue

            data = get_history_data(token, imei, start_time, end_time)
            if not data:
                # isi 0 kalau tidak ada data
                daily_mileage = [0]*delta_days
            else:
                # hitung mileage per hari
                daily_result = defaultdict(lambda: {"mileage_km":0, "prev_odo":None})
                data = sorted([d for d in data if d.get("time") and d.get("mileage") is not None], key=lambda x:x['time'])
                prev_mileage = None
                for d in data:
                    odo = d.get("mileage",0) or 0
                    ts = d.get("time")
                    if prev_mileage is not None and odo>prev_mileage:
                        delta_km = (odo - prev_mileage)/1000
                    else:
                        delta_km = 0
                    prev_mileage = odo
                    if ts:
                        date_str = ts[:10]
                        if delta_km>0 and delta_km<500:
                            daily_result[date_str]["mileage_km"] += delta_km

                # buat list per tanggal range
                daily_mileage = [round(daily_result[d]["mileage_km"],2) if d in daily_result else 0 for d in chart_labels]

            # total mileage & fuel
            total_mileage = sum(daily_mileage)
            fuel_used = round(total_mileage / EFFICIENCY_KM_PER_LITER,2)
            fuel_type = plate_to_fueltype.get(plate,"diesel")
            emisi = hitung_emisi(fuel_used, fuel_type=fuel_type)

            if fuel_type=="gasoline":
                total_gasoline += fuel_used
                total_emisi_gasoline += emisi["Total_CO2e_ton"]
            else:
                total_diesel += fuel_used
                total_emisi_diesel += emisi["Total_CO2e_ton"]
            
            # Hitung total & rata-rata speed
            total_speed = sum([d.get('speed',0) or 0 for d in data])
            count_speed = len([d for d in data if d.get('speed') is not None])
            avg_speed = round(total_speed / count_speed, 2) if count_speed else 0

            summary_data.append({
                "plate": plate,
                "total_mileage": round(total_mileage,2),
                "fuel_consumption": fuel_used,
                "avg_speed": avg_speed,  # <- tambahkan ini
                "fuel_type": fuel_type,
                "daily_mileage": daily_mileage,
                "emisi_total_ton": round(emisi["Total_CO2e_ton"],2),
                "emisi_total_kg": round(emisi["Total_CO2e_kg"],2)
            })


            # Siapkan chart dataset
            chart_datasets.append({
                "label": plate,
                "data": daily_mileage
            })

        # Tentukan chart type
        if search_plate:
            chart_type = "line"  # single plate
        elif delta_days>1:
            chart_type = "line"  # semua kendaraan multi-line
        else:
            chart_type = "bar"   # semua kendaraan 1 hari → bar

        return render_template(
            "dashboard.html",
            all_plates=all_plates,
            search_plate=search_plate,
            start_time=start_time,
            end_time=end_time,
            summary_data=summary_data,
            total_gasoline=round(total_gasoline,2),
            total_diesel=round(total_diesel,2),
            total_emisi_gasoline=round(total_emisi_gasoline,2),
            total_emisi_diesel=round(total_emisi_diesel,2),
            chart_type=chart_type,
            chart_labels=chart_labels,
            chart_datasets=chart_datasets
        )

    except Exception as e:
        import traceback
        return f"<pre>{traceback.format_exc()}</pre>"

# =========================== VEHICLES DATA ===========================

@app.route('/vehicles')
def vehicles():
    token = get_token()
    if not token:
        return "Gagal mendapatkan token dari GPS.id", 500

    try:
        response = requests.get(
            "https://portal.gps.id/backend/seen/public/vehicle",
            headers={"Authorization": f"Bearer {token}"})

        if response.status_code != 200:
            return f"<h3>Gagal mengambil data dari GPS.id: {response.status_code}</h3><pre>{response.text}</pre>", 500

        api_data = response.json().get("message", {}).get("data", [])

        df = pd.read_excel(EXCEL_FILE)
        active_imeis = set(str(imei).strip() for imei in df['imei'].dropna())

        kendaraan_list = []
        for item in api_data:
            imei = str(item.get("imei", "")).strip()
            if not imei:
                continue
            kendaraan_list.append({
                "imei":
                imei,
                "plate":
                item.get("plate", "-"),
                "device_name":
                item.get("device_name", "-"),
                "speed":
                item.get("speed", 0),
                "mileage":
                item.get("mileage", 0),
                "last_update":
                item.get("last_update", "-"),
                "status":
                "Aktif" if imei in active_imeis else "Tidak Aktif"
            })

        return render_template("vehicles.html", kendaraan=kendaraan_list)

    except Exception:
        import traceback
        return f"<h3>Terjadi Error</h3><pre>{traceback.format_exc()}</pre>", 500


def safe_rows(rows):
    for row in rows:
        for key in row:
            if row[key] is None:
                row[key] = ""
    return rows


# =========================== MAPS ===========================


@app.route("/maps", methods=["GET", "POST"])
def maps():
    token = get_token()
    if not token:
        return "Gagal mendapatkan token", 500

    df = load_active_vehicles()
    df['imei'] = df['imei'].astype(str).str.strip()
    plate_to_imei = {row['plate']: row['imei'] for _, row in df.iterrows()}
    all_plates = list(plate_to_imei.keys())

    if request.method == "POST":
        selected_plate = request.form.get("plate")
        start_dt = request.form.get("start_time")
        end_dt = request.form.get("end_time")

        if not selected_plate or not start_dt or not end_dt:
            return "Lengkapi semua input", 400

        imei = plate_to_imei.get(selected_plate)
        if not imei:
            return "Plat tidak ditemukan", 404

        start = start_dt.replace("T", " ")
        end = end_dt.replace("T", " ")

        try:
            raw_data = get_history_data(token, imei, start[:10], end[:10])
        except Exception as e:
            return f"Error ambil data: {e}", 500

        points = []
        for d in raw_data:
            lat = d.get("lat")
            lon = d.get("lon")
            time = d.get("time")
            if lat and lon and time:
                points.append({
                    "Lat": lat,
                    "Lon": lon,
                    "DatetimeUTC": time,
                    "speed": d.get("speed"),
                    "engine": d.get("engine")
                })

        points.sort(key=lambda x: x['DatetimeUTC'])

        # Optimasi: ambil setiap N titik (misalnya setiap 10)
        N = 10
        filtered_data = [pt for i, pt in enumerate(points) if i % N == 0]

        return render_template("maps.html",
                               all_plates=all_plates,
                               selected_plate=selected_plate,
                               imei=imei,
                               vehicle_name=selected_plate,
                               start_time=start_dt,
                               end_time=end_dt,
                               rows=filtered_data,
                               points=filtered_data,
                               result=None)

    # GET method - initial load
    return render_template("maps.html",
                           all_plates=all_plates,
                           selected_plate="",
                           imei="",
                           vehicle_name="",
                           start_time="",
                           end_time="",
                           rows=[],
                           points=[],
                           result=None)

# # =========================== HISTORICAL DATA ===========================
# raw_history_cache = {}
# historical_cache = {}  # 🔑 Cache rekap historis

# @app.route('/historical', methods=['GET'])
# def historical_data():
#     try:
#         token = get_token()
#         if not token:
#             return "Gagal mendapatkan token dari GPS.id", 500

#         # === Ambil daftar kendaraan aktif dari Excel ===
#         df = pd.read_excel(EXCEL_FILE)
#         active_vehicles = df[['imei', 'plate', 'device_name']].dropna()
#         imei_list = active_vehicles.to_dict(orient='records')

#         result = []
#         error = None

#         # === Ambil parameter dari query string ===
#         start_date = request.args.get('start_date', '')
#         end_date = request.args.get('end_date', '')
#         selected_plate = request.args.get('plate', 'all')

#         if start_date and end_date:
#             cache_key = f"{start_date}_{end_date}_{selected_plate}"

#             # === Cek cache dulu ===
#             if cache_key in historical_cache:
#                 print("♻️ Menggunakan cache data historis")
#                 result = historical_cache[cache_key]
#             else:
#                 all_data = []

#                 # === Filter kendaraan (semua atau satu plate) ===
#                 vehicles = (
#                     active_vehicles
#                     if selected_plate == "all"
#                     else active_vehicles[active_vehicles["plate"] == selected_plate]
#                 )

#                 # === Ambil data historis untuk setiap kendaraan ===
#                 for _, row in vehicles.iterrows():
#                     plate = row['plate']
#                     imei = str(row['imei'])
#                     print(f"▶️ Memproses {plate} (IMEI: {imei})")

#                     try:
#                         history = get_history_data(token, imei, start_date, end_date)
#                         raw_key = f"{imei}_{start_date}_{end_date}"
#                         raw_history_cache[raw_key] = history
#                         print(f"  ✔️ Ambil {start_date} - {end_date} → {len(history)} data")

#                         for item in history:
#                             item.update({
#                                 'imei': imei,
#                                 'plate': plate,
#                                 'device_name': row['device_name']
#                             })
#                             all_data.append(item)

#                     except Exception as e:
#                         print(f"  ❌ Gagal ambil data → {e}")

#                     time.sleep(1.5)  # throttle biar tidak overload API

#                 # === Rekap per tanggal ===
#                 grouped = defaultdict(lambda: {
#                     "total_speed": 0,
#                     "speed_count": 0,
#                     "odo_start": None,
#                     "odo_end": None,
#                     "device_name": ""
#                 })

#                 for item in all_data:
#                     if not item.get("time") or item.get("mileage") is None:
#                         continue

#                     key = (item['plate'], item['time'][:10])
#                     group = grouped[key]

#                     if item['speed'] > 1:
#                         group['total_speed'] += item['speed']
#                         group['speed_count'] += 1

#                     odo = item['mileage']
#                     group['odo_start'] = odo if group['odo_start'] is None else min(group['odo_start'], odo)
#                     group['odo_end'] = odo if group['odo_end'] is None else max(group['odo_end'], odo)
#                     group['device_name'] = group['device_name'] or item['device_name']

#                 # === Rekap per kendaraan ===
#                 per_plate = defaultdict(lambda: {
#                     "total_speed": 0,
#                     "speed_count": 0,
#                     "odo_start": None,
#                     "odo_end": None,
#                     "device_name": ""
#                 })

#                 for (plate, _), data in grouped.items():
#                     group = per_plate[plate]
#                     group['total_speed'] += data['total_speed']
#                     group['speed_count'] += data['speed_count']
#                     group['odo_start'] = data['odo_start'] if group['odo_start'] is None else min(group['odo_start'], data['odo_start'])
#                     group['odo_end'] = data['odo_end'] if group['odo_end'] is None else max(group['odo_end'], data['odo_end'])
#                     group['device_name'] = group['device_name'] or data['device_name']

#                 # === Hitung ringkasan per kendaraan ===
#                 for row in vehicles.to_dict(orient='records'):
#                     plate = row['plate']
#                     imei = str(row['imei'])
#                     val = per_plate.get(plate)
#                     if not val:
#                         continue

#                     odo_start, odo_end = val['odo_start'], val['odo_end']
#                     mileage_km = (odo_end - odo_start) / 1000 if odo_start and odo_end else 0
#                     fuel_used = round(mileage_km / EFFICIENCY_KM_PER_LITER, 2) if mileage_km > 0 else 0
#                     avg_speed = round(val['total_speed'] / val['speed_count'], 2) if val['speed_count'] else 0

#                     if mileage_km == 0:
#                         status = "🛑 Tidak Bergerak"
#                     elif val['speed_count'] == 0:
#                         status = "⚠️ Ada Mileage, Tapi Speed 0"
#                     else:
#                         status = "✅ OK"

#                     result.append({
#                         "plate": plate,
#                         "imei": imei,
#                         "device_name": val['device_name'],
#                         "date": f"{start_date} s.d. {end_date}",
#                         "avg_speed": avg_speed,
#                         "mileage_today": round(mileage_km, 2),
#                         "fuel_used": fuel_used,
#                         "status": status
#                     })

#                 # Simpan hasil rekap ke cache
#                 historical_cache[cache_key] = result

#         return render_template(
#             "historical.html",
#             data=result,
#             imei_list=imei_list,
#             start_date=start_date,
#             end_date=end_date,
#             selected_plate=selected_plate,
#             error=error
#         )

#     except Exception:
#         import traceback
#         return f"<pre>{traceback.format_exc()}</pre>"

# =========================== HISTORICAL DATA ===========================
raw_history_cache = {}
historical_cache = {}  # 🔑 Cache rekap historis

def get_summary_from_detail(token, imei, plate, device_name, start_date, end_date):
    """Ambil data detail harian lalu rekap total"""
    cache_key = f"summary_{imei}_{start_date}_{end_date}"
    if cache_key in historical_cache:
        return historical_cache[cache_key]

    # Ambil data historis (pakai chunk biar aman)
    current_start = datetime.strptime(start_date, "%Y-%m-%d")
    current_end = datetime.strptime(end_date, "%Y-%m-%d")
    all_data = []
    while current_start <= current_end:
        chunk_end = min(current_start + timedelta(days=6), current_end)
        history = get_history_data(token, imei,
                                   current_start.strftime("%Y-%m-%d"),
                                   chunk_end.strftime("%Y-%m-%d"))
        all_data.extend(history)
        current_start = chunk_end + timedelta(days=1)

    # Simpan raw ke cache juga
    raw_key = f"{imei}_{start_date}_{end_date}"
    raw_history_cache[raw_key] = all_data

    # Proses harian (seperti di /historical/detail)
    grouped = defaultdict(lambda: {
        "total_speed": 0,
        "speed_count": 0,
        "mileage_km": 0,
        "prev_odo": None
    })

    all_data = sorted(
        [d for d in all_data if d.get("time") and d.get("mileage") is not None],
        key=lambda x: x['time']
    )

    for item in all_data:
        date_str = item['time'][:10]
        odo = item['mileage']
        speed = item.get('speed', 0)
        group = grouped[date_str]

        if group['prev_odo'] is not None and odo >= group['prev_odo']:
            delta_km = (odo - group['prev_odo']) / 1000
            if 0 < delta_km < 500:  # filter noise
                group['mileage_km'] += delta_km
        group['prev_odo'] = odo

        if speed > 1:
            group['total_speed'] += speed
            group['speed_count'] += 1

    # Rekap total kendaraan
    total_mileage = sum(g['mileage_km'] for g in grouped.values())
    total_fuel = round(total_mileage / EFFICIENCY_KM_PER_LITER, 2) if total_mileage > 0 else 0
    avg_speed_list = [g['total_speed'] / g['speed_count'] for g in grouped.values() if g['speed_count']]
    avg_speed = round(sum(avg_speed_list) / len(avg_speed_list), 2) if avg_speed_list else 0

    if total_mileage == 0:
        status = "🛑 Tidak Bergerak"
    elif not avg_speed_list:
        status = "⚠️ Ada Mileage, Tapi Speed 0"
    else:
        status = "✅ OK"

    result = {
        "plate": plate,
        "imei": imei,
        "device_name": device_name,
        "date": f"{start_date} s.d. {end_date}",
        "avg_speed": avg_speed,
        "mileage_today": round(total_mileage, 2),
        "fuel_used": total_fuel,
        "status": status
    }

    historical_cache[cache_key] = result
    return result


@app.route('/historical', methods=['GET'])
def historical_data():
    try:
        token = get_token()
        if not token:
            return "Gagal mendapatkan token dari GPS.id", 500

        # === Ambil daftar kendaraan aktif dari Excel ===
        df = pd.read_excel(EXCEL_FILE)
        active_vehicles = df[['imei', 'plate', 'device_name']].dropna()
        imei_list = active_vehicles.to_dict(orient='records')

        result = []
        error = None

        # === Ambil parameter dari query string ===
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        selected_plate = request.args.get('plate', 'all')

        if start_date and end_date:
            cache_key = f"{start_date}_{end_date}_{selected_plate}"

            # === Cek cache dulu ===
            if cache_key in historical_cache:
                print("♻️ Menggunakan cache data historis")
                result = historical_cache[cache_key]
            else:
                # === Filter kendaraan (semua atau satu plate) ===
                vehicles = (
                    active_vehicles
                    if selected_plate == "all"
                    else active_vehicles[active_vehicles["plate"] == selected_plate]
                )

                # === Ambil summary dari detail ===
                for _, row in vehicles.iterrows():
                    plate = row['plate']
                    imei = str(row['imei'])
                    device_name = row['device_name']
                    print(f"▶️ Memproses {plate} (IMEI: {imei})")

                    try:
                        summary = get_summary_from_detail(token, imei, plate, device_name, start_date, end_date)
                        result.append(summary)
                    except Exception as e:
                        print(f"  ❌ Gagal ambil data {plate} → {e}")

                    time.sleep(1.5)  # throttle biar tidak overload API

                # Simpan hasil ke cache
                historical_cache[cache_key] = result

        return render_template(
            "historical.html",
            data=result,
            imei_list=imei_list,
            start_date=start_date,
            end_date=end_date,
            selected_plate=selected_plate,
            error=error
        )

    except Exception:
        import traceback
        return f"<pre>{traceback.format_exc()}</pre>"

# =========================== DATA HISTORICAL DETAIL ===========================

historical_detail_cache = {}

@app.route('/historical/detail')
def historical_detail():
    token = get_token()
    if not token:
        return "Gagal mendapatkan token dari GPS.id", 500

    plate = request.args.get('plate')
    imei = request.args.get('imei')
    start = request.args.get('start')
    end = request.args.get('end')
    debug = request.args.get('debug')

    if not all([start, end]) or (not plate and not imei):
        return "Parameter tidak lengkap", 400

    # Baca Excel
    df = pd.read_excel(EXCEL_FILE)
    df['imei'] = df['imei'].apply(lambda x: str(int(x)) if not pd.isna(x) else '')
    df['plate'] = df['plate'].astype(str).str.strip()
    all_plates = sorted(df['plate'].unique().tolist())   # 🔑 semua plat

    # Cari kendaraan
    if plate:
        vehicle = df[df['plate'] == plate.strip()]
    elif imei:
        vehicle = df[df['imei'] == imei.strip()]
    else:
        vehicle = pd.DataFrame()

    if vehicle.empty:
        return "Kendaraan tidak ditemukan", 404

    imei = str(vehicle.iloc[0]['imei'])
    plate = vehicle.iloc[0]['plate']
    cache_key = f"{imei}_{start}_{end}"

    # ================== AMBIL DATA RAW (CACHE / API) ==================
    s_date = datetime.strptime(start, "%Y-%m-%d")
    e_date = datetime.strptime(end, "%Y-%m-%d")

    if cache_key in raw_history_cache:
        all_data = raw_history_cache[cache_key]
        log_lines = ["[CACHE] Data diambil dari raw_history_cache (/historical)"]
    else:
        current_start = s_date
        all_data = []
        log_lines = []

        while current_start <= e_date:
            current_end = min(current_start + timedelta(days=6), e_date)
            try:
                history = get_history_data(
                    token, imei,
                    current_start.strftime("%Y-%m-%d"),
                    current_end.strftime("%Y-%m-%d")
                )
                all_data.extend(history)
                log_lines.append(
                    f"Fetched {len(history)} data from {current_start.date()} to {current_end.date()}"
                )
            except Exception as e:
                log_lines.append(
                    f"Error fetching data {current_start.date()} - {current_end.date()}: {e}"
                )
            current_start = current_end + timedelta(days=1)

        # simpan ke cache supaya next time nggak fetch ulang
        raw_history_cache[cache_key] = all_data

    # ================== PROSES DATA ==================
    grouped = defaultdict(lambda: {
        "total_speed": 0,
        "speed_count": 0,
        "mileage_km": 0,
        "prev_odo": None
    })

    all_data = sorted(
        [d for d in all_data if d.get("time") and d.get("mileage") is not None],
        key=lambda x: x['time']
    )

    for item in all_data:
        date_str = item['time'][:10]
        odo = item['mileage']
        speed = item.get('speed', 0)

        group = grouped[date_str]

        if group['prev_odo'] is not None and odo >= group['prev_odo']:
            delta_km = (odo - group['prev_odo']) / 1000
            if 0 < delta_km < 500:  # validasi wajar
                group['mileage_km'] += delta_km

        group['prev_odo'] = odo

        if speed > 1:
            group['total_speed'] += speed
            group['speed_count'] += 1

    result = []
    current_date = s_date
    while current_date <= e_date:
        date_str = current_date.strftime("%Y-%m-%d")
        group = grouped.get(date_str, {})

        mileage_km = group.get("mileage_km", 0)
        fuel_used = round(mileage_km / EFFICIENCY_KM_PER_LITER, 2) if mileage_km > 0 else 0
        avg_speed = round(group['total_speed'] / group['speed_count'], 2) if group.get('speed_count', 0) > 0 else 0

        result.append({
            "date": date_str,
            "avg_speed": avg_speed,
            "mileage_today": round(mileage_km, 2),
            "fuel_used": fuel_used,
            "plate": plate
        })
        current_date += timedelta(days=1)

    # cache hasil perhitungan
    historical_detail_cache[cache_key] = result

    # ================== DEBUG MODE ==================
    if debug:
        log_text = "\n".join(log_lines)
        return f"<pre>{log_text}</pre>"

    # ================== GRAFIK ==================
    if result:
        dates = [r['date'] for r in result]
        mileage = [r['mileage_today'] for r in result]
        fuel = [r['fuel_used'] for r in result]

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(dates, mileage, marker='o', label='Mileage (km)')
        ax.plot(dates, fuel, marker='x', label='Fuel Used (L)')
        ax.set_xlabel("Tanggal")
        ax.set_ylabel("Jumlah")
        ax.set_title(f"Grafik Mileage & BBM - {plate}")
        ax.legend()
        ax.grid(True)
        plt.xticks(rotation=45)
        plt.tight_layout()

        chart_path = f"static/chart_{imei}_{start}_{end}.png"
        fig.savefig(chart_path)
        plt.close()
    else:
        chart_path = None

    # ================== RINGKASAN ==================
    total_mileage = sum(r['mileage_today'] for r in result)
    total_fuel = sum(r['fuel_used'] for r in result)
    avg_speed_list = [r['avg_speed'] for r in result if r['avg_speed'] > 0]
    avg_speed = round(sum(avg_speed_list) / len(avg_speed_list), 2) if avg_speed_list else 0

    # ================== EKSPOR EXCEL ==================
    if request.args.get("export") == "1":
        df_export = pd.DataFrame(result)[["date", "plate", "avg_speed", "mileage_today", "fuel_used"]]
        df_export.columns = [
            "Tanggal", "Plat", "Rata-rata Kecepatan (km/h)",
            "Jarak Tempuh (km)", "BBM Terpakai (L)"
        ]

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df_export.to_excel(writer, index=False, sheet_name='Detail Harian')
        output.seek(0)

        filename = f"rekap_{plate}_{start}_to_{end}.xlsx"
        return send_file(
            output,
            download_name=filename,
            as_attachment=True,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    # ================== RENDER TEMPLATE ==================
    return render_template(
        "historical_detail.html",
        data=result,
        plate=plate,
        start=start,
        end=end,
        vehicle_name=vehicle.iloc[0]['device_name'] if not vehicle.empty else '',
        chart_path=chart_path,
        total_mileage=round(total_mileage, 2),
        total_fuel=round(total_fuel, 2),
        avg_speed=avg_speed,
        all_plates=all_plates
    )

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)

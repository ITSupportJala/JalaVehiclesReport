from flask import Flask, request, render_template, redirect, url_for, jsonify
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

# SETUP LOGGING
logging.basicConfig(
    level=logging.INFO,  # Level default: INFO (bisa DEBUG, WARNING, ERROR)
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()  # tampil di console
        # logging.FileHandler("app.log")  # kalau mau simpan ke file
    ]
)

# =========================== KONFIGURASI & KONSTANTA ===========================
load_dotenv()
EXCEL_FILE = 'data_kendaraan.xlsx'

EFFICIENCY_BY_FUEL = {
    "Pertalite": 12,
    "Pertamax": 23,
    "Solar": 15,
    "None": 15   # default kalau kosong
}

FUEL_MAPPING = {
    "Pertalite": "gasoline",
    "Pertamax": "gasoline",
    "Solar": "diesel",
    "None": "diesel"
}

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

# =========================== DATABASE ===========================
import sqlite3

DB_FILE = "vehicles.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS vehicles_status (
            imei TEXT PRIMARY KEY,
            plate TEXT,
            device_name TEXT,
            custom_name TEXT,              
            status TEXT DEFAULT 'Tidak Aktif'
        )
    """)
    conn.commit()
    conn.close()

def get_status(imei):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT status FROM vehicles_status WHERE imei=?", (imei,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "Tidak Aktif"

def upsert_vehicle(imei, plate, device_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO vehicles_status (imei, plate, device_name, status)
        VALUES (?, ?, ?, 'Tidak Aktif')
    """, (imei, plate, device_name))
    conn.commit()
    conn.close()

def update_status(imei, status):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE vehicles_status SET status=? WHERE imei=?", (status, imei))
    conn.commit()
    conn.close()

# 🔹 Fungsi baru untuk update custom_name
def update_custom_name(imei, custom_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE vehicles_status SET custom_name=? WHERE imei=?", (custom_name, imei))
    conn.commit()
    conn.close()

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
def get_active_vehicles():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT imei, plate, COALESCE(custom_name, device_name) as device_name, fuel_type
            FROM vehicles_status
            WHERE status='Aktif'
        """)
        rows = c.fetchall()
    if not rows:
        return pd.DataFrame(columns=["imei", "plate", "device_name", "fuel_type"])
    return pd.DataFrame(rows, columns=["imei", "plate", "device_name", "fuel_type"]).sort_values(by="plate").reset_index(drop=True)

def load_active_vehicles():
    if not os.path.exists(EXCEL_FILE):
        raise FileNotFoundError("File Excel tidak ditemukan.")
    df = pd.read_excel(EXCEL_FILE)
    return df[['imei', 'plate', 'device_name']].dropna()

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

        # Ambil mapping plate -> imei & fuel_type dari DB
        df_active = get_active_vehicles()
        df_active['imei'] = df_active['imei'].astype(str)
        plate_to_imei = dict(zip(df_active['plate'], df_active['imei']))
        plate_to_fueltype = dict(zip(df_active['plate'], df_active['fuel_type']))
        all_plates = list(plate_to_imei.keys())
        target_plates = [search_plate] if search_plate else all_plates

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
                daily_mileage = [0] * delta_days
            else:
                # hitung mileage per hari
                daily_result = defaultdict(lambda: {"mileage_km": 0, "prev_odo": None})
                data = sorted(
                    [d for d in data if d.get("time") and d.get("mileage") is not None],
                    key=lambda x: x['time']
                )
                prev_mileage = None
                for d in data:
                    odo = d.get("mileage", 0) or 0
                    ts = d.get("time")
                    if prev_mileage is not None and odo > prev_mileage:
                        delta_km = (odo - prev_mileage) / 1000
                    else:
                        delta_km = 0
                    prev_mileage = odo
                    if ts:
                        date_str = ts[:10]
                        if 0 < delta_km < 500:
                            daily_result[date_str]["mileage_km"] += delta_km

                daily_mileage = [
                    round(daily_result[d]["mileage_km"], 2) if d in daily_result else 0
                    for d in chart_labels
                ]

            # total mileage
            total_mileage = sum(daily_mileage)

            # fuel type dari DB
            fuel_type_db = plate_to_fueltype.get(plate, "None")
            efficiency = EFFICIENCY_BY_FUEL.get(fuel_type_db, 15)
            fuel_used = round(total_mileage / efficiency, 2)

            # kategori untuk emission factor
            fuel_category = FUEL_MAPPING.get(fuel_type_db, "diesel")
            emisi = hitung_emisi(fuel_used, fuel_type=fuel_category)

            # total emisi global
            if fuel_category == "gasoline":
                total_gasoline += fuel_used
                total_emisi_gasoline += emisi["Total_CO2e_ton"]
            else:
                total_diesel += fuel_used
                total_emisi_diesel += emisi["Total_CO2e_ton"]

            # avg speed
            total_speed = sum([d.get('speed', 0) or 0 for d in data]) if data else 0
            count_speed = len([d for d in data if d.get('speed') is not None]) if data else 0
            avg_speed = round(total_speed / count_speed, 2) if count_speed else 0

            # simpan ke summary
            summary_data.append({
                "plate": plate,
                "total_mileage": round(total_mileage, 2),
                "fuel_consumption": fuel_used,
                "avg_speed": avg_speed,
                "fuel_type": fuel_type_db,
                "daily_mileage": daily_mileage,
                "emisi_total_ton": round(emisi["Total_CO2e_ton"], 2),
                "emisi_total_kg": round(emisi["Total_CO2e_kg"], 2)
            })

            # chart dataset
            chart_datasets.append({
                "label": plate,
                "data": daily_mileage
            })

        # Tentukan chart type
        if search_plate:
            chart_type = "line"  # single plate
        elif delta_days > 1:
            chart_type = "line"  # multi-plate multi-day
        else:
            chart_type = "bar"   # semua kendaraan 1 hari

        return render_template(
            "dashboard.html",
            all_plates=all_plates,
            search_plate=search_plate,
            start_time=start_time,
            end_time=end_time,
            summary_data=summary_data,
            total_gasoline=round(total_gasoline, 2),
            total_diesel=round(total_diesel, 2),
            total_emisi_gasoline=round(total_emisi_gasoline, 2),
            total_emisi_diesel=round(total_emisi_diesel, 2),
            chart_type=chart_type,
            chart_labels=chart_labels,
            chart_datasets=chart_datasets
        )

    except Exception as e:
        import traceback
        return f"<pre>{traceback.format_exc()}</pre>"

# =========================== VEHICLES DATA ===========================

def get_vehicle_info(imei):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT plate, COALESCE(custom_name, device_name), status, fuel_type
        FROM vehicles_status
        WHERE imei=?
    """, (imei,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1], row[2], row[3]  # plate, name, status, fuel_type
    return None, None, None, None


@app.route('/vehicles')
def vehicles():
    token = get_token()
    if not token:
        return "Gagal mendapatkan token dari GPS.id", 500

    try:
        response = requests.get(
            "https://portal.gps.id/backend/seen/public/vehicle",
            headers={"Authorization": f"Bearer {token}"}
        )

        if response.status_code != 200:
            return f"<h3>Gagal ambil data GPS.id: {response.status_code}</h3><pre>{response.text}</pre>", 500

        api_data = response.json().get("message", {}).get("data", [])

        kendaraan_list = []
        for item in api_data:
            imei = str(item.get("imei", "")).strip()
            if not imei:
                continue

            plate_api = item.get("plate", "-")
            device_name_api = item.get("device_name", "-")

            # pastikan ada di DB
            upsert_vehicle(imei, plate_api, device_name_api)

            # ambil dari DB supaya custom_name kepakai
            plate, vehicle_name, status, fuel_type = get_vehicle_info(imei)

            kendaraan_list.append({
                "imei": imei,
                "plate": plate,
                "custom_name": None,
                "device_name": vehicle_name,
                "speed": item.get("speed", 0),
                "mileage": item.get("mileage", 0),
                "last_update": item.get("last_update", "-"),
                "status": status,
                "fuel_type": fuel_type if fuel_type else "None"
            })

        return render_template("vehicles.html", kendaraan=kendaraan_list)

    except Exception:
        import traceback
        return f"<h3>Terjadi Error</h3><pre>{traceback.format_exc()}</pre>", 500

@app.route('/update_status', methods=['POST'])
def update_status_route():
    data = request.get_json()
    imei = data.get("imei")
    status = data.get("status")
    if not imei or not status:
        return {"success": False, "message": "IMEI / status tidak valid"}, 400
    
    update_status(imei, status)
    return {"success": True, "message": f"Status {imei} diupdate ke {status}"}

@app.route('/update_name', methods=['POST'])
def update_name():
    data = request.get_json()
    imei = data.get('imei')
    custom_name = data.get('custom_name')

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            UPDATE vehicles_status
            SET custom_name = ?
            WHERE imei = ?
        """, (custom_name, imei))
        conn.commit()
        conn.close()
        return jsonify(success=True)
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500

@app.route('/update_fuel_type', methods=['POST'])
def update_fuel_type():
    data = request.json
    imei = data.get("imei")
    fuel_type = data.get("fuel_type")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE vehicles_status SET fuel_type=? WHERE imei=?", (fuel_type, imei))
    conn.commit()
    conn.close()

    return jsonify({"success": True})

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

# =========================== HISTORICAL DATA ===========================
raw_history_cache = {}
historical_cache = {}  # 🔑 Cache rekap historis
historical_detail_cache = {}  # 🔑 Cache detail harian


# ================== SUMMARY UNTUK /historical ==================
def get_summary_from_detail(token, imei, plate, device_name, start_date, end_date, fuel_type="Solar"):
    """Ambil data detail harian lalu rekap total"""
    cache_key = f"summary_{imei}_{start_date}_{end_date}"
    if cache_key in historical_cache:
        return historical_cache[cache_key]

    # ========== ambil data seperti biasa ==========
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

    raw_key = f"{imei}_{start_date}_{end_date}"
    raw_history_cache[raw_key] = all_data

    # ========== proses mileage & speed ==========
    grouped = defaultdict(lambda: {"total_speed": 0, "speed_count": 0,
                                   "mileage_km": 0, "prev_odo": None})
    all_data = sorted(
        [d for d in all_data if d.get("time") and d.get("mileage") is not None],
        key=lambda x: x['time']
    )

    for item in all_data:
        date_str = item['time'][:10]
        odo = item['mileage']
        speed = item.get('speed', 0)
        g = grouped[date_str]

        if g['prev_odo'] is not None and odo >= g['prev_odo']:
            delta_km = (odo - g['prev_odo']) / 1000
            if 0 < delta_km < 500:
                g['mileage_km'] += delta_km
        g['prev_odo'] = odo

        if speed > 1:
            g['total_speed'] += speed
            g['speed_count'] += 1

    # ========== rekap total ==========
    total_mileage = sum(g['mileage_km'] for g in grouped.values())

    # ✅ fuel_type spesifik
    eff = EFFICIENCY_BY_FUEL.get(fuel_type, 15)
    total_fuel = round(total_mileage / eff, 2) if total_mileage > 0 else 0

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
        "fuel_type": fuel_type,   # ✅ fuel_type ikut disimpan
        "date": f"{start_date} s.d. {end_date}",
        "avg_speed": avg_speed,
        "mileage_today": round(total_mileage, 2),
        "fuel_used": total_fuel,
        "status": status
    }

    historical_cache[cache_key] = result
    return result


# ================== ROUTE /historical ==================
@app.route('/historical')
def historical_data():
    """Rekap per kendaraan (periode)"""
    try:
        token = get_token()
        active_vehicles = get_active_vehicles()

        # pastikan DataFrame & hilangkan duplikat plate
        if isinstance(active_vehicles, pd.DataFrame):
            active_vehicles["plate"] = active_vehicles["plate"].astype(str).str.strip().str.upper()
            active_vehicles = active_vehicles.drop_duplicates(subset=["plate"])
            imei_list = active_vehicles.to_dict(orient="records")
        else:
            imei_list = active_vehicles

        error, result = None, []
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        selected_plate = request.args.get('plate', 'all')

        if start_date and end_date:
            cache_key = f"{start_date}_{end_date}_{selected_plate}"
            if cache_key in historical_cache:
                result = historical_cache[cache_key]
                logging.info(f"✅ Cache hit untuk {cache_key}, {len(result)} data diambil dari cache")
            else:
                vehicles = active_vehicles if selected_plate == "all" else active_vehicles[active_vehicles["plate"] == selected_plate]

                for _, row in vehicles.iterrows():
                    try:
                        fuel_type = row["fuel_type"] if "fuel_type" in row and pd.notna(row["fuel_type"]) else "Solar"
                        logging.info(f"🔄 Ambil data {row['plate']} ({fuel_type}) periode {start_date} → {end_date}")

                        summary = get_summary_from_detail(
                            token,
                            str(row['imei']),
                            row['plate'],
                            row['device_name'],
                            start_date,
                            end_date,
                            fuel_type
                        )
                        result.append(summary)
                        logging.info(f"✅ Berhasil ambil data {row['plate']}")

                    except Exception as e:
                        logging.error(f"❌ Gagal ambil data {row['plate']} → {e}")
                    time.sleep(1.5)

                historical_cache[cache_key] = result
                logging.info(f"💾 Data disimpan ke cache: {cache_key}")

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
        logging.exception("🚨 Error di /historical")
        import traceback
        return f"<pre>{traceback.format_exc()}</pre>"

# ================== DETAIL UNTUK /historical/detail ==================
def get_all_plates():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT imei, plate, COALESCE(custom_name, device_name) as device_name
        FROM vehicles_status
        WHERE status='Aktif'
    """)
    rows = c.fetchall()
    conn.close()
    return [{"imei": r[0], "plate": r[1], "device_name": r[2]} for r in rows]

def get_vehicle(plate=None, imei=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if plate:
        c.execute("SELECT imei, plate, COALESCE(custom_name, device_name) FROM vehicles_status WHERE plate=?", (plate.strip(),))
    elif imei:
        c.execute("SELECT imei, plate, COALESCE(custom_name, device_name) FROM vehicles_status WHERE imei=?", (imei.strip(),))
    row = c.fetchone()
    conn.close()
    return row

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

    # 🔹 Ambil daftar semua plat dari DB
    all_plates = get_all_plates()

    # 🔹 Ambil kendaraan spesifik
    vehicle = get_vehicle(plate, imei)
    if not vehicle:
        return "Kendaraan tidak ditemukan", 404

    imei, plate, device_name = vehicle
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

        raw_history_cache[cache_key] = all_data  # simpan cache

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

    historical_detail_cache[cache_key] = result  # cache hasil

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
        vehicle_name=device_name,
        chart_path=chart_path,
        total_mileage=round(total_mileage, 2),
        total_fuel=round(total_fuel, 2),
        avg_speed=avg_speed,
        all_plates=all_plates  # ✅ list kendaraan aktif
    )

if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="127.0.0.1", port=5000)

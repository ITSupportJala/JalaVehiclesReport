from flask import Flask, request, jsonify, render_template
import mysql.connector
import requests
import time
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Konfigurasi database MySQL (untuk push dari GPS.id)
db_config = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME')
}

# Konfigurasi akun GPS.id
gps_config = {
    'username': os.getenv('GPS_USERNAME'),
    'password': os.getenv('GPS_PASSWORD')
}

# Token cache untuk GPS.id API
_token_cache = {"token": None, "expires_at": 0}


# Fungsi mendapatkan token GPS.id
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
    except:
        return None


# Ambil daftar kendaraan dari GPS.id
def get_vehicle_data(token):
    try:
        url = "https://portal.gps.id/backend/seen/public/vehicle"
        headers = {"Authorization": f"Bearer {token}"}
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        return res.json().get("message", {}).get("data", [])
    except:
        return []


def get_history_data(token, imei, start_date, end_date, page=1, per_page=100):
    url = "https://portal.gps.id/backend/seen/public/report/history"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "device": imei,
        "start": f"{start_date} 00:00:00",
        "end": f"{end_date} 23:59:59",
        "page": page,
        "per_page": per_page
    }

    try:
        res = requests.get(url, headers=headers, params=params)
        res.raise_for_status()
        return res.json().get("message", {}).get("data", [])
    except Exception as e:
        print("Error get_history_data:", e)
        return []


# Ambil data mileage penuh dalam rentang waktu
def get_mileage_data(token, imei, start_date, end_date):
    url = "https://portal.gps.id/backend/seen/public/data/mileage"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"imei": imei, "start_date": start_date, "end_date": end_date}
    try:
        res = requests.post(url, json=payload, headers=headers)
        res.raise_for_status()
        return res.json().get("message", {}).get("data", [])
    except:
        return []


def get_full_mileage_data(token, imei, start_date, end_date):
    all_data = []
    try:
        current_start = datetime.strptime(start_date, "%Y-%m-%d")
        final_end = datetime.strptime(end_date, "%Y-%m-%d")
        while current_start <= final_end:
            current_end = min(current_start + timedelta(days=6), final_end)
            chunk = get_mileage_data(token, imei,
                                     current_start.strftime("%Y-%m-%d"),
                                     current_end.strftime("%Y-%m-%d"))
            if isinstance(chunk, list):
                all_data.extend(chunk)
            current_start = current_end + timedelta(days=1)
        return all_data
    except:
        return []


app = Flask(__name__, static_folder='static', template_folder='templates')


# Dashboard utama
@app.route('/')
def dashboard():
    token = get_token()
    if not token:
        return "Token gagal diambil", 500

    vehicles = get_vehicle_data(token)
    data_jala = [{
        "imei": v["imei"],
        "plate": v.get("plate", "-"),
        "mileage": 0
    } for v in vehicles]

    # Ambil data DB terakhir 5
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT VehicleNumber, Speed FROM gps_data ORDER BY DatetimeUTC DESC LIMIT 5"
    )
    data_db = cursor.fetchall()

    # Ambil data historis sample (optional)
    data_history = [{
        "datetime": "2024-06-01 12:00",
        "engine": "ON"
    }, {
        "datetime": "2024-06-01 13:00",
        "engine": "OFF"
    }]

    return render_template("dashboard.html",
                           total=len(vehicles),
                           moving=0,
                           stop=0,
                           data_jala=data_jala,
                           data_db=data_db,
                           data_history=data_history)


# endpoint untuk menerima data dari GPS.id
@app.route('/api/gps-data', methods=['POST'])
def receive_gps_data():
    data = request.get_json()
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        query = """
            INSERT INTO gps_data (
                VehicleId, VehicleNumber, DatetimeUTC, GpsLocation,
                Lon, Lat, Speed, Direction, Engine,
                Odometer, Car_Status, VehicleType
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        values = (
            data.get("VehicleId"),
            data.get("VehicleNumber"),
            data.get("DatetimeUTC"),
            data.get("GpsLocation"),
            data.get("Lon"),
            data.get("Lat"),
            data.get("Speed"),
            data.get("Direction"),
            data.get("Engine"),
            data.get("Odometer"),
            data.get("Car_Status"),
            data.get("VehicleType"),
        )
        cursor.execute(query, values)
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"message": "Data berhasil disimpan"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Endpoint untuk menampilkan data kendaraan dari GPS.id
@app.route('/kendaraan-jala')
def kendaraan_jala():
    token = get_token()
    if not token:
        return "Gagal mendapatkan token dari GPS.id", 500

    vehicles = get_vehicle_data(token)
    today = datetime.today().date()
    last_7 = today - timedelta(days=6)

    data = []
    for v in vehicles:
        imei = v.get("imei")
        plate = v.get("plate", "-")
        mileage = 0
        if imei:
            mileage_data = get_full_mileage_data(token, imei, str(last_7),
                                                 str(today))
            if isinstance(mileage_data, list):
                mileage = sum(
                    d.get("mileage", 0) for d in mileage_data
                    if isinstance(d, dict))
        data.append({
            "imei": imei,
            "plate": plate,
            "mileage": round(mileage / 1000, 2)
        })

    return render_template("kendaraan_jala.html", data=data)


# Endpoint untuk menampilkan peta kendaraan
@app.route('/maps')
def map_view():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT VehicleNumber, Lat, Lon, DatetimeUTC FROM gps_data WHERE Lat IS NOT NULL AND Lon IS NOT NULL ORDER BY id DESC LIMIT 50"
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return render_template("map_view.html", rows=rows)
    except Exception as e:
        return f"<h3>Error loading map: {str(e)}</h3>"


@app.route('/kendaraan-db', methods=['GET', 'POST'])
def kendaraan_db():
    search_plate = request.form.get(
        'plate') if request.method == 'POST' else ''
    start_time = request.form.get(
        'start_time') if request.method == 'POST' else ''
    end_time = request.form.get('end_time') if request.method == 'POST' else ''
    sort_order = request.form.get(
        'sort_order', 'DESC') if request.method == 'POST' else 'DESC'

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)

    query = "SELECT * FROM gps_data WHERE 1=1"
    params = []

    if search_plate:
        query += " AND VehicleNumber = %s"
        params.append(search_plate)
    if start_time:
        query += " AND DatetimeUTC >= %s"
        params.append(start_time)
    if end_time:
        query += " AND DatetimeUTC <= %s"
        params.append(end_time)

    query += f" ORDER BY DatetimeUTC {sort_order} LIMIT 100"

    cursor.execute(query, params)
    rows = cursor.fetchall()

    cursor.execute(
        "SELECT DISTINCT VehicleNumber FROM gps_data ORDER BY VehicleNumber")
    all_plates = [row['VehicleNumber'] for row in cursor.fetchall()]

    cursor.close()
    conn.close()

    # Perhitungan BBM berdasarkan selisih mileage
    efficiency_km_per_liter = 10
    previous_odom = None
    for row in rows:
        if previous_odom is not None and row['Odometer'] is not None and row[
                'Engine'] == 'ON':
            delta_meter = max(0, row['Odometer'] - previous_odom)
            delta_km = delta_meter / 1000
            row['FuelUsed'] = round(delta_km / efficiency_km_per_liter, 2)
        else:
            row['FuelUsed'] = 0.0

        previous_odom = row['Odometer']

    return render_template("kendaraan_db.html",
                           rows=rows,
                           all_plates=all_plates)


@app.route('/historical', methods=['GET', 'POST'])
def historical_data():

    data = []
    error = None
    token = get_token()
    imei_list = []

    if not token:
        return "Gagal mendapatkan token dari GPS.id", 500

    vehicles = get_vehicle_data(token)
    imei_list = [{
        "imei": v["imei"],
        "plate": v.get("plate", "")
    } for v in vehicles]

    if request.method == 'POST':
        imei = request.form.get('imei')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')

        if not imei or not start_date or not end_date:
            error = "Semua field wajib diisi!"
        else:
            data = get_history_data(token, imei, start_date, end_date)
            if not data:
                error = "Data tidak ditemukan dalam rentang waktu."

    return render_template("historical.html",
                           imei_list=imei_list,
                           data=data,
                           error=error)


# Jalankan aplikasi
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

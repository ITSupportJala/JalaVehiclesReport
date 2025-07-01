from flask import Flask, request, jsonify, render_template
import sqlite3
import requests
import time
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DB_PATH = 'gps_data.db'

# Buat tabel gps_data kalau belum ada
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS gps_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            VehicleId TEXT,
            VehicleNumber TEXT,
            DatetimeUTC TEXT,
            GpsLocation TEXT,
            Lon REAL,
            Lat REAL,
            Speed REAL,
            Direction TEXT,
            Engine TEXT,
            Odometer REAL,
            Car_Status TEXT,
            VehicleType TEXT
        )
    ''')
    conn.commit()
    conn.close()


init_db()

app = Flask(__name__, static_folder='static', template_folder='templates')

DB_PATH = 'gps_data.db'


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


gps_config = {
    'username': os.getenv('GPS_USERNAME'),
    'password': os.getenv('GPS_PASSWORD')
}

_token_cache = {"token": None, "expires_at": 0}


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
    except:
        return []


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

    conn = get_db_connection()
    cursor = conn.execute(
        "SELECT VehicleNumber, Speed FROM gps_data ORDER BY DatetimeUTC DESC LIMIT 5"
    )
    data_db = cursor.fetchall()
    conn.close()

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


@app.route('/api/gps-data', methods=['POST'])
def receive_gps_data():
    data = request.get_json()
    try:
        conn = get_db_connection()
        query = """
            INSERT INTO gps_data (
                VehicleId, VehicleNumber, DatetimeUTC, GpsLocation,
                Lon, Lat, Speed, Direction, Engine,
                Odometer, Car_Status, VehicleType
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (data.get("VehicleId"), data.get("VehicleNumber"),
                  data.get("DatetimeUTC"), data.get("GpsLocation"),
                  data.get("Lon"), data.get("Lat"), data.get("Speed"),
                  data.get("Direction"), data.get("Engine"),
                  data.get("Odometer"), data.get("Car_Status"),
                  data.get("VehicleType"))
        conn.execute(query, values)
        conn.commit()
        conn.close()
        return jsonify({"message": "Data berhasil disimpan"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/maps')
def map_view():
    try:
        conn = get_db_connection()
        cursor = conn.execute("""
            SELECT VehicleNumber, Lat, Lon, DatetimeUTC FROM gps_data
            WHERE Lat IS NOT NULL AND Lon IS NOT NULL
            ORDER BY id DESC LIMIT 50
        """)
        rows = cursor.fetchall()
        conn.close()
        return render_template("map_view.html", rows=rows)
    except Exception as e:
        return f"<h3>Error loading map: {str(e)}</h3>"


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

@app.route('/kendaraan-db', methods=['GET', 'POST'])
def kendaraan_db():
    search_plate = request.form.get('plate') if request.method == 'POST' else ''
    start_time = request.form.get('start_time') if request.method == 'POST' else ''
    end_time = request.form.get('end_time') if request.method == 'POST' else ''
    sort_order = request.form.get('sort_order', 'DESC') if request.method == 'POST' else 'DESC'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # agar row bisa diakses seperti dictionary
    cursor = conn.cursor()

    query = "SELECT * FROM gps_data WHERE 1=1"
    params = []

    if search_plate:
        query += " AND VehicleNumber = ?"
        params.append(search_plate)
    if start_time:
        query += " AND DatetimeUTC >= ?"
        params.append(start_time)
    if end_time:
        query += " AND DatetimeUTC <= ?"
        params.append(end_time)

    query += f" ORDER BY DatetimeUTC {sort_order} LIMIT 100"

    cursor.execute(query, params)
    rows = cursor.fetchall()

    cursor.execute("SELECT DISTINCT VehicleNumber FROM gps_data ORDER BY VehicleNumber")
    all_plates = [row['VehicleNumber'] for row in cursor.fetchall()]

    cursor.close()
    conn.close()

    # Hitung FuelUsed (BBM)
    efficiency_km_per_liter = 10
    previous_odom = None
    rows = [dict(r) for r in rows]  # konversi Row ke dict
    for row in rows:
        if previous_odom is not None and row['Odometer'] is not None and row['Engine'] == 'ON':
            delta_meter = max(0, row['Odometer'] - previous_odom)
            delta_km = delta_meter / 1000
            row['FuelUsed'] = round(delta_km / efficiency_km_per_liter, 2)
        else:
            row['FuelUsed'] = 0.0
        previous_odom = row['Odometer']

    return render_template("kendaraan_db.html", rows=rows, all_plates=all_plates)


@app.route('/historical', methods=['GET', 'POST'])
def historical_data():
    data = []
    error = None
    token = get_token()

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


@app.route('/fuel-analysis', methods=['GET', 'POST'])
def fuel_analysis():
    conn = get_db_connection()
    
    # Get all plates for dropdown
    cursor = conn.execute("SELECT DISTINCT VehicleNumber FROM gps_data ORDER BY VehicleNumber")
    all_plates = [row['VehicleNumber'] for row in cursor.fetchall()]
    
    fuel_summary = None
    daily_consumption = None
    vehicle_analysis = None
    detailed_records = None
    
    if request.method == 'POST':
        plate = request.form.get('plate')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        efficiency = float(request.form.get('efficiency', 10))
        fuel_price = 10000  # Rp 10,000 per liter
        
        # Build query
        query = """
            SELECT VehicleNumber, DatetimeUTC, Odometer, Speed, Engine, Lat, Lon
            FROM gps_data 
            WHERE Engine = 'ON' AND Odometer IS NOT NULL
        """
        params = []
        
        if plate:
            query += " AND VehicleNumber = ?"
            params.append(plate)
        if start_date:
            query += " AND DatetimeUTC >= ?"
            params.append(start_date + " 00:00:00")
        if end_date:
            query += " AND DatetimeUTC <= ?"
            params.append(end_date + " 23:59:59")
            
        query += " ORDER BY VehicleNumber, DatetimeUTC"
        
        cursor = conn.execute(query, params)
        records = cursor.fetchall()
        
        # Calculate fuel consumption
        vehicle_data = {}
        daily_data = {}
        detailed_list = []
        
        # Sort records by vehicle and datetime to ensure proper odometer sequence
        records_by_vehicle = {}
        for record in records:
            vehicle = record['VehicleNumber']
            if vehicle not in records_by_vehicle:
                records_by_vehicle[vehicle] = []
            records_by_vehicle[vehicle].append(record)
        
        # Sort each vehicle's records by datetime
        for vehicle in records_by_vehicle:
            records_by_vehicle[vehicle].sort(key=lambda x: x['DatetimeUTC'])
        
        # Process each vehicle's records
        for vehicle, vehicle_records in records_by_vehicle.items():
            vehicle_data[vehicle] = {
                'total_distance': 0,
                'total_fuel': 0,
            }
            
            prev_odo = None
            for record in vehicle_records:
                date = record['DatetimeUTC'][:10]
                current_odo = record['Odometer']
                
                if date not in daily_data:
                    daily_data[date] = {'distance': 0, 'fuel': 0}
                
                # Calculate distance and fuel only if we have a previous odometer reading
                if prev_odo is not None and current_odo is not None and current_odo > prev_odo:
                    distance_m = current_odo - prev_odo
                    distance_km = distance_m / 1000
                    
                    # Only count if distance is reasonable (less than 500km between readings)
                    if distance_km > 0 and distance_km < 500:
                        fuel_consumed = distance_km / efficiency
                        
                        vehicle_data[vehicle]['total_distance'] += distance_km
                        vehicle_data[vehicle]['total_fuel'] += fuel_consumed
                        daily_data[date]['distance'] += distance_km
                        daily_data[date]['fuel'] += fuel_consumed
                        
                        detailed_list.append({
                            'datetime': record['DatetimeUTC'],
                            'plate': vehicle,
                            'distance': round(distance_km, 2),
                            'fuel': round(fuel_consumed, 2),
                            'speed': record['Speed'] or 0,
                            'engine': record['Engine']
                        })
                
                prev_odo = current_odo
        
        # Calculate summary
        total_distance = sum(v['total_distance'] for v in vehicle_data.values())
        total_fuel = sum(v['total_fuel'] for v in vehicle_data.values())
        avg_efficiency = total_distance / total_fuel if total_fuel > 0 else 0
        estimated_cost = total_fuel * fuel_price
        
        fuel_summary = {
            'total_distance': total_distance,
            'total_fuel': total_fuel,
            'avg_efficiency': avg_efficiency,
            'estimated_cost': estimated_cost
        }
        
        # Prepare daily consumption for chart
        if daily_data:
            sorted_dates = sorted(daily_data.keys())
            daily_consumption = {
                'dates': sorted_dates,
                'distance': [daily_data[d]['distance'] for d in sorted_dates],
                'fuel': [daily_data[d]['fuel'] for d in sorted_dates]
            }
        
        # Prepare vehicle analysis
        vehicle_analysis = []
        for vehicle, data in vehicle_data.items():
            if data['total_distance'] > 0:
                eff = data['total_distance'] / data['total_fuel'] if data['total_fuel'] > 0 else 0
                cost = data['total_fuel'] * fuel_price
                vehicle_analysis.append({
                    'plate': vehicle,
                    'distance': data['total_distance'],
                    'fuel': data['total_fuel'],
                    'efficiency': eff,
                    'cost': cost
                })
        
        # Sort by efficiency descending
        vehicle_analysis.sort(key=lambda x: x['efficiency'], reverse=True)
        
        # Limit detailed records to last 100
        detailed_records = detailed_list[-100:]
    
    conn.close()
    
    return render_template("fuel_analysis.html",
                         all_plates=all_plates,
                         fuel_summary=fuel_summary,
                         daily_consumption=daily_consumption,
                         vehicle_analysis=vehicle_analysis,
                         detailed_records=detailed_records)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

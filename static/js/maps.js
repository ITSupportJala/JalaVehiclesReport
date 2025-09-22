document.addEventListener("DOMContentLoaded", function () {
  const map = L.map('map').setView([-1.5, 120.5], 6); // Center Indonesia

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '© OpenStreetMap contributors'
  }).addTo(map);

  if (!markers || markers.length === 0) return;

  const latlngs = markers.map(p => [p.Lat, p.Lon]);
  const polyline = L.polyline(latlngs, {
    color: '#007bff',
    weight: 4,
    opacity: 0.8
  }).addTo(map);

  map.fitBounds(polyline.getBounds());

  const clusterGroup = L.markerClusterGroup();

  // Start Marker
  const start = markers[0];
  clusterGroup.addLayer(
    L.marker([start.Lat, start.Lon], { icon: startIcon() })
      .bindPopup(`<b>START</b><br>${start.DatetimeUTC}`)
  );

  // Finish Marker
  const end = markers[markers.length - 1];
  clusterGroup.addLayer(
    L.marker([end.Lat, end.Lon], { icon: finishIcon() })
      .bindPopup(`<b>FINISH</b><br>${end.DatetimeUTC}`)
  );

  // Deteksi STOP dan PARKIR
  for (let i = 1; i < markers.length; i++) {
    const p = markers[i];
    const latlng = [p.Lat, p.Lon];

    const isParkir = p.engine == 0 && p.speed == 0;
    const isStop = p.speed == 0 && p.engine != 0;

    if (isParkir) {
      clusterGroup.addLayer(
        L.marker(latlng, { icon: parkingIcon() })
          .bindPopup(`<b>PARKIR</b><br>${p.DatetimeUTC}`)
      );
    } else if (isStop) {
      clusterGroup.addLayer(
        L.marker(latlng, { icon: stopIcon() })
          .bindPopup(`<b>STOP</b><br>${p.DatetimeUTC}`)
      );
    } else {
      clusterGroup.addLayer(
        L.marker(latlng, { icon: smallIcon() })
          .bindPopup(`<b>POSISI</b><br>${p.DatetimeUTC}`)
      );
    }
  }

  map.addLayer(clusterGroup);

  // Custom Marker Icons
  function startIcon() {
    return L.icon({
      iconUrl: "/static/img/start-icon.png",
      iconSize: [32, 32],
      iconAnchor: [16, 32]
    });
  }

  function finishIcon() {
    return L.icon({
      iconUrl: "/static/img/finish-icon.png",
      iconSize: [32, 32],
      iconAnchor: [16, 32]
    });
  }

  function parkingIcon() {
    return L.icon({
      iconUrl: "/static/img/parking-icon.png",
      iconSize: [24, 24],
      iconAnchor: [12, 24]
    });
  }

  function stopIcon() {
    return L.icon({
      iconUrl: "/static/img/stop-icon.png",
      iconSize: [24, 24],
      iconAnchor: [12, 24]
    });
  }

  function smallIcon() {
    return L.icon({
      iconUrl: "/static/img/small-icon.png",
      iconSize: [15, 15]
    });
  }
});

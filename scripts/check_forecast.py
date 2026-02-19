import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

cities = {
    'Chicago': (41.88, -87.63),
    'Los_Angeles': (34.05, -118.24),
    'Denver': (39.74, -104.98),
    'Philadelphia': (39.95, -75.17),
    'Tel_Aviv': (32.08, 34.78),
    'NYC': (40.71, -74.01),
}

for city, (lat, lon) in cities.items():
    r = requests.get('https://api.open-meteo.com/v1/forecast', params={
        'latitude': lat, 'longitude': lon,
        'daily': 'temperature_2m_max,temperature_2m_min',
        'temperature_unit': 'fahrenheit',
        'timezone': 'America/New_York',
        'forecast_days': 3,
    }, timeout=8)
    d = r.json()['daily']
    for i, dt in enumerate(d['time']):
        hi = d['temperature_2m_max'][i]
        lo = d['temperature_2m_min'][i]
        print(f"  {city}: {dt}  High={hi}F  Low={lo}F")
    print()

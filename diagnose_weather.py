"""Manually run weather evaluation logic against current REST data to diagnose 0-trade problem."""
import sys, math
from datetime import datetime, timezone, timedelta
from kalshi_bot import KalshiAPI
from config import Config

sys.path.insert(0, '.')
from ws_trader import WSConvergenceTrader

bot = WSConvergenceTrader()
api = KalshiAPI(api_key=Config.KALSHI_API_KEY)
now = datetime.now(timezone.utc)

print(f"UTC: {now.strftime('%Y-%m-%d %H:%M')}")
print(f"base_sigma={bot.weather_base_sigma}, min_edge={bot.weather_min_edge_pct}%, min_conf={bot.weather_min_conf}")
print(f"no_max={bot.weather_no_max_price}c, yes_max={bot.weather_yes_max_price}c, atm_mult={bot.weather_atm_sigma_mult}, max_hrs={bot.weather_max_expiry_hours}h")
print()

today = now.strftime('%y') + now.strftime('%b').upper() + now.strftime('%d')
tom   = (now + timedelta(days=1)).strftime('%y') + (now + timedelta(days=1)).strftime('%b').upper() + (now + timedelta(days=1)).strftime('%d')
print(f"Scanning events: {today} and {tom}\n")

for ws_series, ws_info in bot.weather_series.items():
    mkts = api.get_all_markets(status='open', series_ticker=ws_series)
    for m in mkts:
        et = m.get('event_ticker', '')
        if today not in et and tom not in et:
            continue
        ct = m.get('close_time', '')
        try:
            close = datetime.fromisoformat(ct.replace('Z', '+00:00'))
        except Exception:
            continue
        mins_left = (close - now).total_seconds() / 60
        hrs_left = mins_left / 60.0
        if mins_left < 60 or mins_left > bot.weather_max_expiry_hours * 60:
            continue

        parts = et.split('-')
        event_date = parts[-1] if len(parts) >= 2 else ''
        fs = m.get('floor_strike')
        cs = m.get('cap_strike')
        strike_type = m.get('strike_type', 'between')
        ticker = m.get('ticker', '')
        yb = m.get('yes_bid', 0) or 0
        ya = m.get('yes_ask', 0) or 0
        no_ask = (100 - yb) if yb > 0 else 0

        ts = bot._get_weather_temp_and_sigma(ws_series, event_date, hrs_left)
        if not ts:
            print(f"  {ticker}: NO FORECAST DATA")
            continue
        forecast_temp, sigma = ts
        model_prob = bot.weather_probability(forecast_temp, sigma, fs, cs, strike_type)
        model_prob_no = 1.0 - model_prob

        # ATM check
        is_atm = False
        if strike_type == 'between' and fs is not None and cs is not None:
            bracket_center = (fs + cs) / 2.0
            if fs <= forecast_temp < cs:
                is_atm = True
            elif abs(forecast_temp - bracket_center) < sigma * bot.weather_atm_sigma_mult:
                is_atm = True

        implied_no = no_ask / 100.0
        edge_no = (model_prob_no - implied_no) * 100

        flags = []
        if is_atm:
            flags.append("ATM_SKIP")
        if no_ask == 0:
            flags.append("NO_LIQ")
        elif no_ask > bot.weather_no_max_price:
            flags.append(f"NO_TOO_EXP({no_ask}>{bot.weather_no_max_price})")
        if no_ask > 0 and edge_no < bot.weather_min_edge_pct:
            flags.append(f"EDGE_LOW({edge_no:.1f}<{bot.weather_min_edge_pct})")
        if no_ask > 0 and model_prob_no < bot.weather_min_conf:
            flags.append(f"CONF_LOW({model_prob_no*100:.0f}<{bot.weather_min_conf*100:.0f})")

        status = "*** TRADE! ***" if not flags else "/".join(flags)
        print(f"  {ticker:<42} fcast={forecast_temp:.1f} sig={sigma:.1f} "
              f"mp_no={model_prob_no*100:.0f}% edge={edge_no:.1f}% no={no_ask}c {hrs_left:.1f}h  => {status}")

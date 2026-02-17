"""Quick test: connect to Kalshi WebSocket and read a few messages."""
import asyncio
import json
import time
import base64
import websockets
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend
from config import Config

pk_pem = Config.load_private_key()
private_key = serialization.load_pem_private_key(pk_pem.encode('utf-8'), password=None, backend=default_backend())
api_key = Config.KALSHI_API_KEY

ws_path = '/trade-api/ws/v2'
timestamp = str(int(time.time() * 1000))
msg_bytes = (timestamp + 'GET' + ws_path).encode('utf-8')
sig = private_key.sign(
    msg_bytes,
    asym_padding.PSS(mgf=asym_padding.MGF1(hashes.SHA256()), salt_length=asym_padding.PSS.DIGEST_LENGTH),
    hashes.SHA256()
)
headers = {
    'KALSHI-ACCESS-KEY': api_key,
    'KALSHI-ACCESS-TIMESTAMP': timestamp,
    'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode('ascii'),
}


async def test():
    url = 'wss://api.elections.kalshi.com/trade-api/ws/v2'
    print(f'Connecting to {url}...')
    async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
        print('CONNECTED!')

        # Subscribe to ticker for all markets
        sub = {"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"]}}
        await ws.send(json.dumps(sub))
        print('Sent ticker subscription')

        # Read messages
        for i in range(8):
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(raw)
            mtype = data.get('type', '?')
            msg = data.get('msg', {})

            if mtype == 'subscribed':
                print(f'  Subscribed: channel={msg.get("channel")} sid={msg.get("sid")}')
            elif mtype == 'ticker':
                tk = msg.get('market_ticker', '')
                bid = msg.get('yes_bid', '')
                ask = msg.get('yes_ask', '')
                vol = msg.get('volume', '')
                print(f'  Ticker: {tk}  bid={bid}  ask={ask}  vol={vol}')
            elif mtype == 'error':
                print(f'  Error: code={msg.get("code")} msg={msg.get("msg")}')
            else:
                print(f'  {mtype}: {json.dumps(data)[:200]}')

        await ws.close()
        print('Done! WebSocket works.')


if __name__ == '__main__':
    asyncio.run(test())

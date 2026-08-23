import requests, time

t1 = time.time()
resp = requests.post('http://127.0.0.1:6500/api/download/album-list', json={'album_id': 40121646}, timeout=60)
t2 = time.time()
print(f'Status: {resp.status_code}, Time: {t2-t1:.2f}s')
data = resp.json()
success = data.get('success')
tracks = data.get('tracks', [])
total = data.get('track_total')
print(f'Success: {success}, Tracks: {len(tracks)}, Total: {total}')
if tracks:
    print(f'First: {tracks[0]["title"]}')
    print(f'Last: {tracks[-1]["title"]}')

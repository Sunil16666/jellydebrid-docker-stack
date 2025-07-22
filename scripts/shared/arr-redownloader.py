import os
import requests
import threading
import time
from collections import defaultdict


class RadarrInstance:
    """Base class for Radarr instances with common functionality."""

    def __init__(self, name, url, api_key):
        self.name = name
        self.url = url.rstrip('/')
        self.api_key = api_key

    def get_custom_headers(self) -> dict:
        return {'X-Api-Key': self.api_key}

    def get_custom_payload(self, queue_ids: list[int]) -> dict:
        """Prepare payload for deleting queue items and triggering redownloads."""
        return {'ids': queue_ids}

    def fetch_queue(self) -> list[dict]:
        """Fetch the current download queue from Radarr and return list of records."""
        resp = requests.get(f"{self.url}/api/v3/queue", headers=self.get_custom_headers())
        if resp.status_code != 200:
            print(f"❌ {self.name}: queue HTTP {resp.status_code}\n{resp.text}")
            return []
        try:
            data = resp.json()
        except ValueError as ve:
            print(f"❌ {self.name}: failed JSON decode: {ve}\n{resp.text}")
            return []
        records = data.get('records') if isinstance(data, dict) else data
        return records if isinstance(records, list) else []


class SonarrInstance:
    """Base class for Sonarr instances with common functionality."""

    def __init__(self, name, url, api_key):
        self.name = name
        self.url = url.rstrip('/')
        self.api_key = api_key

    def get_custom_headers(self) -> dict:
        return {'X-Api-Key': self.api_key}

    def get_custom_payload(self, queue_ids: list[int]) -> dict:
        """Prepare payload for deleting queue items and triggering redownloads."""
        return {'ids': queue_ids}

    def fetch_queue(self) -> list[dict]:
        """Fetch the current download queue from Sonarr and return list of records."""
        resp = requests.get(f"{self.url}/api/v3/queue", headers=self.get_custom_headers())
        if resp.status_code != 200:
            print(f"❌ {self.name}: queue HTTP {resp.status_code}\n{resp.text}")
            return []
        try:
            data = resp.json()
        except ValueError as ve:
            print(f"❌ {self.name}: failed JSON decode: {ve}\n{resp.text}")
            return []
        records = data.get('records') if isinstance(data, dict) else data
        return records if isinstance(records, list) else []


class RDTClient:
    """Client for interacting with RDTClient API (qBittorrent-compatible)."""

    def __init__(self, username: str, password: str, url: str = 'gluetun:8080'):
        self.username = username
        self.password = password
        self.url = url
        self.session = requests.Session()
        self.authenticate()

    def authenticate(self):
        """Authenticate with RDTClient (retry until ready)."""
        for attempt in range(6):
            try:
                resp = self.session.post(
                    f"http://{self.url}/api/v2/auth/login",
                    data={'username': self.username, 'password': self.password},
                    headers={'Referer': 'http://rdtclient/'}
                )
                resp.raise_for_status()
                print("✅ Successfully authenticated with RDTClient")
                return
            except requests.exceptions.RequestException as e:
                print(f"⚠️ RDTClient auth attempt {attempt+1} failed: {e}")
                time.sleep(5)
        raise RuntimeError("RDTClient authentication failed after retries")

    def list_downloading(self) -> list[dict]:
        resp = self.session.get(f"http://{self.url}/api/v2/torrents/info?filter=downloading")
        resp.raise_for_status()
        return resp.json()

    def list_all(self) -> list[dict]:
        resp = self.session.get(f"http://{self.url}/api/v2/torrents/info")
        resp.raise_for_status()
        return resp.json()


class ArrRedownloader:
    """Class to handle redownloading items in Radarr/Sonarr based on slow torrents."""

    CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 5))    # seconds between checks
    RETRY_DELAY = int(os.environ.get('RETRY_DELAY', 30))        # seconds below threshold
    SAFE_PROGRESS = float(os.environ.get('SAFE_PROGRESS', 0.90))# skip if >= this fraction
    GRACE_PERIOD = int(os.environ.get('GRACE_PERIOD', 30))     # seconds to allow startup before checks
    RETRY_COOLDOWN  = int(os.environ.get('RETRY_COOLDOWN', 30))

    def __init__(self,
                 rdtclient: RDTClient,
                 radarr_instances: list[RadarrInstance],
                 sonarr_instances: list[SonarrInstance],
                 bandwidth: int = 17500):  # KiB/s
        self.rdt = rdtclient
        self.radarr_instances = radarr_instances
        self.sonarr_instances = sonarr_instances
        self.bandwidth = bandwidth

        # state: hash → {first_below, handled, safe_skip_until}
        self._torrent_state = {}
        # mapping: hash → [(type, instance, [queue_ids])]
        self.mapping = defaultdict(list)
        # when mapping established: hash → timestamp
        self._mapping_time = {}
        
        self._last_retry = {}

        self._stop_event = threading.Event()
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def fetch_mappings_for_hash(self, h: str):
        """Find queue item IDs in each Arr for a given torrent hash."""
        found = []
        lower_hash = h.lower()
        for inst in self.radarr_instances:
            for item in inst.fetch_queue():
                if item.get('downloadId', '').lower() == lower_hash:
                    found.append(('radarr', inst, [item['id']]))
        for inst in self.sonarr_instances:
            for item in inst.fetch_queue():
                if item.get('downloadId', '').lower() == lower_hash:
                    found.append(('sonarr', inst, [item['id']]))
        return found

    def _update_mappings(self):
        torrents = self.rdt.list_downloading()
        for t in torrents:
            h = t['hash']
            if h not in self.mapping:
                for inst_type, inst, qids in self.fetch_mappings_for_hash(h):
                    print(f"🔗 Mapping hash {h[:8]}... → {inst_type} {inst.name} queue IDs {qids}")
                    self.mapping[h].append((inst_type, inst, qids))
                    # record mapping time
                    self._mapping_time.setdefault(h, time.time())

    def _trigger_redownload(self, h: str):
        for inst_type, inst, qids in self.mapping.get(h, []):
            payload = inst.get_custom_payload(qids)
            print(f"🔄 Triggering redownload for {inst_type.title()} {inst.name} queue IDs {qids}...")
            try:
                resp = requests.delete(
                    f"{inst.url}/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false&changeCategory=false",
                    headers=inst.get_custom_headers(),
                    json=payload
                )
                self._last_retry[h] = time.time()
                resp.raise_for_status()
                print(f"🔄 {inst_type.title()} {inst.name} redownload queue {qids}: OK")
            except Exception as e:
                print(f"❌ {inst_type.title()} {inst.name} delete queue {qids} failed: {e}")

    def _check_speeds_and_retry(self):
        torrents = {t['hash']: t for t in self.rdt.list_downloading()}
        count = len(torrents)
        
        # Calculate speeds
        speeds = [t.get('dlspeed', 0) / 1024 for t in torrents.values()]
        total_speed = sum(speeds)
        avg_speed = total_speed / count if count > 0 else 0
        
        # Use minimum speed threshold instead of equal distribution
        # This accounts for RDTClient's dynamic bandwidth allocation
        MIN_SPEED_THRESHOLD = int(os.environ.get('MIN_SPEED_THRESHOLD', 1000))  # KiB/s
        
        # Only use bandwidth-based threshold if total speed is significantly below bandwidth
        if total_speed < self.bandwidth * 0.7:  # 70% of bandwidth
            threshold = self.bandwidth / (count or 1) / 3
        else:
            # Use minimum threshold or 25% of average (whichever is higher)
            threshold = max(MIN_SPEED_THRESHOLD, avg_speed * 0.25)
            
        now = time.time()
        print(f"📈 Speed check: {count} torrents, total {total_speed:.1f} KiB/s, avg {avg_speed:.1f} KiB/s, threshold {threshold:.1f} KiB/s")

        for h, data in torrents.items():
            # skip if still in grace period
            mapped_at = self._mapping_time.get(h, 0)
            age = now - mapped_at
            if age < self.GRACE_PERIOD:
                print(f"⏱ Skipping {h[:8]}: in grace period ({age:.1f}s/<{self.GRACE_PERIOD}s)")
                continue
            
            last = self._last_retry.get(h, 0)
            if now - last < self.RETRY_COOLDOWN:
                print(f"⏱ Skipping {h[:8]}: cooling down ({now-last:.0f}s/<{self.RETRY_COOLDOWN}s)")
                continue 
            progress = data.get('progress', 0)
            speed_kib = data.get('dlspeed', 0) / 1024
            print(f"🔍 {h[:8]} prog {progress*100:.1f}% speed {speed_kib:.1f} KiB/s")

            # Check if torrent reached safe progress - if so, skip monitoring for 4 minutes
            state = self._torrent_state.setdefault(h, {'first_below': None, 'handled': False, 'safe_skip_until': 0})
            
            if progress >= self.SAFE_PROGRESS:
                if state['safe_skip_until'] == 0:  # First time reaching safe progress
                    state['safe_skip_until'] = now + 240  # Skip for 4 minutes (240 seconds)
                    print(f"🏁 {h[:8]} reached {progress*100:.1f}% - skipping monitoring for 4 minutes")
                continue
            
            # Check if we're still in the safe skip period
            if now < state['safe_skip_until']:
                remaining = state['safe_skip_until'] - now
                print(f"⏭️ Skipping {h[:8]}: in safe completion period ({remaining:.0f}s remaining)")
                continue

            # Continue with normal speed checking
            if speed_kib < threshold:
                if state['first_below'] is None:
                    state['first_below'] = now
                elif not state['handled'] and now - state['first_below'] > self.RETRY_DELAY:
                    print(f"🐢 Slow! {speed_kib:.1f} < {threshold:.1f}, retrying")
                    self._trigger_redownload(h)
                    state['handled'] = True
            else:
                # Reset the slow tracking when speed is good
                state.update({'first_below': None, 'handled': False})

    def _cleanup_finished(self):
        active = {t['hash'] for t in self.rdt.list_all()}
        for h in list(self._torrent_state):
            if h not in active:
                self._torrent_state.pop(h)
                self.mapping.pop(h, None)
                self._mapping_time.pop(h, None)

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            print("🔄 Monitor iteration start")
            self._update_mappings()
            self._check_speeds_and_retry()
            self._cleanup_finished()
            time.sleep(self.CHECK_INTERVAL)

    def stop(self):
        self._stop_event.set()


if __name__ == '__main__':
    rdt = RDTClient(
        os.environ['RDTCLIENT_USERNAME'],
        os.environ['RDTCLIENT_PASSWORD'],
        os.environ.get('RDTCLIENT_HOST', 'gluetun:8080')
    )
    radarr_instances = [
        RadarrInstance('1080p-en', os.environ['RADARR_1080P_EN_URL'], os.environ['RADARR_1080P_EN_KEY']),
        RadarrInstance('1080p-de', os.environ['RADARR_1080P_DE_URL'], os.environ['RADARR_1080P_DE_KEY']),
        RadarrInstance('2160p-en', os.environ['RADARR_2160P_EN_URL'], os.environ['RADARR_2160P_EN_KEY']),
        RadarrInstance('2160p-de', os.environ['RADARR_2160P_DE_URL'], os.environ['RADARR_2160P_DE_KEY']),
    ]
    sonarr_instances = [
        SonarrInstance('1080p-en', os.environ['SONARR_1080P_EN_URL'], os.environ['SONARR_1080P_EN_KEY']),
        SonarrInstance('1080p-de', os.environ['SONARR_1080P_DE_URL'], os.environ['SONARR_1080P_DE_KEY']),
        SonarrInstance('2160p-en', os.environ['SONARR_2160P_EN_URL'], os.environ['SONARR_2160P_EN_KEY']),
        SonarrInstance('2160p-de', os.environ['SONARR_2160P_DE_URL'], os.environ['SONARR_2160P_DE_KEY']),
    ]
    downloader = ArrRedownloader(
        rdt,
        radarr_instances,
        sonarr_instances,
        bandwidth=int(os.environ.get('DOWNLOAD_BANDWIDTH_KB', 19000))
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        downloader.stop()

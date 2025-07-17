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
    
    def get_custom_payload(self, movie_ids: list[int]) -> dict:
        """Prepare payload for deleting queue and redownloading movies."""
        return {'ids': movie_ids}

    def fetch_queue(self) -> list[dict]:
        """Fetch the current download queue from Radarr and normalize to list of records."""
        resp = requests.get(f"{self.url}/api/v3/queue", headers=self.get_custom_headers())
        if resp.status_code != 200:
            print(f"❌ {self.name}: queue HTTP {resp.status_code}\n{resp.text}")
            return []
        try:
            data = resp.json()
        except ValueError as ve:
            print(f"❌ {self.name}: failed JSON decode: {ve}\n{resp.text}")
            return []
        # Radarr returns a dict with 'records'
        if isinstance(data, dict) and 'records' in data:
            return data['records']
        if isinstance(data, list):
            return data
        print(f"❌ {self.name}: unexpected queue format (expected list or dict with records): {data}")
        return []



class SonarrInstance:
    """Base class for Sonarr instances with common functionality."""
    
    def __init__(self, name, url, api_key):
        self.name = name
        self.url = url.rstrip('/')
        self.api_key = api_key

    def get_custom_headers(self) -> dict:
        return {'X-Api-Key': self.api_key}
    
    def get_custom_payload(self, series_ids: list[int]) -> dict:
        """Prepare payload for deleting queue and redownloading series."""
        return {'seriesIds': series_ids}

    def fetch_queue(self) -> list[dict]:
        """Fetch the current download queue from Sonarr and normalize to list records."""
        resp = requests.get(f"{self.url}/api/v3/queue", headers=self.get_custom_headers())
        if resp.status_code != 200:
            print(f"❌ {self.name}: queue HTTP {resp.status_code}\n{resp.text}")
            return []
        try:
            data = resp.json()
        except ValueError as ve:
            print(f"❌ {self.name}: failed JSON decode: {ve}\n{resp.text}")
            return []
        # Sonarr may also return paged dict
        if isinstance(data, dict) and 'records' in data:
            return data['records']
        if isinstance(data, list):
            return data
        print(f"❌ {self.name}: unexpected queue format (expected list or dict with records): {data}")
        return []


class RDTClient:
    """Client for interacting with RDTClient API (qBittorrent-compatible)."""
    
    def __init__(self, username: str, password: str, url: str = 'gluetun'):
        self.username = username
        self.password = password
        self.url = url
        self.session = requests.Session()
        self.authenticate()
        
    def authenticate(self):
        """Authenticate with RDTClient."""
        response = self.session.post(
            f'http://{self.url}/api/v2/auth/login',
            data={'username': self.username, 'password': self.password},
            headers={'Referer': 'http://rdtclient/'}
        )
        response.raise_for_status()
        print("✅ Successfully authenticated with RDTClient")

    def list_downloading(self) -> list[dict]:
        """List all downloading torrents."""
        resp = self.session.get(f'http://{self.url}/api/v2/torrents/info?filter=downloading')
        resp.raise_for_status()
        return resp.json()

    def list_all(self) -> list[dict]:
        """List all torrents (any state)."""
        resp = self.session.get(f'http://{self.url}/api/v2/torrents/info')
        resp.raise_for_status()
        return resp.json()


class ArrRedownloader:
    """Class to handle redownloading items in Radarr/Sonarr based on slow torrents."""
    
    CHECK_INTERVAL = 10      # seconds between monitoring checks (ref. _monitor_loop)
    RETRY_DELAY    = 60      # seconds below threshold before redownload (ref. _check_speeds_and_retry)
    SAFE_PROGRESS = 0.9      # 90% progress considered safe to proceed with current download
    
    def __init__(self,
                 rdtclient: RDTClient,
                 radarr_instances: list[RadarrInstance],
                 sonarr_instances: list[SonarrInstance],
                 bandwidth: int = 175):
        self.rdt = rdtclient
        self.radarr_instances = radarr_instances
        self.sonarr_instances = sonarr_instances
        self.bandwidth = bandwidth

        # torrent hash → { first_below: timestamp, handled: bool }
        self._torrent_state: dict[str, dict] = {}

        # mapping torrent_hash → list of (instance_type, instance_obj, [media_ids])
        self.mapping = defaultdict(list)

        self._stop_event = threading.Event()
        # I'm aware atomcity is not guranteed but this thread is the only one calling the functions that modify the mapping, so it should be fine.
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def register_mapping(self, torrent_hash: str, instance_type: str, instance_obj, media_ids: list[int]):
        """Register a mapping of hash → (radarr/sonarr, instance, media IDs)"""
        self.mapping[torrent_hash].append((instance_type, instance_obj, media_ids))

    def fetch_mappings_for_hash(self, h: str):
        """Query each *Arr instance to find media IDs for a given torrent hash."""
        found = []
        # Radarr
        for inst in self.radarr_instances:
            try:
                for item in inst.fetch_queue():
                    if item.get('downloadId') == h:
                        found.append(('radarr', inst, [item['movieId']]))
            except Exception as e:
                print(f"❌ Error fetching Radarr queue for {inst.name}: {e}")
        # Sonarr
        for inst in self.sonarr_instances:
            try:
                for item in inst.fetch_queue():
                    if item.get('downloadId') == h:
                        found.append(('sonarr', inst, [item['seriesId']]))
            except Exception as e:
                print(f"❌ Error fetching Sonarr queue for {inst.name}: {e}")
        return found

    def _update_mappings(self):
        """Ensure we have mappings for all active downloading torrents."""
        torrents = self.rdt.list_downloading()
        for t in torrents:
            h = t['hash']
            if h not in self.mapping:
                maps = self.fetch_mappings_for_hash(h)
                for inst_type, inst, mids in maps:
                    print(f"🔗 Mapping hash {h} → {inst_type} {inst.name} IDs {mids}")
                    self.register_mapping(h, inst_type, inst, mids)

    def del_queue_bulk_redownload_radarr(self, movie_ids: list[int]) -> bool:
        for instance in self.radarr_instances:
            headers = instance.get_custom_headers()
            try:
                resp = requests.delete(
                    f"{instance.url}/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false&changeCategory=false",
                    headers=headers,
                    json=instance.get_custom_payload(movie_ids)
                )
                if resp.status_code == 200:
                    print(f"✅ Removed & requeued in Radarr {instance.name}: {movie_ids}")
                else:
                    print(f"❌ Radarr {instance.name} failed: {resp.status_code} {resp.text}")
                    return False
            except Exception as e:
                print(f"❌ Radarr {instance.name} error: {e}")
                return False
        return True

    def del_queue_bulk_redownload_sonarr(self, series_ids: list[int]) -> bool:
        for instance in self.sonarr_instances:
            headers = instance.get_custom_headers()
            try:
                resp = requests.delete(
                    f"{instance.url}/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false&changeCategory=false",
                    headers=headers,
                    json=instance.get_custom_payload(series_ids)
                )
                if resp.status_code == 200:
                    print(f"✅ Removed & requeued in Sonarr {instance.name}: {series_ids}")
                else:
                    print(f"❌ Sonarr {instance.name} failed: {resp.status_code} {resp.text}")
                    return False
            except Exception as e:
                print(f"❌ Sonarr {instance.name} error: {e}")
                return False
        return True

    def _trigger_redownload(self, h: str):
        """Trigger redownload for a given torrent hash by using registered mappings."""
        for inst_type, inst, mids in self.mapping.get(h, []):
            if inst_type == 'radarr':
                ok = self.del_queue_bulk_redownload_radarr(mids)
            else:
                ok = self.del_queue_bulk_redownload_sonarr(mids)
            print(f"🔄 {inst_type.title()} {inst.name} redownload {mids}: {'OK' if ok else 'FAIL'}")

    def _check_speeds_and_retry(self):
        """Check torrent speeds and trigger redownloads for slow torrents, respecting progress safeguard."""
        torrents = { t['hash']: t for t in self.rdt.list_downloading() }
        num = len(torrents)
        threshold = self.bandwidth / max(num,1) / 1 # Just for testing purpose
        now = time.time()

        for h, data in torrents.items():
            progress = data.get('progress', 0.0)
            if progress >= self.SAFE_PROGRESS:
                continue
            speed_kib = data['dlspeed'] / 1024
            state = self._torrent_state.setdefault(h, {'first_below': None, 'handled': False})

            if speed_kib < threshold:
                if state['first_below'] is None:
                    state['first_below'] = now
                elif not state['handled'] and (now - state['first_below'] > self.RETRY_DELAY):
                    print(f"🐢 Slow torrent {h}: {speed_kib:.1f} KiB/s < {threshold:.1f}, progress {progress*100:.1f}%")
                    self._trigger_redownload(h)
                    state['handled'] = True
            else:
                state['first_below'], state['handled'] = None, False

    def _cleanup_finished(self):
        """Cleanup state and mappings for torrents no longer present."""
        active = { t['hash'] for t in self.rdt.list_all() }
        for h in list(self._torrent_state):
            if h not in active:
                self._torrent_state.pop(h, None)
                self.mapping.pop(h, None)

    def _monitor_loop(self):
        """Background loop to update mappings, check speeds, trigger retries, and cleanup."""
        while not self._stop_event.is_set():
            self._update_mappings()
            self._check_speeds_and_retry()
            self._cleanup_finished()
            time.sleep(self.CHECK_INTERVAL)

    def stop(self):
        """Stop the background monitor."""
        self._stop_event.set()


if __name__ == '__main__':
    import os

    rdt_user = os.environ['RDTCLIENT_USERNAME']
    rdt_pass = os.environ['RDTCLIENT_PASSWORD']
    rdt_host = os.environ.get('RDTCLIENT_HOST', 'gluetun')

    # Build RDTClient
    rdt = RDTClient(rdt_user, rdt_pass, url=rdt_host)

    # Radarr instances
    radarr_instances = [
        RadarrInstance('1080p-en', os.environ['RADARR_1080P_EN_URL'], os.environ['RADARR_1080P_EN_KEY']),
        RadarrInstance('1080p-de', os.environ['RADARR_1080P_DE_URL'], os.environ['RADARR_1080P_DE_KEY']),
        RadarrInstance('2160p-en', os.environ['RADARR_2160P_EN_URL'], os.environ['RADARR_2160P_EN_KEY']),
        RadarrInstance('2160p-de', os.environ['RADARR_2160P_DE_URL'], os.environ['RADARR_2160P_DE_KEY']),
    ]

    # Sonarr instances
    sonarr_instances = [
        SonarrInstance('1080p-en', os.environ['SONARR_1080P_EN_URL'], os.environ['SONARR_1080P_EN_KEY']),
        SonarrInstance('1080p-de', os.environ['SONARR_1080P_DE_URL'], os.environ['SONARR_1080P_DE_KEY']),
        SonarrInstance('2160p-en', os.environ['SONARR_2160P_EN_URL'], os.environ['SONARR_2160P_EN_KEY']),
        SonarrInstance('2160p-de', os.environ['SONARR_2160P_DE_URL'], os.environ['SONARR_2160P_DE_KEY']),
    ]

    # Optional: override bandwidth via env
    bw = int(os.environ.get('DOWNLOAD_BANDWIDTH_KB', 22000))  # default 22 MB/s

    # Launch watcher
    downloader = ArrRedownloader(rdt, radarr_instances, sonarr_instances, bandwidth=bw)

    # Keep the container alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        downloader.stop()

    
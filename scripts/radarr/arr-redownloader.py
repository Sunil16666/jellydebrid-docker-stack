import os
import requests
import threading
import time
from collections import defaultdict

class RadarrInstance:
    """Base class for Radarr instances with common functionality."""
    
    def __init__(self, name, url, api_key):
        self.name = name
        self.url = url
        self.api_key = api_key

    def get_custom_headers(self) -> dict:
        headers = {'X-Api-Key': self.api_key}
        return headers
    
    def get_custom_payload(self, movie_ids: list[int]) -> dict:
        """Prepare payload for deleting queue and redownloading movies."""
        return {
            'ids': movie_ids
            }


class SonarrInstance:
    """Base class for Sonarr instances with common functionality."""
    
    def __init__(self, name, url, api_key):
        self.name = name
        self.url = url
        self.api_key = api_key

    def get_custom_headers(self) -> dict:
        headers = {'X-Api-Key': self.api_key}
        return headers
    
    def get_custom_payload(self, movie_ids: list[int]) -> dict:
        """Prepare payload for deleting queue and redownloading movies."""
        return {
            'movieIds': movie_ids
            }


class RDTClient:
    """Client for interacting with RDTClient API."""
    
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
        if response.status_code != 200:
            raise Exception("Failed to authenticate with RDTClient")
        print("✅ Successfully authenticated with RDTClient")


class ArrRedownloader:
    """Class to handle redownloading movies in Radarr instances."""
    
    CHECK_INTERVAL = 10      # seconds between speed checks
    RETRY_DELAY    = 60      # seconds below threshold before redownload
    
    def __init__(self, rdtclient: RDTClient, radarr_instances: list[RadarrInstance], sonarr_instances: list[SonarrInstance], bandwidth: int = 175):
        self.rdtclient = rdtclient
        self.radarr_instances = radarr_instances
        self.sonarr_instances = sonarr_instances
        self.bandwidth = bandwidth
        #   { torrent_hash: { 'first_below': timestamp, 'handled': bool } }
        self._torrent_state = {}

        # mapping torrent_hash → [(instance_type, instance_obj, [media_ids]), ...]
        self.mapping = defaultdict(list)

        # start background monitoring
        self._stop_event = threading.Event()
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def register_mapping(self, torrent_hash: str, instance_type: str, instance_obj, media_ids: list[int]):
        """Call this when you add a torrent so we know which Arr instance & IDs to trigger."""
        self.mapping[torrent_hash].append((instance_type, instance_obj, media_ids))
    
    def _monitor_loop(self):
        """Runs in the background: check speeds, trigger retries, clean up."""
        while not self._stop_event.is_set():
            self._check_speeds_and_retry()
            self._cleanup_finished()
            time.sleep(self.CHECK_INTERVAL)
    
    def del_queue_bulk_redownload_radarr(self, movie_ids: list[int]) -> bool:
        """Delete the queue for multiple movies and trigger redownload."""
        for instance in self.radarr_instances:
            if not instance.api_key:
                print(f"❌ No API key found for {instance.name}")
                continue
            
            headers = instance.get_custom_headers()
            try:
                # Delete the queue for the specified movie IDs
                response = requests.delete(
                    f"{instance.url}/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false&changeCategory=false",
                    headers=headers,
                    json=instance.get_custom_payload(movie_ids)
                )
                if response.status_code == 200:
                    print(f"✅ Successfully deleted queue for {instance.name}")
                else:
                    print(f"❌ Failed to delete queue for {instance.name}: {response.status_code}")
                    print(f"Response: {response.text}")
                    return False
            except requests.exceptions.RequestException as e:
                print(f"❌ Connection error for {instance.name}: {e}")
                return False
            except Exception as e:
                print(f"❌ Unexpected error for {instance.name}: {e}")
                return False
        return True
    
    def del_queue_bulk_redownload_sonarr(self, movie_ids: list[int]) -> bool:
        """Delete the queue for multiple movies in Sonarr and trigger redownload."""
        for instance in self.sonarr_instances:
            if not instance.api_key:
                print(f"❌ No API key found for {instance.name}")
                continue
            
            headers = instance.get_custom_headers()
            try:
                # Delete the queue for the specified movie IDs
                response = requests.delete(
                    f"{instance.url}/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false&changeCategory=false",
                    headers=headers,
                    json=instance.get_custom_payload(movie_ids)
                )
                if response.status_code == 200:
                    print(f"✅ Successfully deleted queue for {instance.name}")
                else:
                    print(f"❌ Failed to delete queue for {instance.name}: {response.status_code}")
                    print(f"Response: {response.text}")
                    return False
            except requests.exceptions.RequestException as e:
                print(f"❌ Connection error for {instance.name}: {e}")
                return False
            except Exception as e:
                print(f"❌ Unexpected error for {instance.name}: {e}")
                return False
        return True
    
    def _trigger_redownload(self, torrent_hash: str):
        """Call the appropriate API for each registered mapping on this torrent."""
        for instance_type, inst, media_ids in self.mapping.get(torrent_hash, []):
            if instance_type == 'radarr':
                ok = self.del_queue_bulk_redownload_radarr(media_ids)
            else:
                ok = self.del_queue_bulk_redownload_sonarr(media_ids)
            print(f"🔄 Triggered redownload on {instance_type} {inst.name} for {media_ids}: {'OK' if ok else 'FAIL'}")
    
    def check_download_speeds(self, movie_ids: list[int]) -> list[tuple]:
        """Check the download speed of a movie using RdtClient."""
        response = self.rdtclient.session.get(f'http://{self.rdtclient.url}/api/v2/torrents/info?filter=downloading')
        response.raise_for_status()
        resp_json = response.json()
        dl_speed = 0
        torrents_dl_speed = [tuple()]
        for torrent in resp_json:
            if torrent['hash'] in movie_ids:
                dl_speed = torrent['downloadSpeed']
                torrents_dl_speed.append((torrent['hash'], dl_speed))
        if not torrents_dl_speed:
            print("❌ No torrents found for the specified movie IDs")
            return []
        return torrents_dl_speed
    
    def _check_speeds_and_retry(self):
        # 1) Fetch all downloading torrents
        resp = self.rdtclient.session.get(f'http://{self.rdtclient.url}/api/v2/torrents/info?filter=downloading')
        resp.raise_for_status()
        torrents = { t['hash']: t for t in resp.json() }

        # 2) compute dynamic threshold
        num = len(torrents)
        threshold = (self.bandwidth / num / 1024) / 3 if num else 0  # KiB/s

        now = time.time()
        for h, data in torrents.items():
            speed_kib = data['downloadSpeed'] / 1024

            state = self._torrent_state.setdefault(h, {'first_below': None, 'handled': False})
            if speed_kib < threshold:
                # below threshold
                if state['first_below'] is None:
                    state['first_below'] = now
                # if waited long enough and not yet handled
                elif not state['handled'] and (now - state['first_below'] > self.RETRY_DELAY):
                    self._trigger_redownload(h)
                    state['handled'] = True
            else:
                # back above threshold → reset
                state['first_below'], state['handled'] = None, False

    def _cleanup_finished(self):
        """Remove any hashes no longer present in rdtclient from our state & mapping."""
        resp = self.rdtclient.session.get(f'http://{self.rdtclient.url}/api/v2/torrents/info')
        resp.raise_for_status()
        active = { t['hash'] for t in resp.json() }

        # remove any that disappeared
        for h in list(self._torrent_state):
            if h not in active:
                del self._torrent_state[h]
                self.mapping.pop(h, None)
    
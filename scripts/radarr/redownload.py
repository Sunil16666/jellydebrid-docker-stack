import os
import requests

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
    
    def __init__(self, rdtclient: RDTClient, radarr_instances: list[RadarrInstance], sonarr_instances: list[SonarrInstance], bandwidth: int = 175):
        self.rdtclient = rdtclient
        self.radarr_instances = radarr_instances
        self.sonarr_instances = sonarr_instances
        self.bandwidth = bandwidth
        self.torrents = {} # Currently using torrent_hash as key to track torrents, may need to change to map correctly to sonarr and radarr instances
        
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
    
    def get_download_num(self) -> int:
        """Get the current number of downloads in progress."""
        response = self.rdtclient.session.get(f'http://{self.rdtclient.url}/api/v2/torrents/info?filter=downloading')
        response.raise_for_status()
        resp_json = response.json()
        return len(resp_json)
    
    def get_max_speed_dynamic(self) -> float:
        """Get the current download ratio."""
        download_num = self.get_download_num()
        return self.bandwidth / download_num if download_num > 0 else 0.0
    
    def is_speed_lower_threshold(self, movie_ids: list[int]):
        """Check if the download speed is below the threshold."""
        threshold = self.get_max_speed_dynamic() / 3 # Dynamic threshold based on current download speed, can be adjusted and must be tested
        torrents_dl_speed = self.check_download_speeds(movie_ids)
        is_below_threshold = False
        for torrent_id, speed in torrents_dl_speed:
            if speed < threshold:
                print(f"⚠️  Download speed for torrent {torrent_id} is below the threshold: {speed} < {threshold}")
                self.torrents[torrent_id] = is_below_threshold
        print("✅ All torrents are above the download speed threshold")

    def update_torrents(self):
        """Get the list of torrents in the RDTClient."""
        response = self.rdtclient.session.get(f'http://{self.rdtclient.url}/api/v2/torrents/info?')
        response.raise_for_status()
        resp_json = response.json()
        torrents = []
        for torrent in resp_json:
            torrents.append(torrent['hash'])
        if not torrents:
            print("❌ No torrents found in RDTClient")
            return
        for trnt in self.torrents:
            if trnt not in torrents:
                rem_item = self.torrents.pop(trnt)
                print(f"❌ Torrent {trnt} not found in RDTClient, removing{rem_item} from list")
        print(f"✅ Found {len(self.torrents)} torrents in RDTClient")
        if not self.torrents:
            print("❌ No torrents found in RDTClient after filtering")
            return
        print("✅ Successfully updated torrents in RDTClient")
    
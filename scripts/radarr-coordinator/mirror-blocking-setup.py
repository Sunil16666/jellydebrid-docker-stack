#!/usr/bin/env python3
import requests
import json
import os

def mirror_blocking_setup():
    """Mirror the blocking setup from 1080p-en to other Radarr instances"""
    
    # API keys from environment
    source_instance = {
        'name': 'radarr-1080p-en',
        'url': 'http://radarr-1080p-en:7878',
        'api_key': os.getenv('RADARR_EN_1080P_API_KEY', '')
    }
    
    target_instances = {
        'radarr-1080p-de': {
            'url': 'http://radarr-1080p-de:7878',
            'api_key': os.getenv('RADARR_DE_1080P_API_KEY', '')
        },
        'radarr-2160p-en': {
            'url': 'http://radarr-2160p-en:7878',
            'api_key': os.getenv('RADARR_EN_2160P_API_KEY', '')
        },
        'radarr-2160p-de': {
            'url': 'http://radarr-2160p-de:7878',
            'api_key': os.getenv('RADARR_DE_2160P_API_KEY', '')
        }
    }
    
    print("🔄 Mirroring blocking setup from radarr-1080p-en to other instances...")
    print("=" * 60)
    
    # Get the source configuration
    headers = {'X-Api-Key': source_instance['api_key']}
    
    print(f"📡 Getting configuration from {source_instance['name']}...")
    
    try:
        # Get custom formats from source
        response = requests.get(f"{source_instance['url']}/api/v3/customformat", headers=headers)
        source_formats = response.json()
        blocked_format = None
        
        for fmt in source_formats:
            if fmt['name'] == 'BLOCKED':
                blocked_format = fmt
                break
                
        if not blocked_format:
            print("❌ BLOCKED custom format not found in source instance")
            return
            
        print(f"✅ Found BLOCKED custom format (ID: {blocked_format['id']})")
        
        # Get quality profile with BLOCKED format
        response = requests.get(f"{source_instance['url']}/api/v3/qualityprofile", headers=headers)
        profiles = response.json()
        
        blocked_profile = None
        for profile in profiles:
            if profile['name'] == 'BLOCKED':
                blocked_profile = profile
                break
                
        if not blocked_profile:
            print("❌ BLOCKED quality profile not found in source instance")
            return
            
        print(f"✅ Found BLOCKED quality profile (ID: {blocked_profile['id']})")
        
    except Exception as e:
        print(f"❌ Error getting source configuration: {e}")
        return
        
    # Apply to target instances
    for instance_name, config in target_instances.items():
        print(f"\n📡 Configuring {instance_name}...")
        
        if not config['api_key']:
            print(f"❌ No API key for {instance_name}")
            continue
            
        headers = {'X-Api-Key': config['api_key']}
        
        try:
            # 1. Create BLOCKED custom format
            print("🔧 Creating BLOCKED custom format...")
            
            # Remove ID from format for creation
            format_to_create = blocked_format.copy()
            if 'id' in format_to_create:
                del format_to_create['id']
                
            response = requests.post(f"{config['url']}/api/v3/customformat", 
                                   json=format_to_create, headers=headers)
            
            if response.status_code == 201:
                created_format = response.json()
                print(f"✅ Created BLOCKED custom format (ID: {created_format['id']})")
                new_format_id = created_format['id']
            elif response.status_code == 400:
                print("⚠️  BLOCKED custom format already exists")
                # Get existing format ID
                existing_response = requests.get(f"{config['url']}/api/v3/customformat", headers=headers)
                existing_formats = existing_response.json()
                for fmt in existing_formats:
                    if fmt['name'] == 'BLOCKED':
                        new_format_id = fmt['id']
                        break
            else:
                print(f"❌ Failed to create custom format: {response.status_code}")
                continue
                
            # 2. Create BLOCKED quality profile
            print("🔧 Creating BLOCKED quality profile...")
            
            # Update format IDs in profile
            profile_to_create = blocked_profile.copy()
            if 'id' in profile_to_create:
                del profile_to_create['id']
                
            # Update format items to use new format ID
            for format_item in profile_to_create['formatItems']:
                if format_item['format'] == blocked_format['id']:
                    format_item['format'] = new_format_id
                    
            response = requests.post(f"{config['url']}/api/v3/qualityprofile", 
                                   json=profile_to_create, headers=headers)
            
            if response.status_code == 201:
                created_profile = response.json()
                print(f"✅ Created BLOCKED quality profile (ID: {created_profile['id']})")
            elif response.status_code == 400:
                print("⚠️  BLOCKED quality profile already exists")
            else:
                print(f"❌ Failed to create quality profile: {response.status_code}")
                
        except Exception as e:
            print(f"❌ Error configuring {instance_name}: {e}")
            
    print("\n" + "=" * 60)
    print("🎯 Blocking setup mirroring completed!")
    print("💡 All instances now have the same BLOCKED custom format and profile setup.")

if __name__ == "__main__":
    mirror_blocking_setup()

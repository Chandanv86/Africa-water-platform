#!/usr/bin/env python3
"""Test agriculture API endpoint."""

import json
import sys
import time
import requests

BASE_URL = "http://127.0.0.1:8000"

# Sample AOI from Ghana/Togo region (approximately where user provided centroid)
SAMPLE_AOI = {
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [2.204, 6.350],
                [2.257, 6.350],
                [2.257, 6.400],
                [2.204, 6.400],
                [2.204, 6.350],
            ]
        ],
    },
    "label": "Test AOI - West Africa",
}

def test_agriculture_api():
    """Test the agriculture analysis endpoint."""
    url = f"{BASE_URL}/aoi/agriculture/analyze"
    params = {
        "year_start": 2022,
        "year_end": 2025,
    }
    
    print(f"\n=== Testing Agriculture API ===")
    print(f"URL: {url}")
    print(f"Params: {params}")
    print(f"Payload: {json.dumps(SAMPLE_AOI, indent=2)}")
    
    try:
        print("\n[*] Sending request...")
        response = requests.post(url, json=SAMPLE_AOI, params=params, timeout=120)
        
        print(f"[*] Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"\n[✓] SUCCESS")
            print(f"[*] Overall Status: {data.get('status')}")
            print(f"[*] Years: {data.get('year_range')}")
            print(f"[*] Summary: {json.dumps(data.get('summary'), indent=2)}")
            print(f"\n[*] Cropland Extent Results:")
            for extent in data.get('cropland_extent', []):
                print(f"  {extent['year']}: {extent['value']} {extent['unit']} (status={extent['status']}, coverage={extent['coverage']})")
            
            print(f"\n[*] Phenology Results:")
            for pheno in data.get('phenology', []):
                print(f"  {pheno['year']}: valid_months={pheno['valid_month_count']}, status={pheno['status']}")
            
            print(f"\n[*] Food Security Results:")
            for food in data.get('food_security', []):
                print(f"  {food['year']}: phase={food['phase']} ({food['phase_label']}), status={food['status']}")
                
            print(f"\n[*] Full Response saved to: test_agriculture_response.json")
            with open("test_agriculture_response.json", "w") as f:
                json.dump(data, f, indent=2)
        else:
            print(f"\n[✗] FAILED with status {response.status_code}")
            print(f"[*] Response: {response.text}")
            
    except Exception as e:
        print(f"\n[✗] Exception: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_agriculture_api()

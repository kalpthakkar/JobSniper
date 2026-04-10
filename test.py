import requests

url = "https://api.lever.co/v0/postings/distro"
params = {"mode": "json", "limit": 50}

resp = requests.get(url, params=params)
print(resp.status_code)
print(resp.text[:200])
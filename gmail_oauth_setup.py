import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

client_id = os.environ.get("GMAIL_CLIENT_ID") or input("Client ID: ").strip()
client_secret = os.environ.get("GMAIL_CLIENT_SECRET") or input("Client Secret: ").strip()
config = {"installed": {
    "client_id": client_id,
    "client_secret": client_secret,
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"],
}}
flow = InstalledAppFlow.from_client_config(config, SCOPES)
credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("\\nCADASTRE NO RAILWAY (não publique no GitHub):")
print(f"GMAIL_CLIENT_ID={client_id}")
print(f"GMAIL_CLIENT_SECRET={client_secret}")
print(f"GMAIL_REFRESH_TOKEN={credentials.refresh_token}")

import json
from pathlib import Path
import sqlite3

import requests
from app.config import settings

BASE_URL = settings.comfyui_base_url.rstrip("/")
API_KEY = settings.comfyui_api_key.strip()
WORKFLOW_PATH = Path("workflows/ltx25_i2v_api.json")
DB_PATH = Path("data/studio.db")

if not API_KEY:
    raise RuntimeError("COMFYUI_API_KEY is missing from .env")

if not WORKFLOW_PATH.is_file():
    raise FileNotFoundError(f"Workflow not found: {WORKFLOW_PATH.resolve()}")

workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))

response = requests.post(
    f"{BASE_URL}/workflows/register",
    headers={"X-API-Key": API_KEY},
    json={
        "name": "ltx23_image_audio_video",
        "workflow": workflow,
    },
    timeout=120,
)

print("HTTP STATUS:", response.status_code)
print("SERVER RESPONSE:")
print(response.text)

response.raise_for_status()

# Parse the new workflow_id from the server response
res_data = response.json()
new_workflow_id = res_data.get("workflow_id")

if new_workflow_id:
    # Ensure database table exists or create a simple settings/config table to store it
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Create a lightweight settings/metadata table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    # Store or update the active workflow ID
    cursor.execute("""
        INSERT INTO app_settings (key, value) VALUES ('active_workflow_id', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (new_workflow_id,))
    
    conn.commit()
    conn.close()
    print(f"Successfully saved active workflow_id to database: {new_workflow_id}")
else:
    print("Warning: Response did not contain a workflow_id.")
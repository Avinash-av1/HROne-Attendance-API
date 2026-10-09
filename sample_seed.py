
"""Load the sample employees and attendance records into MongoDB."""

import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

sample_dir = pathlib.Path(__file__).parent / "sample_data"

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017")
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

for name in ("employees", "attendance_logs"):
    file_path = sample_dir / f"{name}.json"
    documents = json_util.loads(file_path.read_text())

    db[name].delete_many({})
    db[name].insert_many(documents)

    print(f"Loaded {len(documents)} records into {db.name}.{name}")

client.close()

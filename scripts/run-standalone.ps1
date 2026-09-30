$ErrorActionPreference = "Stop"
python scripts/verify_models.py
docker compose -f docker-compose.standalone.yml up --build

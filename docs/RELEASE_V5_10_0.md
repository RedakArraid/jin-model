# JIN 5.10.0 header reliability — portable CPU release

The portable ZIP is built with:

```powershell
python scripts/build_release.py
```

It contains the versioned application sources, local core engine, runtime
overrides, web UI and the three ready CPU model artifacts. After extraction:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

The UI is available at `http://localhost:8080` and health at
`http://localhost:8080/api/health`.

The package deliberately excludes customer PDFs, campaign results, feedback,
local secrets, caches and the large BAN index. Address structure and cleanup
work without BAN; official local address verification can be restored with:

```powershell
python scripts/build_ban_index.py --all --output data/reference/ban
```

Every ZIP includes `RELEASE-MANIFEST.json` with the version, Git commit and
SHA-256 of every packaged file. An adjacent `.sha256` file authenticates the
archive itself.

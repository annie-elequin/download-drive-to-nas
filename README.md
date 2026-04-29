# Drive folder to ZIP (NAS / Portainer)

Small web app: paste a **public** Google Drive folder link, choose a **destination folder path** (prefilled from config), and the app downloads the folder with [gdown](https://github.com/wkentaro/gdown), zips it, and writes the archive there. Writes are always restricted to the directory tree rooted at **`DATA_DIR`** so you control the boundary (NAS mount in Docker, or your Downloads folder when testing on your laptop).

## Requirements

- The Drive item must be a **folder** shared as **Anyone with the link can view** (or equivalent public/link access). Private folders are not supported.
- Large folders may hit Google or network limits; use for reasonably sized backups.

## Configuration (`.env`)

Put settings in a **`.env` file in the project root** (same folder as `docker-compose.yml`). Copy the template once:

```bash
cp .env.example .env
```

Then edit `.env`. The app loads it automatically on startup ([python-dotenv](https://pypi.org/project/python-dotenv/) in [`app/main.py`](app/main.py)). The file is listed in `.gitignore` so it is not committed.

**Docker Compose** also reads `.env` from the project root for `${VARIABLE}` substitution in `docker-compose.yml`, so the same file drives local containers.

| Variable | Description |
|----------|-------------|
| `APP_PASSWORD` | Single shared password for the web UI. Use a long random value (e.g. from a password manager). |
| `SESSION_SECRET` | Secret used to sign session cookies. Generate a random string for production. |
| `DATA_DIR` | Root directory for all output paths (default `/data` in Docker). Every destination you enter in the UI must resolve to a path inside this tree. |
| `DEFAULT_OUTPUT_PATH` | Optional. Prefills the destination field in the UI. If unset, the default is `<DATA_DIR>/exports`. Must lie under `DATA_DIR`. |

## Portainer stack

1. Create a new stack and paste `docker-compose.yml` (or point to this repo).
2. Set **environment** in the stack UI: `APP_PASSWORD`, `SESSION_SECRET` (do not use defaults in production).
3. Set **volumes**: map your Synology folder to `/data`, for example:

   `/volume1/Backups/drive-pull:/data`

4. Publish a host port to container `8000` if you do not use the default `8080:8000`.

## Local development (Docker)

```bash
cp .env.example .env
# edit .env — at minimum set APP_PASSWORD and SESSION_SECRET
mkdir -p data
docker compose up --build
```

Open `http://localhost:8080`. With the default `DATA_DIR=/data`, the form defaults to `/data/exports` (inside the compose volume `./data`). Zips land under `./data/exports/` on your machine.

## Local development (no Docker — writes to Downloads)

In `.env`, set **`DATA_DIR`** to a folder on your Mac (the app only allows destinations inside that tree). Optionally set **`DEFAULT_OUTPUT_PATH`** to prefill the form.

```bash
cd /path/to/download-drive-to-nas
cp .env.example .env
# edit .env — set APP_PASSWORD, SESSION_SECRET, and point DATA_DIR at a local folder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p "$HOME/Downloads/drive-to-nas-output"   # same path as DATA_DIR in .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`. Archives are written only under `DATA_DIR`.

To use **Docker** but still write to **Downloads**, bind-mount that folder as `/data` in compose (left side: host path, right side: `/data`) and keep `DATA_DIR=/data`.

If `gdown` fails with a message about the public link or permissions, open the folder in Drive and confirm **General access** is set to **Anyone with the link** (Viewer). Google may also throttle repeated downloads.

## Reverse proxy

Place nginx or Cloudflare in front of the service; TLS termination is typically at the proxy. The app uses cookie-based sessions (`SameSite=lax`); if the UI is on a different origin than the API, adjust cookie settings as needed.

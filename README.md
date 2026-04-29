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

**Docker Compose** reads a **`.env`** file in the same directory as `docker-compose.yml` for **`${VARIABLE}` substitution** in the compose file (including the **host** side of bind mounts). That is separate from the app reading `.env` at runtime (see [`app/main.py`](app/main.py)).

| Variable | Description |
|----------|-------------|
| `HOST_DATA_PATH` | **Host** directory bind-mounted to `/data` in the container. Example: `/volume1/Backups/drive-to-nas`. Default when unset: `./data` (repo folder). |
| `APP_PASSWORD` | Single shared password for the web UI. Use a long random value (e.g. from a password manager). |
| `SESSION_SECRET` | Secret used to sign session cookies. Generate a random string for production. |
| `DATA_DIR` | Path **inside the container** for allowed output paths (default `/data`). You normally leave this as `/data` so it matches the mount. |
| `DEFAULT_OUTPUT_PATH` | Optional. Prefills the destination field in the UI. If unset, the default is `<DATA_DIR>/exports`. Must lie under `DATA_DIR`. |

### Portainer stacks (`.env`, `stack.env`, and interpolation)

Portainer does **not** automatically read a random filename for **compose interpolation** (the `${...}` parts in `docker-compose.yml`, especially **`HOST_DATA_PATH`** on the volume line). You typically do one of the following:

1. **Web editor (most common)** — When you create or update the stack, use Portainer’s **Environment variables** section and add each line, for example:
   - `HOST_DATA_PATH=/volume1/Backups/drive-to-nas`
   - `APP_PASSWORD=…`
   - `SESSION_SECRET=…`  
   Portainer passes these into Compose so `${HOST_DATA_PATH}` resolves when the stack is deployed.

2. **`.env` next to the compose file** — If you deploy from a Git repo or folder on disk where a **`.env`** file lives beside `docker-compose.yml`, Compose (and Portainer’s compose path) can use it for substitution the same way as on your laptop.

3. **`stack.env` on the server** — Copy [`stack.env.example`](stack.env.example) to **`stack.env`** in the same directory as `docker-compose.yml` on the NAS. The compose file includes an optional `env_file: stack.env` (**`required: false`**) so missing files do not break the deploy; when **`stack.env` exists**, its variables are also passed **into the container**.  
   **Note:** In many setups, **volume `${HOST_DATA_PATH}` still has to be available at parse time**, so you still set `HOST_DATA_PATH` in Portainer’s environment UI or in `.env` unless your workflow exports it before `docker compose` runs. Treat `stack.env` as a convenient place to hold secrets on disk; keep **`HOST_DATA_PATH`** where your Portainer/Compose version expects it for interpolation.

Use [`stack.env.example`](stack.env.example) as a checklist of keys to paste or copy.

You do **not** need to change the in-container path **`/data`** unless you have a special layout; only **`HOST_DATA_PATH`** (the Synology/host folder) needs to match where you want files on disk.

## Portainer stack (quick steps)

1. **Add stack** in Portainer and paste `docker-compose.yml`.
2. Under **Environment variables**, paste values from `stack.env.example` (set **`HOST_DATA_PATH`** to your real share path, and strong secrets).
3. **Deploy**. The app writes under `/data/...` in the container, which is your **`HOST_DATA_PATH`** folder on the host.
4. Adjust the published port if you change `8080:8000` in the compose file.

## Local development (Docker)

```bash
cp .env.example .env
# edit .env — set APP_PASSWORD, SESSION_SECRET, and optionally HOST_DATA_PATH (default ./data)
mkdir -p "${HOST_DATA_PATH:-./data}"
docker compose up --build
```

Open `http://localhost:8080`. With defaults, **`HOST_DATA_PATH=./data`** maps to `/data` in the container; the form defaults to `/data/exports`. Zips land under `./data/exports/` on your machine (or under whatever folder you set as `HOST_DATA_PATH`).

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

# Railway Deployment

## Start
The app is configured for Railway with:
- `Procfile`
- `railway.toml`
- `requirements.txt` including `gunicorn` and `psutil`

## Environment variables
- `SECRET_KEY` (recommended)
- `PORT` (Railway sets this automatically)
- `DATA_DIR` (optional persistent storage mount)

## Notes
- The app binds to `0.0.0.0`
- Files and users are stored relative to `DATA_DIR` when provided

- `settings.json` stores the default max projects per user (editable from admin panel)
- The admin panel includes a Project Limit control and template presets

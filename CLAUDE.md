# Notes for Claude

- When a change touches hosting (`worker/`, `tastr/cloud/modal_web.py`,
  `.github/workflows/deploy.yml`, `.github/workflows/deploy-worker.yml`, the settings in `tastr/web/hosted.py`), update `docs/hosting-map.md` in the
  same change, diagrams included. `tests/test_hosting_map.py` catches missing names, not a stale diagram.

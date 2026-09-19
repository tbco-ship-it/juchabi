# 주차비 — 전국 공영주차장 요금·감면 조회

Static site. Source: 공공데이터포털 전국주차장정보표준데이터 (data.go.kr/data/15012896), pulled through the portal's own `/download/standard.json` pager (see WORK_LOGS in the workspace).

```
../martday/.venv/bin/python scripts/normalize.py        # data/raw/parking_YYYYMMDD.json -> data/lots.json
../martday/.venv/bin/python scripts/build.py --base / --origin https://<domain> --cname <domain>
```
Deploys from GitHub Actions on push to main. Flip `ORIGIN`/`BASE`/`CNAME` in `.github/workflows/deploy.yml` when the domain lands.

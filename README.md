# CodingContest MCP

Multi-account Streamable HTTP MCP server for [codingcontest.org](https://codingcontest.org), covering challenges, competitions, files, submissions, and results.

![CodingContest dashboard](docs/codingcontest-dashboard.png)

## Quick start

```sh
cp .env.example .env
docker compose up --build -d
```

Or with Python 3.12+: `pip install -r requirements.txt && python server.py`.

## Connect and use

Connect to `http://localhost:8000/mcp` with `X-CCC-Session: <CCC SESSION cookie>` (omit `SESSION=`). Public deployments require HTTPS and `MCP_PUBLIC_ORIGIN`.

Run solutions locally in any language. From this repository with dependencies installed, transfer large files without putting their contents in chat:

```sh
python -m ccc_mcp --url https://your-domain/mcp download ARTIFACT_ID level.zip
python -m ccc_mcp --url https://your-domain/mcp upload answer.out
```

The local client prompts for your cookie or reads `CCC_SESSION`. Upload returns an `artifact_id` for `submit_solution`; it does not submit. Downloads never overwrite files. Default file limit: 64 MiB (`--max-bytes` locally, `CCC_MAX_FILE_BYTES` on the server).

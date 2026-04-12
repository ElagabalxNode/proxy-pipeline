# pipelinectl

```bash
cd orchestrator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pipelinectl --help
```

Requires on PATH: `terraform`, `ansible-playbook`, `jq`, `curl`, `ssh`.

Set `PROXY_PIPELINE_ROOT` to the repository root if running from elsewhere.

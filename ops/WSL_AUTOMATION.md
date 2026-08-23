# WSL paper-summary automation

The scheduled paper job runs on a repository-scoped GitHub Actions runner in
the `Ubuntu-24.04` WSL distribution. The Windows host must remain powered on,
online, and able to expose the NVIDIA GPU to WSL.

The repository checkout is located at:

```text
G:\share\projects\arxiv-papers-daily
/mnt/g/share/projects/arxiv-papers-daily
```

## vLLM service

The service runs `Qwen3.5-9B-FP8-dynamic` as `PaperReader-Qwen3.5` with the following
equivalent command:

```bash
/home/zyf/softwares/miniforge3/envs/vllm/bin/vllm serve \
  /home/zyf/models/RedHatAI/Qwen3.5-9B-FP8-dynamic \
  --served-model-name PaperReader-Qwen3.5 \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.60 \
  --max-num-seqs 1 \
  --kv-cache-dtype fp8 \
  --reasoning-parser qwen3
```

Install the tracked service file from the current repository location, restart
the service, and verify the OpenAI-compatible endpoint:

```bash
cd /mnt/g/share/projects/arxiv-papers-daily
sudo install -m 0644 ops/vllm-paper.service /etc/systemd/system/vllm-paper.service
sudo systemctl daemon-reload
sudo systemctl enable vllm-paper.service
sudo systemctl restart vllm-paper.service
curl --fail --retry 30 --retry-connrefused --retry-delay 5 \
  http://127.0.0.1:8000/v1/models
```

The endpoint must expose exactly one model unless the workflow environment sets
`VLLM_MODEL`; for this configuration the model ID is `PaperReader-Qwen3.5`.
Model reasoning is parsed by vLLM and is never written to the published summary.

## Repository runner

In **GitHub repository → Settings → Actions → Runners**, add a Linux x64
self-hosted runner. Install it at `/home/zyf/softwares/actions-runner`, add the custom
labels `wsl2,gpu`, and then install it as a service:

```bash
cd /home/zyf/softwares/actions-runner
sudo ./svc.sh install zyf
sudo ./svc.sh start
sudo ./svc.sh status
```

Registration commands contain a short-lived token and must be copied from the
GitHub UI at setup time. Do not commit that token or the runner's `.credentials`
files.

## Start WSL with Windows

Create a Windows Task Scheduler task that runs at system startup or user logon:

```powershell
wsl.exe -d Ubuntu-24.04 --exec /bin/true
```

After each Windows boot, verify that both services started with WSL:

```bash
systemctl is-active vllm-paper.service
systemctl is-active actions.runner.zyf515730395-ArXiv-Papers-Daily.ZYF-WinSZ.service
```

After the runner and vLLM services are healthy, trigger **Run Arxiv Papers
Daily** manually once. The scheduled run remains daily at 01:00 UTC.

## Manual queue recovery

From the repository checkout inside WSL:

```bash
cd /mnt/g/share/projects/arxiv-papers-daily
SUMMARY_ENABLED=1 \
PAPER_NOTES_ROOT=/mnt/g/share/papers \
VLLM_BASE_URL=http://127.0.0.1:8000/v1 \
python daily_arxiv.py --summaries-only
```

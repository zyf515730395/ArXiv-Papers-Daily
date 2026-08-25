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

After the runner and vLLM services are healthy, the two independent workflows
can be triggered manually for validation:

- **Run Arxiv Papers Daily** fetches at 01:00 UTC (09:00 Asia/Shanghai).
- **Backfill Arxiv Paper Summaries** starts at 02:00 UTC (10:00
  Asia/Shanghai) and runs its summary phase for up to four hours.

Both workflows use the same `arxiv-paper-pipeline` concurrency group. If daily
ingestion is delayed, backfill waits rather than accessing the shared checkout,
summary state, or vLLM concurrently.

## Daily ingestion and latest-paper priority

Daily ingestion fetches arXiv metadata, appends the archive, and atomically
queues newly discovered IDs in `/mnt/g/share/papers/.summary-state.json`. It
sets `SUMMARY_DEFER_PROCESSING=1`, so it does not wait for vLLM or perform any
paper inference. The latest paper list and current Summary states are published
immediately after ingestion.

The separate backfill workflow processes queued non-historical papers first,
ordered by arXiv ID from newest to oldest. Consequently, papers added by the
09:00 run have priority over the historical archive. Only after that latest
queue is clear does it advance the historical schedule.

## Historical summary backfill

The independent backfill workflow covers every archived year. Historical
buckets are ordered by month from newest to oldest, by the topic order in
`config.yaml` within each month, and by paper date and arXiv ID from newest to
oldest within each topic. Existing queue entries use the same order after a
restart.

`SUMMARY_BACKFILL_LIMIT` controls the arXiv metadata batch size rather than the
number processed per run. The workflow leaves `SUMMARY_BACKFILL_YEAR` unset so
every archived year remains eligible, and sets
`SUMMARY_BACKFILL_TIME_BUDGET_MINUTES=240` so each historical phase starts new
papers for up to four hours. A paper already in progress at the deadline is
allowed to finish; the runner then publishes and commits the completed pages.
A paper shared by multiple topics is inferred once and its Markdown is copied
into each topic directory. Failed items are attempted only once in a run,
later ordered items continue, and failed items remain pending for the next run.

The backfill GitHub Actions job has a 360-minute timeout to leave room for the
four-hour summary phase, one in-flight paper to finish, and the final commit.
Summary state and Markdown are saved atomically after every paper, so the next
scheduled or manual run resumes the same all-years order.

The summary instruction is a fixed, versioned expert template. Only the paper's
configured topic list is inserted into the expert role; the title, abstract,
and Introduction are appended as untrusted source material. The template
requires standard English technical terms, abbreviations, method names,
datasets, metrics, losses, and components such as `token` and `Transformer` to
remain in English. Changing the template version marks earlier ready summaries
for ordered background refresh without hiding their currently published text.

## Python runtime and manual queue recovery

The self-hosted job runs directly in the shared G-drive checkout and keeps its
Python dependencies outside the repository at
`/home/zyf/.cache/arxiv-papers-daily/venv`. This avoids modifying the vLLM
Conda environment and does not create untracked files in the repository. The
synchronization step also sets repository-local `core.autocrlf=true` so Windows
and WSL agree on the shared checkout's CRLF files before the clean-tree check.

To prepare the same runtime and manually enqueue and process the next ordered
historical batch:

```bash
cd /mnt/g/share/projects/arxiv-papers-daily
VENV_PATH=/home/zyf/.cache/arxiv-papers-daily/venv
if [ ! -x "$VENV_PATH/bin/python" ]; then
  /home/zyf/softwares/miniforge3/envs/vllm/bin/python -m venv "$VENV_PATH"
fi
"$VENV_PATH/bin/python" -m pip install -r requirements.txt

SUMMARY_ENABLED=1 \
SUMMARY_BACKFILL_LIMIT=10 \
SUMMARY_BACKFILL_TIME_BUDGET_MINUTES=240 \
PAPER_NOTES_ROOT=/mnt/g/share/papers \
VLLM_BASE_URL=http://127.0.0.1:8000/v1 \
"$VENV_PATH/bin/python" daily_arxiv.py --summaries-only --backfill-history
```

Omit --backfill-history to retry pending items without advancing the historical
schedule.

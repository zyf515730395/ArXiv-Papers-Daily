# Cloud collection and local paper-summary automation

The repository uses two deliberately separate paths:

- GitHub-hosted Actions collects arXiv candidates at 09:00 Asia/Shanghai,
  updates the public paper table, and deploys GitHub Pages. It never connects
  to WSL, the G-drive notes state, or vLLM.
- Codex local automations curate candidates and trigger the WSL summary
  runtime. Summary Markdown and queue state remain under
  `/mnt/g/share/papers`.

The shared checkout is located at:

```text
G:\share\projects\arxiv-papers-daily
/mnt/g/share/projects/arxiv-papers-daily
```

When Windows uses a localhost proxy, WSL must use mirrored networking so the
proxy is reachable from background services. The host configuration is:

```ini
[wsl2]
networkingMode=mirrored
dnsTunneling=true
firewall=true
autoProxy=true
```

The weekend unit explicitly sets the host proxy at `127.0.0.1:7890` and keeps
`127.0.0.1,localhost` in `NO_PROXY`, so arXiv downloads use the proxy while
requests to the local vLLM endpoint never do.

## Candidate collection and curation

The tracked `data/arxiv-candidates.json` ledger keeps every cloud-discovered
candidate as `pending`, `accepted`, or `rejected`. The 09:00 workflow adds new
pending papers to the public archive immediately, generates `docs/index.html`,
and pushes the result. Until local curation runs, Summary displays `待生成`.

At 21:30 Monday through Friday, the local Codex automation reviews every
pending candidate. It writes a temporary decision document and applies it with:

```bash
PAPER_NOTES_ROOT=/mnt/g/share/papers \
/home/zyf/.cache/arxiv-papers-daily/venv/bin/python daily_arxiv.py \
  --apply-curation /tmp/arxiv-curation-decisions.json
```

Accepted papers are assigned exactly one configured topic and enter the local
summary queue. Rejected papers are removed from the public archive and remain
recorded in the ledger so cloud collection cannot add them again. The temporary
decision document is deleted and never committed.

After the curation commit is clean and pushed, process only newly accepted or
previously failed non-historical papers with:

```bash
ops/summary_runtime.sh weekday
```

This command starts vLLM on demand, handles the newest paper date and arXiv ID
first, publishes completed summaries, and stops the model before returning.

## vLLM and weekend backfill services

The model service runs `Qwen3.5-9B-FP8-dynamic` as `PaperReader-Qwen3.5`:

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

Install the tracked services and the narrowly scoped sudoers rule:

```bash
cd /mnt/g/share/projects/arxiv-papers-daily
sudo visudo -cf ops/arxiv-vllm-runner.sudoers
sudo install -m 0644 ops/vllm-paper.service /etc/systemd/system/vllm-paper.service
sudo install -m 0644 ops/arxiv-weekend-backfill.service \
  /etc/systemd/system/arxiv-weekend-backfill.service
sudo install -m 0440 ops/arxiv-vllm-runner.sudoers \
  /etc/sudoers.d/arxiv-vllm-runner
sudo systemctl daemon-reload
sudo systemctl disable --now vllm-paper.service arxiv-weekend-backfill.service
```

Both services remain disabled at boot. `vllm-paper.service` has a 48-hour
safety limit to cover the weekend window; the runtime script normally stops it
earlier. `Restart=no`, process-group shutdown, a shell cleanup trap, and the
weekend service runtime limit ensure GPU memory is released after completion or
failure.

The sudoers file grants `zyf` only the exact start/stop commands for these two
units. It does not grant general passwordless sudo.

## Weekend schedule and checkpoints

The local weekend automation starts the durable unit at 09:30 Saturday:

```bash
sudo -n /usr/bin/systemctl start arxiv-weekend-backfill.service
systemctl status arxiv-weekend-backfill.service --no-pager
```

The service holds a local lock, processes historical papers continuously, and
stops by 23:30 Sunday. Historical order is month newest first, configured topic
order, then paper date and arXiv ID newest first. Any pending non-historical
paper still takes priority.

Every approximately 150 minutes the service finishes the in-flight paper,
pulls main with `git pull --ff-only`, renders the five aggregate summary pages,
commits and pushes the checkpoint, and continues without restarting vLLM.
Inference between checkpoints changes only G-drive Markdown and
`.summary-state.json`, so the Git checkout remains clean and can safely receive
the cloud candidate commit.

The PDF is downloaded into memory and parsed with PyMuPDF. No PDF, arXiv HTML,
extracted text, or cache directory is persisted. A paper shared by multiple
topics is inferred once and its Markdown is written to each selected topic
directory. Weekend history uses the title and date already stored in the
archive together with the canonical PDF URL, so it does not wait on the
`export.arxiv.org` metadata API. Its abstract field is empty when no prior local
state exists; the model then summarizes from the extracted Introduction.

## Manual commands and verification

```bash
# Model should normally be inactive outside a local summary run.
systemctl is-active vllm-paper.service
curl --fail http://127.0.0.1:8000/v1/models

# Start a weekday new-paper pass directly.
cd /mnt/g/share/projects/arxiv-papers-daily
ops/summary_runtime.sh weekday

# Start or stop the durable weekend pass.
sudo -n /usr/bin/systemctl start arxiv-weekend-backfill.service
sudo -n /usr/bin/systemctl stop arxiv-weekend-backfill.service
journalctl -u arxiv-weekend-backfill.service -f
```

The fixed versioned summary prompt inserts only the configured topic into its
expert role. It preserves standard English terms, abbreviations, method names,
datasets, metrics, losses, and components such as `token`, `Transformer`,
`NeRF`, and `Gaussian Splatting`.

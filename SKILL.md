---
name: autodl-autopilot
description: Use when a project's experiments run on an AutoDL GPU instance and it needs powering on or off (有卡 or 无卡), a job run on it, files moved, budget or spending checked, or kept from billing while idle (空转, 忘关机, 自动关机); when choosing or renting a machine on the GPU market (算力市场, 找机器, 空闲卡); when moving files to or from AutoDL file storage (文件存储, autodl-fs); also before experiments, training or data transfer there, including automated experiment pipelines (自动跑实验).
---

# AutoDL Autopilot

You manage the user's AutoDL instance with the `autopilot` command. Every command prints one JSON object; read it before the next step. Run it as `<this skill's directory>/bin/autopilot <command>` (Windows: `bin\autopilot.cmd`). Installed for Claude Code that is `~/.claude/skills/autodl-autopilot/bin/autopilot`.

## Rules

1. **Spending needs the user's permission, recorded.** Before the first power-on of an instance, ask which modes may be used and the monthly budget, then `autopilot grant ID --modes gpu|nogpu|both --budget <yuan|none> --said '<their words>'`. Within that, power on without asking again. Never grant on your own.
2. **Never handle the token.** The user stores it in their own terminal (`autopilot token set`, reading stdin). Do not ask for it, read its file or print it. Exit 14 means it is missing or expired: tell the user to take a new one (F12 in the logged-in console, `copy(localStorage.token)`, then `autopilot token set`).
3. **Send each power request once.** Exit 6 means a request got no answer: run `autopilot status ID` and decide from the state; never repeat blindly.
4. **Jobs through `autopilot run`.** It gives the job a name, a log and `--then-off`; the guard sees its resource use like any other process.
5. **Do not cut jobs.** `off` refuses while jobs run (exit 3). Use `--force` (or `off --console --force` when SSH is down) only when the user agrees at that moment.
6. **Renting needs the user's yes, every time.** `autopilot rent` without `--yes` only prints a plan; show it (machine, GPUs, price per hour, image, planned cost) and rent only after the user agrees to that plan, with `--yes --said '<their words>'`. A grant for the instance does not cover renting another one.
7. **Deleting stored files needs the user's yes.** `autopilot storage rm` deletes for good (no recycle bin); name what will go and delete only after the user agrees. `storage init` also only with their agreement (`--yes`).
8. **Nothing else in the console.** Autopilot cannot release, reset, clone, recharge or renew, and you do not do these things any other way.

## First use on a computer

1. `autopilot doctor`. Missing ssh/scp: install the OpenSSH client (ask first). Missing token: rule 2.
2. `autopilot keygen` makes `~/.ssh/id_ed25519_autodl` (an existing key: `autopilot config set key <path>`). Nothing to paste: when an instance refuses the key, Autopilot logs in once with the root password the console shows for it and adds the key to `/root/.ssh/authorized_keys`; any SSH command does this, `autopilot connect ID` does only this. The password is never printed; do not read or ask for it. If the user does not want that: `config set add_key false`, and they paste `public_key` into the console's 设置SSH免密登录, which applies to instances powered on after it.
3. Settings, if the defaults do not fit: `autopilot config set workdir /root/autodl-tmp/<project>`, `config set env 'source /root/miniconda3/etc/profile.d/conda.sh && conda activate base'` (non-interactive SSH loads no conda), `config set idle_min 15`.

Write what you settled into the project's instruction file (`CLAUDE.md`) under `## AutoDL`: the instance ID, the modes, the budget, the idle minutes, the workdir. Read it at the start of later conversations.

## Power on

`autopilot on ID --mode gpu|nogpu --hours <planned hours>` does, in order: grant and budget check, balance check, a console timer at now + 30 min as a fallback, the power-on, waiting until running, starting the guard over SSH, then cancelling the fallback (or keeping `--hard-limit 8h` as a console timer if the user wants a hard stop that cuts jobs).

- Exit 10: over budget. Exit 12: no grant for that mode. Tell the user; do not raise the budget yourself.
- Exit 3 with "no free GPU": the host's GPUs are taken. Wait and retry every few minutes (tell the user), or use no-GPU mode if the task allows, or show alternatives with `autopilot market --like ID` (below).
- Exit 1 after powering on ("the guard did not start"): the fallback timer still shuts it down. Check `autopilot doctor` and `autopilot connect ID`, then `autopilot arm ID`.
- Exit 2 with "refused the login": the key is not on the instance and could not be added (`add_key` off, or the console's root password was refused). Tell the user; `autopilot connect ID` prints the reason.
- Already running (the user started it): `autopilot arm ID` starts the guard; check the budget with `autopilot check ID --mode M --hours H` first.

Choose the mode: no-GPU (0.1 yuan/h) for moving data, installing pure-Python packages, editing; GPU for training and evaluation. Switching means `off`, then `on` in the other mode.

## Jobs and files

- `autopilot run ID NAME [--then-off] [--protect 1d] -- <command>` starts the command in the workdir with the env setup, in the background; output goes to its log. `--then-off` shuts the instance down 2 minutes after the job ends; `--protect` whitelists it (below).
- `autopilot jobs ID` shows the guard (`last.busy`, `last.shutdown_in`), every job (`running`, `rc`) and the whitelist. `autopilot tail ID NAME` shows a log's end; `tail ID guard` the guard's.
- `autopilot push ID <local> <remote dir>`, `autopilot pull ID <remote path> <local dir>`. Pull results before shutting down.

## The guard

- **Default logic.** Busy means GPU ≥ 5 %, CPU ≥ 0.2 cores or network ≥ 200 KB/s, a hold, or a whitelisted process. A process that merely exists, a running job included, does not keep the instance on: a hung or waiting process that uses nothing is idle, and after `idle_min` the instance shuts down.
- **Whitelist.** A process the user wants kept even while idle (a Jupyter server, a job that waits for hours): `autopilot protect ID NAME --cmd '<part of its command line>' --for 7d`, or `--pid N`, or `--job <run name>`. While the process lives and the entry has not expired, the instance is not shut down for idleness. `autopilot protected ID` lists the entries with their state (protecting, no_process, expired); `autopilot unprotect ID NAME` removes one. Entries stay on the data disk across power-ons until they expire (at most 90 days). Whitelist only what the user asked for, with the duration they gave, and say when it expires: it keeps the instance billing.
- **A short wait without a process:** `autopilot hold ID 2h` (0 clears).
- **On or off.** `autopilot config set guard_default true|false` is the default for every instance; `autopilot guard ID on|off` overrides it for one instance, `guard ID auto` follows the default again, `guard ID` shows the setting and where it comes from. Changing it on a running instance starts or stops the guard there at once. With the guard off, `on` removes the fallback timer once the instance runs: nothing shuts it down until `autopilot off` (or `--hard-limit`), so tell the user. Change these settings only when the user asks.

## Finding a machine (算力市场)

`autopilot market` searches the pay-as-you-go GPU market: machines with free GPUs now, cheapest first. It only reads.

- Filters: `--gpu 'RTX 4090'` (or a part like `4090`, repeatable), `--region 内蒙B区` (a name, a part like `西北`, or a sign like `nm-B1`), `--gpus 2` (free GPUs needed on one machine, default 1), `--max-price 2` (yuan per GPU per hour), `--min-cuda 12.4`, `--min-memory 48` (GB of GPU memory), `--sort price|free`, `--limit 20`. `--like ID`: the GPU model and region of that instance. `--all` adds machines without free GPUs.
- `autopilot market --list gpus` / `--list regions` gives the names the filters accept.
- The console lists the machines with free GPUs first, so the search stops after them (about 1 second; `read: early`). One freed in the last minutes can be missing: `--thorough` reads the whole market (about 15 seconds). When the free block holds nothing that fits, it reads on by itself.
- Present a few machines: model, region, free/total GPUs, price per GPU-hour, CUDA, data disk. Free GPUs change by the minute.
- Rent on the chosen machine: `autopilot rent MACHINE --gpus N --hours H --budget <yuan|none> [--image PyTorch|PyTorch:2.5|base-image-…|image-…] [--expand-disk GB]` prints the plan (rule 6); the same command with `--yes --said '<their words>'` rents. The default image is the newest PyTorch the machine's chip and driver run; `autopilot images [--framework X] [--private]` lists the choices; ask when the project needs a particular framework or version.
- Renting creates the instance and starts it at once (GPU mode, billed from that second). Then it goes like `on`: the fallback timer, waiting until running (the image can take minutes), the key, the guard, the timer cancelled. The new instance gets a grant with `--budget` and `--modes` (default both), recorded with the user's words. Write its ID into `CLAUDE.md`.
- **First rent: suggest no-GPU mode for the setup.** When showing the plan, tell the user: a new instance has an empty data disk, and downloading models, datasets and packages in GPU mode pays the GPU price while the GPU sits idle. Suggest: right after the rent, `autopilot off`, then `autopilot on ID --mode nogpu` (0.1 yuan/h) for the downloads and the environment, then `off` and `on --mode gpu` to train. Say the risk too: a powered-off instance keeps no GPU, so the host may have none free when switching back (then wait, or find another with `market --like ID`); on a host with few free GPUs, starting the work in GPU mode right away can be the better choice. Let the user decide.
- Exit 3 with `MachineGpuNumUpdateFailed`: the GPUs were taken meanwhile; search again. Exit 6: the rent got no answer and no new instance is listed: `autopilot status` in a minute, never rent again blindly.
- Autopilot cannot migrate or clone; moving data from an old instance is the user's choice (the console's 克隆实例, or `pull` then `push`).

## File storage (文件存储)

File storage belongs to a region: once initialized there, every instance of that region mounts it at `/root/autodl-fs` (an instance running at the time sees it after its next power-on). It is filled and emptied from this computer through the console, with every instance off, so no GPU or no-GPU hours are paid for moving data.

- `autopilot storage list`: the regions, `created` or `uninitialized`. `autopilot storage init REGION` shows what it means; `--yes` initializes (rule 7; the account must be bound to WeChat; beyond the free size it is billed by use).
- `autopilot storage ls REGION [PATH]`, `storage put REGION <local file or folder> [REMOTE_DIR]`, `storage get REGION <remote file or folder> [LOCAL_DIR]`, `storage rm REGION PATH [--recursive]` (rule 7). REGION is a name (or part, like 内蒙), a sign, or an instance ID (its region). `put` checks the free space first; a folder goes with everything in it.
- Prefer it for datasets and model weights: upload once while everything is off, then the instance reads `/root/autodl-fs/...` (copy hot data to the data disk `/root/autodl-tmp` if the job reads it many times). Results the user wants to keep can be written there and fetched with `storage get` after the shutdown.
- The storage itself cannot be deleted; emptying it stops what it costs beyond the free size.

## Power off and spending

- `autopilot off ID` shuts down over SSH, waits until the console shows it shut down, cancels a leftover timer and syncs the bills.
- `autopilot spend [ID]` gives this month's spending from the cached bills; `autopilot sync` refreshes them. `autopilot balance` reads the balance.
- Before keeping an instance on much longer than planned, `autopilot check ID --mode M --hours H`.
- Report briefly: after power-on (mode, price per hour, idle minutes), after power-off (duration, cost from the bills, this month's total).

## Facts

- Billing is per second from power-on to power-off, GPU used or not; each power-on costs at least 0.01 yuan. GPUs are not reserved while off: a GPU power-on can find none free.
- An instance shut down for 15 days is released and its data deleted; `status` shows `release_at`. Remind the user when fewer than 3 days are left.
- The console API Autopilot uses is the console's own, not a public one; if it changes, commands fail with clear errors: tell the user.

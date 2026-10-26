# Changelog

## Unreleased

- `remind ID`: reminders before the console's timed shutdown, which cuts running jobs (a `--hard-limit`, `timer --in`, the fallback timer). It waits for the next of 30, 20 and 10 minutes before the timer (`--at` to change), prints one JSON object (minutes left, the jobs still running over SSH, how to extend) and exits, so an AI waiting on it in the background is woken; marks already given for the same timer are not repeated by the next run, a moved timer counts again, and an instance that is off ends it. `--follow` keeps going (one JSON object per line) until the instance is off; `--notify-cmd` runs a command per reminder with the message in `$AUTOPILOT_REMINDER`; `--no-jobs` skips SSH. `on --hard-limit` and `timer --in` now print a `remind` hint
- `on` checks the mode against the host's free GPUs before sending anything. With fewer than `spare_gpus` (default 2) free beyond what the instance needs, no-GPU mode is stopped: the GPUs can be taken meanwhile, so power on in GPU mode and download there. With that many or more, GPU mode is stopped: download in no-GPU mode first. `--skip-mode-check` powers on all the same, `config set mode_check false` turns the check off. A host with no GPU free for the instance does not stop no-GPU mode. The rent plan's tip follows the same rule
- `market` and the `rent` plan show the price this account pays: a member (炼丹会员) the member price (95% of the list price, rounded to the fen as the console bills it), a normal user the list price. The level is read from `/api/v1/user/level` (read only, new in the request list); `prices` says which prices are shown and `list_price_yuan_h` gives the list price. `--max-price`, the sort, the rent budget and balance checks use it. When the level cannot be read the list price is used, which can only overestimate. Checked against a member account's bills
- `on` and `rent --yes` check the region's file storage first. When it is not initialized they send nothing and say to initialize it (`storage init`), since an instance mounts `/root/autodl-fs` only when it starts and storage initialized later means powering off and on again; the rent plan says the same. `--without-storage` goes on without it, `config set storage_check false` stops the check. A region without file storage, or a list that cannot be read, does not stop anything
- READMEs rewritten in plainer language; they now cover the data disk, the file storage and backing up before a long shutdown
- `disk`: the data disk of an instance. Shows size, use and what can be added or removed; `--add GB` / `--remove GB` / `--remove-all` give a plan first (size after, price per day, when it takes effect) and order with `--yes --said`. Adding to a running instance takes effect only after a restart (said in the plan); removing only while shut down and pay as you go. Shown and quoted on a real account; an order has not been run
- `off` prints `release_at` and a reminder to back up before a long shutdown (15 days off and the instance is released with its data)
- The guard's idle time follows the mode: 15 minutes in GPU mode (`idle_min`), 60 in no-GPU mode (`idle_min_nogpu`), since no-GPU mode is mostly for long downloads and costs 0.1 yuan an hour; `--idle` still sets it for one power-on
- `storage`: the file storage (文件存储) of a region, through the console with every instance off: `list` the regions, `init` one (with `--yes`), `ls`, `put` a file or folder (chunked like the web page, free space checked first), `get` a file or folder, `rm` (a folder only with `--recursive`, never the root). Download links are never printed. Checked on a real account (upload, listing, download byte for byte, deletion)

## 0.1.0 (2026-10-23)

First release.

- `autopilot` command line (Python 3.8+, standard library only; macOS, Linux, Windows)
  - console API: instance status, balance, bills, power on (GPU or no-GPU), power off, timed shutdown
  - `on`: grant and budget check, balance check, a 30-minute fallback console timer, power-on once, wait until running, start the guard over SSH, then cancel the fallback (or keep `--hard-limit`)
  - `off`: over SSH, refused while jobs run unless `--force`; `--console --force` when SSH is down
  - `run` / `jobs` / `tail` / `hold`, `push` / `pull`, `grant` / `check` / `sync` / `spend`, `token`, `keygen`, `doctor`
- SSH address read from the console, host name checked before every remote command, own known_hosts
- The key installs itself: when an instance refuses it, one login with the root password the console shows adds it to `/root/.ssh/authorized_keys`, then the command goes on with the key (`add_key`, on by default; `connect` does only this). The password goes to ssh through SSH_ASKPASS and the ssh process's environment, never a command line, file or output; checked on a real no-GPU instance
- Instance-side guard (`autopilot/guard.sh`, bash 3.2+): busy while GPU, CPU or network use is above its threshold, during a hold, or while a whitelisted process lives; shuts down after the idle time, or after a `--then-off` job ends. A process that merely exists does not keep the instance on
- Process whitelist with an expiry (`protect` / `unprotect` / `protected`, `run --protect`): by PID, job name or a piece of the command line, from 1 second to 90 days, kept on the data disk across power-ons
- Guard switch: `guard_default` in the settings, `guard ID on|off|auto` per instance, `--guard` / `--no-guard` for one power-on; a change on a running instance starts or stops the guard at once
- `market`: searches the pay-as-you-go GPU market (read only) by GPU model, region, free GPUs per machine, price per GPU-hour, CUDA version and GPU memory, cheapest or freest first; `--like ID` takes an instance's model and region; `--list gpus|regions`. The console lists the machines with free GPUs first, so the search stops after them (about 1 s instead of 16 s for the whole market); `--thorough` reads every page, and it reads on by itself when the free block has nothing that fits; checked against the real market
- `rent`: rents a pay-as-you-go instance on a market machine. Without `--yes` only a plan (machine, GPUs, price, image, planned cost, balance); with `--yes --said` one order, a grant for the new instance, the fallback timer, then like `on` (running, key, guard, timer cancelled). The image is the newest of a framework that the machine's chip and driver run, or a given base or private image; `images` lists them. A rent without an answer is found by machine and creation time, or ends uncertain (exit 6). The plan was checked against the real market; an actual rent has not been run yet
- Console token: environment variable, then a 0600 credentials file, then the OS credential store (5-second timeout, skipped on failure), so it works with the screen locked and over SSH
- Budget judged from the console's own bills, per calendar month in Beijing time
- Disclaimer (`DISCLAIMER.md`, `docs/DISCLAIMER.md`, a section in both READMEs) with the rights/takedown contact `ButterFuture@proton.me`
- Tests: 139, run on macOS (bash 3.2) and Linux (Python 3.8 and 3.12, bash 5.2)

### Not verified yet

- The whole flow on a real instance over SSH (power-on with the guard, whitelist, jobs, power-off): so far only the console API (status, balance, bills, no-GPU power on and off, timer) was run against a real account; the SSH side and the guard ran against stubs and in Linux containers
- GPU-mode power-on on a real account; the console's error codes for no free GPU and for an insufficient balance
- Windows: the launcher, the credential store and file locking have not been run on a Windows machine

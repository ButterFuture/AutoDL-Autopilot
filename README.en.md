<p align="center"><img src="assets/hero.svg" alt="AutoDL Autopilot: your AI powers AutoDL on and off" width="830"></p>

<p align="center">
A Claude Code skill that lets the AI power your AutoDL instances on and off, run jobs on them and keep to a budget.
</p>

<p align="center">
<a href="#start">Quick start</a> ·
<a href="#how">How it works</a> ·
<a href="#safety">Cost and safety</a> ·
<a href="#faq">FAQ</a> ·
<a href="README.md">中文</a>
</p>

> Unofficial project. It is not affiliated with, authorized by or endorsed by AutoDL or its operator. It calls the API the console's web page uses itself, and the costs and risks of powering instances on and off are the user's own. See the [Disclaimer](#disclaimer) and [DISCLAIMER.md](docs/DISCLAIMER.md).

<p align="center"><img src="assets/demo.svg" alt="One run: budget check, power-on, the job, shutdown, bookkeeping; 8 min 47 s, 0.14 yuan" width="830"></p>

Say "start the training" and the AI checks this month's budget, powers the instance on, starts the job, shuts the instance down when the job is done and records what it cost. When the job ends or the instance has nothing to do, it shuts itself down: after 15 idle minutes in GPU mode, or 60 in no-GPU mode, which is mostly used for downloads that take a while. This still happens if the conversation is gone.

Power, the shutdown timer, the balance and the bills go through the console's API, so there is no browser to open and each step takes seconds. It can also:

- search the GPU market by model, region, free GPUs, price and CUDA version, usually in about a second, and rent the machine you pick after you agree to the plan;
- upload, download and delete files on the file storage (`/root/autodl-fs`) while the instance is off, so moving data costs no instance hours;
- add data disk space or remove it, with a price quote first and a clear note on when the change takes effect.

<a id="start"></a>

## Quick start

You need Python 3.8+ and OpenSSH. It works with Claude Code for now; a version for Codex will follow.

1. Install:

```bash
git clone https://github.com/ButterFuture/AutoDL-Autopilot ~/.claude/skills/autodl-autopilot
```

2. Store the console token. In the logged-in AutoDL console press F12 and run `copy(localStorage.token)`, then run this in your own terminal, paste and press Enter. The token never passes through the conversation.

```bash
~/.claude/skills/autodl-autopilot/bin/autopilot token set
```

3. Tell the AI in your project:

```
This project's experiments run on AutoDL. Use autodl-autopilot to power the instance on and off.
```

On first use the AI makes a dedicated SSH key and puts its public half on the instance the first time it connects, so there is nothing for you to paste. It then asks for the instance ID, what GPU and no-GPU mode are for, the monthly budget and how long to wait before an idle shutdown, and writes this into the project's `CLAUDE.md`. After that, say what you need, or call it with `/autodl-autopilot`.

| You say | It does |
|---|---|
| start the training | checks the budget, powers on with GPU, starts the guard and the job |
| upload the data, then shut down | powers on without GPU (0.1 yuan an hour), uploads, shuts down, records the cost |
| how much budget is left this month | reads the local ledger, no power-on |
| any free 4090 in 内蒙 under 2 yuan? | searches the GPU market and lists machines with free GPUs by price |
| rent the first one, two GPUs | shows the plan first (machine, price, image, planned cost) and rents only after you agree |
| upload the data folder to the 内蒙 file storage | uploads it with the instance off; instances see it under `/root/autodl-fs` |
| add 100 GB to the data disk | quotes the price and says when it takes effect (a running instance needs a restart) |

<a id="how"></a>

## How it works

<p align="center"><img src="assets/arch.svg" alt="Architecture: you tell the AI; the AI calls autopilot on your computer through the skill; autopilot powers the instance and reads the bills through the console API and runs jobs over SSH; the guard on the instance shuts it down when idle" width="830"></p>

Every step the AI takes goes through `autopilot`, a command-line tool on your computer, and a power-on passes the budget check first.

Anything that involves the console (power, the shutdown timer, the balance, the bills, the GPU market, the file storage, the data disk) goes through its HTTP API. The instance's SSH address comes from there too, so you don't configure a host alias.

Jobs, file transfers and normal shutdowns on the instance go over SSH. Before every command it checks the host name and does nothing if it reached another machine.

The guard runs from the instance's data disk and decides whether the instance is idle from its real GPU, CPU and network use, whether or not the conversation is still open.

<a id="safety"></a>

## Cost and safety

<p align="center"><img src="assets/safety.svg" alt="Three safeguards: the budget gate, the guard, the console timer" width="830"></p>

Before each power-on it sets a console shutdown timer 30 minutes ahead and cancels it only once the guard runs on the instance. If the conversation dies right after the power-on, the platform still shuts the instance down.

Only you store the token, in `~/.config/autodl-autopilot/credentials` (mode 0600). It works with the screen locked, asleep or over SSH, and the AI never reads or shows it.

The SSH key installs itself. When the instance refuses it, autopilot logs in once with the root password the console shows and appends the key to `/root/.ssh/authorized_keys`; after that it uses the key. The password is never shown, stored or put on a command line. You can turn this off and paste the key into the console's 设置SSH免密登录 yourself.

Power-ons follow the permission and budget you gave. New costs such as renting a machine, adding data disk space or initializing file storage always come as a plan first and happen only after you agree, and so does deleting files from the file storage. Release, reset, cloning, recharging and renewal are not in the code at all.

An instance shut down for 15 days in a row is released by the platform, and everything on its data disk goes with it. After each shutdown autopilot reminds you to copy what matters to your computer or to the file storage if the instance will stay off for long.

If a job will sit almost idle for a long time (waiting for data, sleeping), tell the AI first. It holds the instance so the job isn't taken for idle.

The console API is not an official public API but the one AutoDL's own web page uses. A platform change can break it, and the commands then fail with a clear error.

<a id="faq"></a>

## FAQ

<details>
<summary><b>What if no GPU is free?</b></summary>

A pay-as-you-go instance keeps no GPU while it is off, so a GPU power-on can find its host full. The AI tells you and retries every few minutes, or looks for another machine with the same model on the GPU market. For moving data or editing code, no-GPU mode works in the meantime.
</details>

<details>
<summary><b>Does adding data disk space need a restart?</b></summary>

You can order more space while the instance runs, but it only takes effect after a restart; until then the instance keeps its old size. A restart means powering off and on again, which stops running jobs, and in GPU mode the power-off releases the GPU, which may be gone when you power on. So the AI says this before every order and suggests restarting once the jobs are done, or shutting down before adding space. Space added while the instance is off takes effect at the next power-on. Removing space works only while the instance is off, and only for pay-as-you-go instances.
</details>

<details>
<summary><b>What stays on the instance?</b></summary>

`/root/autodl-tmp/.autopilot/` on the data disk, with the guard script, job logs and state, plus one line with the public key in `/root/.ssh/authorized_keys` on the system disk. Autopilot starts the guard after each power-on, so an instance you power on by hand in the console has no guard.
</details>

<details>
<summary><b>How do I keep one process from being shut down while it is idle?</b></summary>

Ask the AI to whitelist it for a while, for example "keep Jupyter for 7 days". While that process is alive and the entry has not expired, the instance is not shut down for being idle; then the entry lapses on its own. Processes that are not whitelisted follow the default rule: only real GPU, CPU and network use counts, and a process that just sits there doing nothing is not busy.
</details>

<details>
<summary><b>Can the guard be off by default and on for one machine only?</b></summary>

Yes. Ask the AI to set the guard's default to off, then turn it on for the machine that needs it; the reverse works too. A machine without the guard does not shut itself down, so you or the AI power it off.
</details>

<details>
<summary><b>What if the instance is not used for a long time?</b></summary>

By the platform's rules, an instance that stays shut down for 15 days is released and its data deleted. The AI reminds you when fewer than 3 days are left. Copy anything you want to keep to your computer or the file storage before then.
</details>

<a id="disclaimer"></a>

## Disclaimer

1. This project is unofficial. It is not affiliated with, authorized by or endorsed by AutoDL or its operator.
2. It calls the API the console's web page uses itself, not an official public API. Users must follow AutoDL's terms of service and applicable laws.
3. Power-on, power-off, jobs and budget checks are carried out by an AI. Costs or data lost to an unwanted power-on, a missed shutdown, an interrupted job, a released instance and the like are the user's.
4. The console token is equivalent to the account's login. The consequences of a leak are the user's.
5. The software is provided as is, without warranty of any kind. The API may stop working when the platform changes, and the author has no duty to maintain it.

### Rights / takedown

If you believe this project infringes your rights or poses a risk, contact the author, including to ask for a takedown:

- Email: `ButterFuture@proton.me`
- Subject: `[AutoDL-Autopilot] Takedown / Rights Notice`
- Include: your identity and contact details, the paths or commits concerned, the basis of your rights, and the action you want.

## License

Copyright (C) 2026 ButterFuture. Released under the [GNU AGPL-3.0](LICENSE): you may use, change and share it freely, but a changed version, including one offered as a network service, must be published under the same license with its source.

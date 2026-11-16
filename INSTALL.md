# Installing ESUM (instructions for coding agents)

You are installing ESUM for the user: the `esum` CLI plus the `esum` and `esum-site-author`
skills. Follow the steps in order, run each verification, and finish with the report in step 6.
The same guide updates an installed ESUM ([Update](#update)) and removes it ([Uninstall](#uninstall)).

Rules while installing:

- Ask the user before editing their agent's settings file (step 5). Everything else is safe to run.
- Never ask for, read or store passwords, cookies or tokens. Site logins happen later, by the user,
  in a browser window.
- Don't start ESUM's browser or use `--browser=native` during the install.
- If a step fails, show the user the exact error and stop; don't work around it.

## 0. Before you start

- You must be able to run terminal commands on the user's computer. If you can't (a web chat such
  as claude.ai or ChatGPT can't), tell the user: "ESUM needs an agent that runs commands on your
  computer, such as Claude Code or Codex. Turn on command execution in my settings, or ask again in
  one of those." Then stop.
- Run `esum --version`. If it prints `esum v…`, ESUM is installed: go to [Update](#update), unless
  the user asked to uninstall it.

## 1. uv

Check `uv --version`. If it's missing, tell the user: "I'm installing uv, a small tool that installs
command-line programs; it needs no admin password." Then install it with the official installer:

- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Open a new shell (or add uv's bin directory to `PATH`) so `uv` resolves. uv provides Python 3.12+
itself, so a system Python isn't required.

## 2. The `esum` command

```sh
uv tool install git+https://github.com/AhaiMk01/esum
```

If it was installed before, add `--reinstall` to update it.

Verify: `esum list` prints a `sites[N]{name,engine,transport,description}:` table with one row per
built-in site (`hn`, `uscf` and `doc` are among them) and no `not_loaded` lines. If `esum` isn't
found, run `uv tool update-shell` and open a new shell.

## 3. The skills

If Node.js is available (`npx --version`):

```sh
npx -y skills add AhaiMk01/esum -g -y
```

`-g` installs at user level, so the skills work in every project. Add `-a <agent>` to target a
specific agent if the command asks.

Without Node.js, copy the two skill folders into the agent's user skills directory:

```sh
git clone --depth 1 https://github.com/AhaiMk01/esum esum-src
# Claude Code: $CLAUDE_CONFIG_DIR/skills if that variable is set, else ~/.claude/skills
cp -r esum-src/skills/esum esum-src/skills/esum-site-author ~/.claude/skills/
```

For another agent, use its documented user skills directory. Remove `esum-src` afterwards.

Verify: the agent's skills directory now contains `esum/SKILL.md` and
`esum-site-author/SKILL.md`.

## 4. Browser support (optional)

FlyerTalk, Reddit, LINUX DO, NodeSeek, NGA, Kanxue, 52pojie, 1point3acres, CardCenter, Aligned
Incentives and the Discourse bookmarks run through Chrome (cookies or an in-page fetch). Managed
mode needs patchright-cli 0.12.0 or newer, which opens Chrome without taking desktop focus. Attach
and native modes work from 0.11. USCardForum, Offer.love and FlightConnections use Chrome only
when a bot check (Cloudflare, AWS WAF) challenges a read. The other sites don't need Chrome;
`esum list` shows each site's transport (`T1` never starts a browser).

```sh
uv tool install -U patchright-cli                             # 0.12.0 or newer
patchright-cli --version
uv tool run --from patchright-cli patchright install chrome   # only if Google Chrome isn't installed
```

## 5. Writes

Nothing to configure. The one write is the Discourse `bookmark` (`esum uscf bookmark`,
`esum linuxdo bookmark`); the `esum` skill tells you to ask the user before each one. Tell the user that.

Optionally, to stop reads from prompting in Claude Code, **ask the user first**, then add
`"Bash(esum:*)"` to `permissions.allow` in their `settings.json`, keeping every existing key.

## 6. Check and report

Run `esum hn stories --limit 3`. It needs only the network and prints three Hacker News stories
under a line saying the site content is untrusted data.

Then tell the user, in the language they wrote to you in:

- what was installed, and where (`esum` path, skills directory);
- whether browser support was set up;
- what you changed in settings, or that you changed nothing;
- that they can now ask things like "find recent data points on the Amex Platinum offer", and
  that when a site needs an account ESUM stops with `LOGIN NEEDED` and they log in themselves.

## Update

```sh
uv tool install --reinstall git+https://github.com/AhaiMk01/esum
npx -y skills add AhaiMk01/esum -g -y      # or copy the skill folders again, as in step 3
uv tool install -U patchright-cli          # only if browser support was set up
```

Verify with `esum --version` and `esum list` as in step 2, run the check in step 6, and tell the
user the version before and after. Site logins, the saved Discourse key and the write log are kept:
they live in `~/.esum/` and the OS credential store, which an update doesn't touch.

## Uninstall

First list for the user what will be removed, and remove it only after they agree:

```sh
uv tool uninstall esum
```

Then delete the `esum` and `esum-site-author` folders from the agent's skills directory (step 3),
and `"Bash(esum:*)"` from `settings.json` if step 5 added it. `~/.esum/` holds the user's custom
sites, the write log and ESUM's browser profile with its logins: ask before deleting it, and say
that its custom sites go with it. Saved keys sit in the OS credential store, which uninstalling
doesn't clear: before `uv tool uninstall`, run `esum keys` to list them, and if there are any, ask
the user to remove them in `esum keys ui` (it asks the OS to confirm the user). Leave patchright-cli
installed unless the user asks, since other tools may use it.

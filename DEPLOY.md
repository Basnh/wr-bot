# Deploy the bot from Discord

Once the bot is running on the VPS, the owner can send `!deploy`. The command fetches
the configured GitHub branch, fast-forwards the checkout, installs
`requirements.txt` in the bot's active Python environment, and restarts the systemd
service. It refuses to deploy when the VPS checkout has uncommitted changes.

The owner can also run `!refresh_wr` to fetch every song ID's current leaderboard,
compare it with the previous snapshot, and save the latest rows and rank/score
changes to `data/wr_rankings.json`. The snapshot is runtime data and is excluded from Git.

## One-time setup

1. Create a private GitHub repository and push this bot project to the configured
   branch. Push and commit changes from the laptop before invoking `!deploy`.
2. On the VPS, install Git, Python, and OpenSSH; add a read-only GitHub deploy key
   for the private repository, then clone the repository and check out that branch.
3. Create a Python virtual environment in the bot directory and install
   `requirements.txt`. Copy `.env.example` to `.env` on the VPS, then set
   `DISCORD_TOKEN`, `ALERT_CHANNEL_ID`, `DEPLOY_GIT_BRANCH`, and
   `DEPLOY_SYSTEMD_SERVICE`. Never commit `.env`.
4. Copy `wr-bot.service.example` to `/etc/systemd/system/wr-bot.service`, replace
   `BOT_USER` with the Linux account that owns the checkout, and adjust the paths
   if needed. Run `systemctl daemon-reload`, then
   `systemctl enable --now wr-bot.service`.
5. Allow the account running the bot to restart only this service without a
   password using a sudoers rule. Check the `systemctl` path with
   `command -v systemctl`, then add a rule like
   `BOT_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart wr-bot.service`
   under `/etc/sudoers.d/` and validate it with `visudo`. `!deploy` uses
   `sudo -n systemctl restart <service>` and reports an error if permission is
   missing.

## Each update

Commit and push the desired code from the laptop to the configured branch, then
send `!deploy` in a channel the bot can read. The command does not create commits,
push local files, or overwrite uncommitted changes on the VPS.

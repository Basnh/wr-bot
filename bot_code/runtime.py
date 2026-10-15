RANKINGS_REFRESH_LOCK = asyncio.Lock()


@bot.command(name="refresh_wr", aliases=["update_wr", "wr_update"])
@commands.is_owner()
async def refresh_wr_cmd(ctx: commands.Context):
    """Làm mới bảng xếp hạng cho tất cả ID, lưu snapshot top 100 vào wr_rankings.json."""
    if RANKINGS_REFRESH_LOCK.locked():
        await ctx.send("Một lần cập nhật toàn bộ WR đang chạy rồi.")
        return

    async with RANKINGS_REFRESH_LOCK:
        song_ids = list(SONGS)
        if not song_ids:
            await ctx.send("Không có ID bài hát nào trong songs.json.")
            return

        try:
            rankings = await asyncio.to_thread(load_rankings)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            await ctx.send(f"Không đọc được snapshot WR hiện tại: {error}")
            return

        status_message = await ctx.send(
            f"Đang cập nhật bảng xếp hạng cho {len(song_ids)} bài..."
        )
        same_season = rankings.get("season") == SEASON
        refreshed = 0
        failed: list[tuple[str, str]] = []
        compared = rank_changes = score_changes = entered_top100 = left_top100 = 0
        score_improvements = 0
        change_samples: list[str] = []
        top100_alerts: list[discord.Embed] = []
        batch_size = 25

        async def update_progress(completed: int) -> None:
            try:
                await status_message.edit(
                    content=(
                        f"Đang cập nhật WR: {completed}/{len(song_ids)} bài "
                        f"(thành công {refreshed}, lỗi {len(failed)})..."
                    )
                )
            except discord.HTTPException as error:
                print(f"[refresh_wr] Không cập nhật được tin nhắn tiến độ: {error}", flush=True)

        for start in range(0, len(song_ids), batch_size):
            batch = song_ids[start : start + batch_size]
            cached_fetch_times = {
                song_id: (wr.cache.get(song_id) or {}).get("fetched")
                for song_id in batch
            }
            results = await asyncio.gather(
                *(wr.get(song_id, force=True) for song_id in batch),
                return_exceptions=True,
            )

            for song_id, result in zip(batch, results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    failed.append((song_id, type(result).__name__))
                    print(f"[refresh_wr] ID {song_id}: {result}", flush=True)
                    continue
                if not result or not isinstance(result.get("data"), list) or not result["data"]:
                    failed.append((song_id, "Không có dữ liệu WR"))
                    continue
                if result.get("fetched", 0) <= (cached_fetch_times[song_id] or 0):
                    failed.append((song_id, "API lỗi; nhận lại dữ liệu cache cũ"))
                    continue

                updated = result.get("updated")
                previous = (
                    rankings["songs"].get(song_id, {}).get("data", [])
                    if same_season
                    else []
                )
                change_report = (
                    compare_rankings(previous, result["data"])
                    if previous
                    else {
                        "rank_changes": 0,
                        "score_changes": 0,
                        "entered_top100": 0,
                        "left_top100": 0,
                        "players": [],
                    }
                )
                if previous:
                    compared += 1
                    rank_changes += change_report["rank_changes"]
                    score_changes += change_report["score_changes"]
                    entered_top100 += change_report["entered_top100"]
                    left_top100 += change_report["left_top100"]
                    if change_report["players"]:
                        top100_alerts.extend(_top100_change_embeds(song_id, change_report))
                    song_name = discord.utils.escape_mentions(display(song_id))[:45]
                    for change in change_report["players"]:
                        increase = _score_increase(change)
                        if increase is None or not _is_tracked_player(change):
                            continue
                        score_improvements += 1
                        nickname = discord.utils.escape_markdown(
                            discord.utils.escape_mentions(change.get("nickname") or "Unknown")
                        )[:24]
                        rank = (
                            f" · hạng hiện tại #{change['new_rank']}"
                            if change.get("new_rank") is not None
                            else ""
                        )
                        change_samples.append(
                            f"`{song_id}` {song_name}: **{nickname}** tăng điểm "
                            f"{change['old_score']:,} → {change['new_score']:,} "
                            f"(+{increase:,.0f}){rank}"
                        )

                rankings["songs"][song_id] = {
                    "updated": updated.isoformat() if updated else None,
                    "fetched_at": datetime.fromtimestamp(
                        result["fetched"], timezone.utc
                    ).isoformat(),
                    "entries": len(result["data"]),
                    "data": result["data"],
                    "changes": change_report,
                }
                refreshed += 1

            completed = min(start + len(batch), len(song_ids))
            if completed < len(song_ids):
                await update_progress(completed)

        if refreshed:
            rankings["season"] = SEASON
            rankings["updated_at"] = datetime.now(timezone.utc).isoformat()
            rankings["change_summary"] = {
                "compared_songs": compared,
                "rank_changes": rank_changes,
                "score_changes": score_changes,
                "entered_top100": entered_top100,
                "left_top100": left_top100,
            }
            try:
                await asyncio.to_thread(save_rankings, rankings)
            except (OSError, TypeError, ValueError) as error:
                await status_message.edit(
                    content=f"Đã tải mới {refreshed} bài nhưng không lưu được {RANKINGS_FILE}: {error}"
                )
                return

        summary = (
            f"✅ Đã cập nhật {refreshed}/{len(song_ids)} bài. "
            f"Snapshot và thay đổi được lưu tại `{os.path.basename(RANKINGS_FILE)}`."
            if refreshed
            else f"⚠️ Không cập nhật được dữ liệu mới cho bài nào trong {len(song_ids)} ID."
        )
        if compared:
            summary += (
                f"\nĐã so sánh {compared} bài: điểm tăng trong top 100 "
                f"ở **{score_improvements}** lượt."
            )
            baseline_created = refreshed - compared
            if baseline_created:
                summary += f" Tạo baseline mới cho {baseline_created} bài."
            if change_samples:
                summary += "\n**Một số thay đổi:**"
                shown = 0
                for line in change_samples:
                    sample = f"\n• {line}"
                    if len(summary) + len(sample) > 1500:
                        break
                    summary += sample
                    shown += 1
                remaining = len(change_samples) - shown
                if remaining > 0:
                    summary += f"\n... và {remaining} thay đổi khác."
        elif refreshed:
            summary += (
                f"\nChưa có snapshot cũ để so sánh; đã tạo baseline cho {refreshed} bài."
            )
        if failed:
            failed_ids = ", ".join(song_id for song_id, _ in failed[:20])
            more = f" và {len(failed) - 20} ID khác" if len(failed) > 20 else ""
            summary += (
                f"\nKhông cập nhật {len(failed)} bài: `{failed_ids}{more}`. "
                "Snapshot cũ của các ID lỗi được giữ nguyên."
            )
        try:
            await status_message.edit(content=summary)
        except discord.HTTPException as error:
            print(f"[refresh_wr] Không gửi được kết quả cuối: {error}", flush=True)
        await _send_alert_embeds(top100_alerts, "refresh_wr")


async def _run_deploy_process(
    args: list[str],
    cwd: str | None = None,
    timeout: float = 120,
) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as error:
        return 127, f"Không tìm thấy chương trình: {error.filename}"
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise
    return (
        process.returncode if process.returncode is not None else 0,
        output.decode("utf-8", errors="replace").strip(),
    )


@bot.command(name="deploy")
@commands.is_owner()
async def deploy_cmd(ctx: commands.Context):
    missing = [
        name
        for name, value in (
            ("DEPLOY_SYSTEMD_SERVICE", DEPLOY_SYSTEMD_SERVICE),
            ("DEPLOY_GIT_BRANCH", DEPLOY_GIT_BRANCH),
        )
        if not value
    ]
    if missing:
        await ctx.send(
            "Chưa cấu hình deploy trên VPS: "
            + ", ".join(f"`{name}`" for name in missing)
            + ". Hãy điền các giá trị trong `.env` theo `.env.example`."
        )
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", DEPLOY_GIT_BRANCH):
        await ctx.send("DEPLOY_GIT_BRANCH không hợp lệ.")
        return
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", DEPLOY_SYSTEMD_SERVICE):
        await ctx.send("DEPLOY_SYSTEMD_SERVICE phải là tên systemd service, ví dụ `wr-bot.service`.")
        return

    git_root_code, git_root = await _run_deploy_process(
        ["git", "rev-parse", "--show-toplevel"], cwd=BASE_DIR
    )
    if git_root_code != 0 or not git_root:
        await ctx.send("Không tìm thấy Git repository của bot trên VPS.")
        return
    git_root = git_root.splitlines()[-1]

    relative_env = os.path.relpath(os.path.join(BASE_DIR, ".env"), git_root)
    tracked_env_code, _ = await _run_deploy_process(
        ["git", "-C", git_root, "ls-files", "--error-unmatch", "--", relative_env]
    )
    if tracked_env_code == 0:
        await ctx.send(
            f"Deploy đã dừng: `{relative_env}` đang được Git theo dõi. "
            "Hãy gỡ file bí mật khỏi repository trước."
        )
        return

    branch_code, branch = await _run_deploy_process(
        ["git", "-C", git_root, "branch", "--show-current"]
    )
    if branch_code != 0 or branch != DEPLOY_GIT_BRANCH:
        await ctx.send(
            f"Branch đang checkout trên VPS không khớp với `{DEPLOY_GIT_BRANCH}`."
        )
        return

    status_code, status = await _run_deploy_process(
        ["git", "-C", git_root, "status", "--porcelain"]
    )
    if status_code != 0:
        await ctx.send(f"Không kiểm tra được trạng thái Git: {status}")
        return
    if status:
        await ctx.send(
            "Deploy đã dừng vì có thay đổi cục bộ chưa commit trên VPS. "
            "Hãy kiểm tra các thay đổi đó trước; bot không tự ghi đè chúng."
        )
        return

    remote_code, _ = await _run_deploy_process(
        ["git", "-C", git_root, "remote", "get-url", "origin"]
    )
    if remote_code != 0:
        await ctx.send("Git repository trên VPS chưa có remote `origin`.")
        return

    try:
        fetch_code, fetch_output = await _run_deploy_process(
            ["git", "-C", git_root, "fetch", "origin", DEPLOY_GIT_BRANCH],
            timeout=120,
        )
    except FileNotFoundError as error:
        await ctx.send(f"Không tìm thấy Git trên VPS: {error.filename}")
        return
    except asyncio.TimeoutError:
        await ctx.send("Git fetch quá thời gian chờ; chưa restart bot.")
        return
    except OSError as error:
        await ctx.send(f"Không thể chạy Git fetch: {error}")
        return
    if fetch_code != 0:
        await ctx.send(f"Không tải được code từ GitHub:\n```text\n{fetch_output[-1200:]}\n```")
        return

    pull_code, pull_output = await _run_deploy_process(
        ["git", "-C", git_root, "merge", "--ff-only", "FETCH_HEAD"],
        timeout=120,
    )
    if pull_code != 0:
        await ctx.send(
            f"Không thể fast-forward code trên VPS; bot chưa restart:\n"
            f"```text\n{pull_output[-1200:] or 'git merge --ff-only failed'}\n```"
        )
        return

    dependency_code, dependency_output = await _run_deploy_process(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            os.path.join(git_root, "requirements.txt"),
        ],
        cwd=git_root,
        timeout=300,
    )
    if dependency_code != 0:
        await ctx.send(
            "Code đã được cập nhật nhưng cài dependencies thất bại; bot chưa restart:\n"
            f"```text\n{dependency_output[-1200:] or 'pip install failed'}\n```"
        )
        return

    revision_code, revision = await _run_deploy_process(
        ["git", "-C", git_root, "rev-parse", "--short", "HEAD"]
    )
    if revision_code != 0:
        revision = "không xác định"
    await ctx.send(
        f"Đã cập nhật code tới `{revision}`. Đang restart `{DEPLOY_SYSTEMD_SERVICE}`..."
    )

    try:
        restart_code, restart_output = await _run_deploy_process(
            ["sudo", "-n", "systemctl", "restart", DEPLOY_SYSTEMD_SERVICE],
            timeout=30,
        )
    except FileNotFoundError as error:
        await ctx.send(f"Không tìm thấy lệnh cần thiết trên VPS: {error.filename}")
        return
    except asyncio.TimeoutError:
        await ctx.send("Lệnh restart quá thời gian chờ; hãy kiểm tra trạng thái service trên VPS.")
        return
    except OSError as error:
        await ctx.send(f"Không thể restart service: {error}")
        return
    if restart_code != 0:
        await ctx.send(
            f"Code đã được cập nhật nhưng restart thất bại. "
            f"Kiểm tra quyền sudo/systemd:\n```text\n{restart_output[-1200:] or 'systemctl restart failed'}\n```"
        )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return  # bỏ qua các tin nhắn bắt đầu bằng "!" nhưng không phải lệnh của bot
    if isinstance(error, commands.CheckFailure):
        await ctx.send("Bạn không có quyền dùng lệnh này.")
        return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"Cách dùng: `{PREFIX}{ctx.command.qualified_name} {ctx.command.signature}`")
        return
    traceback.print_exception(type(error), error, error.__traceback__)


@tasks.loop(seconds=AUTO_RELOAD_SECONDS)
async def songs_watcher():
    if _songs_mtime_now() != _songs_mtime:
        print(f"[auto-reload] {reload_songs()[1]}", flush=True)
    if _players_mtime_now() != _players_mtime:
        print(f"[auto-reload] {reload_players()[1]}", flush=True)


def _console_listener(loop: asyncio.AbstractEventLoop) -> None:
    """Đọc lệnh từ stdin: do manager.py gửi, hoặc gõ trực tiếp khi chạy bot trong terminal.
    Lệnh có sẵn: reload, quit. Mọi thay đổi dữ liệu đều chạy trên luồng của bot để tránh xung đột."""
    if sys.stdin is None:
        return
    try:
        while True:
            line = sys.stdin.readline()
            if not line:  # stdin đã đóng
                return
            cmd = line.strip().lower()
            if cmd == "reload":
                loop.call_soon_threadsafe(lambda: print(f"[reload] {reload_all()[1]}", flush=True))
            elif cmd in ("quit", "exit", "stop"):
                asyncio.run_coroutine_threadsafe(bot.close(), loop)
                return
            elif cmd:
                print(f"[console] lệnh không rõ: {cmd!r} (có: reload, quit)", flush=True)
    except Exception:
        return

# Source - https://stackoverflow.com/a/76881515
# Posted by treeben77
# Retrieved 2026-10-13, License - CC BY-SA 4.0

@bot.event
async def on_ready():
    print('Bot is now working!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Dino đang thức và đang code"))




# ----------------------------- Theo dõi WR mới -----------------------------
@tasks.loop(minutes=POLL_MINUTES)
async def watch():
    async with RANKINGS_REFRESH_LOCK:
        await _watch_cycle()


async def _watch_cycle() -> None:
    state = load_state()
    first_run = not state
    top1_alerts: list[discord.Embed] = []
    top100_alerts: list[discord.Embed] = []
    cached_revs = {
        song_id: (wr.cache.get(song_id) or {}).get("rev", 0)
        for song_id in SONGS
    }
    fresh_entries: dict[str, dict] = {}
    state_dirty: list[str] = []

    async def check(song_id: str):
        entry = await wr.get(song_id, force=True)
        if not entry or not entry["data"]:
            return
        data = entry["data"]
        fresh_entries[song_id] = entry
        top_score = data[0]["highscore"]
        holders = [p["nickname"] for p in data if p["highscore"] == top_score]
        top_rank = data[0].get("rank")

        prev = state.get(song_id)
        new_state = {"score": top_score, "holders": holders, "rank": top_rank}
        if prev != new_state:
            state[song_id] = new_state
            state_dirty.append(song_id)

        # bỏ qua lần chạy đầu (chỉ ghi nền) hoặc bài mới chưa có trạng thái
        if first_run or prev is None:
            return
        if top_score > prev["score"]:
            cover_url = SONGS[song_id].get("cover", "")
            embed = discord.Embed(
                title="🏆 World Record mới!",
                description=f"**{display(song_id)}**",
                color=discord.Color.gold(),
            )
            if cover_url.startswith(("https://", "http://")):
                embed.set_thumbnail(url=cover_url)
            embed.add_field(name="Điểm mới", value=f"{top_score:,}", inline=True)
            embed.add_field(name="Điểm cũ", value=f"{prev['score']:,}", inline=True)
            previous_rank = prev.get("rank")
            rank_text = (
                f"#{previous_rank if previous_rank is not None else '—'}"
                f" → #{top_rank if top_rank is not None else '—'}"
            )
            embed.add_field(name="Hạng trước → sau", value=rank_text, inline=True)
            if data[0].get("updatedAt"):
                embed.add_field(name="Đạt lúc", value=time_text(data[0]["updatedAt"]), inline=True)
            embed.add_field(
                name="Người đạt",
                value=", ".join(discord.utils.escape_markdown(n) for n in holders[:10]),
                inline=False,
            )
            embed.set_footer(text=f"Season {SEASON}")
            embed.timestamp = entry["updated"]
            top1_alerts.append(embed)

    await asyncio.gather(*(check(sid) for sid in list(SONGS)))
    if state_dirty:
        await asyncio.to_thread(save_state, state)

    # Chỉ những bài có dữ liệu mới (không phải 304) mới cần so sánh / đọc snapshot.
    changed_ids = [
        sid for sid, e in fresh_entries.items()
        if e.get("rev", 0) != cached_revs.get(sid, 0)
    ]
    rankings = None
    if changed_ids:
        try:
            rankings = await asyncio.to_thread(load_rankings)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(f"[watch] Không đọc được snapshot Top 100; bỏ qua so sánh lần này: {error}", flush=True)

    rankings_changed = False
    if rankings is not None:
        for song_id in changed_ids:
            entry = fresh_entries[song_id]
            data = entry.get("data")
            if not isinstance(data, list) or not data:
                continue

            previous = rankings["songs"].get(song_id, {}).get("data", [])
            if previous:
                change_report = compare_rankings(previous, data)
                if not change_report["players"]:
                    continue
                top100_alerts.extend(_top100_change_embeds(song_id, change_report))
            else:
                change_report = {
                    "rank_changes": 0,
                    "score_changes": 0,
                    "entered_top100": 0,
                    "left_top100": 0,
                    "players": [],
                }

            updated = entry.get("updated")
            rankings["songs"][song_id] = {
                "updated": updated.isoformat() if updated else None,
                "fetched_at": datetime.fromtimestamp(
                    entry["fetched"], timezone.utc
                ).isoformat(),
                "entries": len(data),
                "data": data,
                "changes": change_report,
            }
            rankings_changed = True

    if rankings is not None and rankings_changed:
        rankings["season"] = SEASON
        rankings["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await asyncio.to_thread(save_rankings, rankings)
        except (OSError, TypeError, ValueError) as error:
            top100_alerts.clear()
            print(f"[watch] Không lưu được snapshot Top 100; không gửi cảnh báo thay đổi: {error}", flush=True)

    await _send_alert_embeds(top1_alerts, "watch-top1", WR_ALERT_CHANNEL_ID)
    await _send_alert_embeds(top100_alerts, "watch-top100")


@watch.before_loop
async def before_watch():
    await bot.wait_until_ready()


bot.run(TOKEN)

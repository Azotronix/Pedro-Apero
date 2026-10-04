import asyncio
import logging
import math
import os
import random
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("pedro")

TOKEN = os.getenv("DISCORD_TOKEN")
COOKIES_FILE = os.getenv("YTDL_COOKIES_FILE")

MAX_PLAYLIST_ITEMS = 200
MAX_QUEUE_SIZE = 1000
DEFAULT_SOURCE = "youtube"
STREAM_TTL = 300
HISTORY_SIZE = 50
MIN_PLAY_SECONDS = 3
PAGE_SIZE = 10
COLOR = discord.Colour.from_rgb(255, 0, 51)

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
YT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}
URL_RE = re.compile(r"^(?:https?://|(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)/)", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class MusicError(Exception):
    pass


class LoopMode(Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


LOOP_LABELS = {
    LoopMode.OFF: "Désactivée",
    LoopMode.TRACK: "Musique actuelle uniquement",
    LoopMode.QUEUE: "Musique actuelle + file d'attente",
}


@dataclass
class Track:
    title: str
    url: str
    duration: Optional[float]
    uploader: Optional[str]
    thumbnail: Optional[str]
    requester: str
    stream_url: Optional[str] = None
    user_agent: Optional[str] = None
    stream_time: float = 0.0


def esc(text: str, limit: int = 80) -> str:
    text = discord.utils.escape_markdown(text or "").replace("[", "(").replace("]", ")")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def link(track: Track, limit: int = 80) -> str:
    return f"[{esc(track.title, limit)}]({track.url})"


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def progress_bar(position: float, duration: float, width: int = 16) -> str:
    index = int(min(max(position / duration, 0), 1) * width)
    return "▬" * index + "●" + "▬" * (width - index)


def cancel(task: Optional[asyncio.Task]) -> None:
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


def ytdl_options(**extra) -> dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 3,
        "extractor_retries": 2,
        "source_address": "0.0.0.0",
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    opts.update(extra)
    return opts


async def extract(query: str, **opts) -> Optional[dict]:
    def run():
        with yt_dlp.YoutubeDL(ytdl_options(**opts)) as ydl:
            info = ydl.extract_info(query, download=False)
            return ydl.sanitize_info(info) if info else None

    try:
        return await asyncio.wait_for(asyncio.to_thread(run), timeout=60)
    except yt_dlp.utils.DownloadError as exc:
        message = ANSI_RE.sub("", str(exc)).replace("ERROR: ", "").strip()
        log.warning("yt-dlp : %s", message)
        raise MusicError(f"YouTube a refusé la requête : {message[:250]}") from exc
    except asyncio.TimeoutError as exc:
        raise MusicError("YouTube met trop de temps à répondre.") from exc


def normalize_url(raw: str) -> tuple[str, bool]:
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in YT_HOSTS:
        raise MusicError("Seuls les liens YouTube et YouTube Music sont pris en charge.")

    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    path = parsed.path or "/"
    short = host.endswith("youtu.be")
    if short:
        video_id = path.strip("/").split("/")[0]
        if not video_id:
            raise MusicError("Lien YouTube invalide.")
        params["v"] = video_id
        path = "/watch"
    params.pop("si", None)

    netloc = "www.youtube.com" if short or path.startswith(("/watch", "/playlist")) else host
    url = urlunparse(("https", netloc, path, "", urlencode(params), ""))
    single = "list" not in params and path.startswith(("/watch", "/shorts/"))
    return url, single


def entry_to_track(entry: Optional[dict], requester: str, with_stream: bool = False) -> Optional[Track]:
    if not entry:
        return None
    title = entry.get("title") or "Titre inconnu"
    if title in ("[Private video]", "[Deleted video]"):
        return None

    video_id = entry.get("id") or ""
    if len(video_id) == 11:
        url = f"https://www.youtube.com/watch?v={video_id}"
        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    else:
        url = entry.get("webpage_url") or entry.get("url") or ""
        if "watch?v=" not in url:
            return None
        thumbnail = entry.get("thumbnail")

    track = Track(
        title=title,
        url=url,
        duration=entry.get("duration"),
        uploader=entry.get("uploader") or entry.get("channel"),
        thumbnail=thumbnail,
        requester=requester,
    )
    if with_stream and entry.get("formats") and entry.get("url"):
        track.stream_url = entry["url"]
        track.user_agent = (entry.get("http_headers") or {}).get("User-Agent")
        track.stream_time = time.time()
    return track


def info_to_tracks(info: Optional[dict], requester: str, with_stream: bool = False):
    if not info:
        return [], None
    if info.get("_type") == "playlist" or "entries" in info:
        entries = (entry_to_track(e, requester, with_stream) for e in info.get("entries") or [])
        return [t for t in entries if t], info.get("title")
    track = entry_to_track(info, requester, with_stream)
    return ([track] if track else []), None


async def resolve_stream(track: Track) -> tuple[str, Optional[str]]:
    if track.stream_url and time.time() - track.stream_time < STREAM_TTL:
        return track.stream_url, track.user_agent
    info = await extract(track.url, noplaylist=True)
    if not info or not info.get("url"):
        raise MusicError("Flux audio introuvable.")
    track.stream_url = info["url"]
    track.user_agent = (info.get("http_headers") or {}).get("User-Agent")
    track.stream_time = time.time()
    track.duration = track.duration or info.get("duration")
    return track.stream_url, track.user_agent


async def resolve_url(url: str, requester: str):
    clean, single = normalize_url(url)

    # Les Mix YouTube (listes dont l'identifiant commence par RD) sont des
    # playlists dynamiques : le lien pointe vers une vidéo de départ et le
    # paramètre list contient le Mix généré par YouTube.
    parsed = urlparse(clean)
    params = parse_qs(parsed.query)
    playlist_id = params.get("list", [""])[0]
    is_mix = playlist_id.upper().startswith("RD")

    if is_mix:
        params["start_radio"] = ["1"]
        mix_url = urlunparse((
            "https",
            parsed.netloc,
            parsed.path,
            "",
            urlencode(params, doseq=True),
            "",
        ))
        info = await extract(
            mix_url,
            noplaylist=False,
            extract_flat="in_playlist",
            playlistend=MAX_PLAYLIST_ITEMS,
        )
        tracks, title = info_to_tracks(info, requester)
        if tracks:
            return tracks, title or "YouTube Mix"

        # Si YouTube/yt-dlp ne renvoie pas le Mix, on garde au moins la vidéo
        # explicitement présente dans le lien.
        info = await extract(clean, noplaylist=True)
        return info_to_tracks(info, requester, with_stream=True)

    if single:
        info = await extract(clean, noplaylist=True)
        return info_to_tracks(info, requester, with_stream=True)

    info = await extract(clean, noplaylist=False, extract_flat="in_playlist", playlistend=MAX_PLAYLIST_ITEMS)
    return info_to_tracks(info, requester)


async def resolve_search(query: str, source: str, requester: str):
    if source == "ytmusic":
        try:
            info = await extract(
                f"https://music.youtube.com/search?q={quote_plus(query)}#songs",
                noplaylist=False, extract_flat="in_playlist", playlistend=1,
            )
            tracks, _ = info_to_tracks(info, requester)
            if tracks:
                return tracks[:1], None
        except MusicError:
            log.info("Recherche YouTube Music indisponible, repli sur YouTube")
    info = await extract(f"ytsearch1:{query}", noplaylist=False)
    tracks, _ = info_to_tracks(info, requester, with_stream=True)
    if not tracks:
        raise MusicError(f"Aucun résultat pour « {esc(query, 100)} ».")
    return tracks[:1], None


async def resolve_query(query: str, source: str, requester: str):
    query = query.strip().strip("<>")
    if not query:
        raise MusicError("Indique un lien ou des mots-clés.")
    if URL_RE.match(query):
        return await resolve_url(query, requester)
    return await resolve_search(query, source, requester)


class Player:
    def __init__(self, bot: "Pedro", guild: discord.Guild, text_channel):
        self.bot = bot
        self.guild = guild
        self.text_channel = text_channel
        self.queue: list[Track] = []
        self.history: list[Track] = []
        self.current: Optional[Track] = None
        self.loop = LoopMode.OFF
        self.controls_message: Optional[discord.Message] = None

        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._track_done = asyncio.Event()
        self._end_reason = "finished"
        self._skip_announce = False
        self._started_at: Optional[float] = None
        self._paused_total = 0.0
        self._pause_started: Optional[float] = None
        self._destroyed = False
        self._task = asyncio.create_task(self._run())

    @property
    def position(self) -> float:
        if self._started_at is None:
            return 0.0
        now = self._pause_started if self._pause_started is not None else time.monotonic()
        return max(0.0, now - self._started_at - self._paused_total)

    def now_playing_embed(self, progress: bool = False) -> discord.Embed:
        track = self.current
        embed = discord.Embed(title="Lecture en cours", description=link(track, 100), colour=COLOR)
        if progress and track.duration:
            pos = self.position
            bar = progress_bar(pos, track.duration)
            embed.add_field(
                name="Progression",
                value=f"`{fmt_duration(pos)}` {bar} `{fmt_duration(track.duration)}`",
                inline=False,
            )
        else:
            embed.add_field(name="Durée", value=fmt_duration(track.duration))
        embed.add_field(name="Demandé par", value=esc(track.requester, 30))
        if track.uploader:
            embed.add_field(name="Chaîne", value=esc(track.uploader, 30))
        embed.add_field(name="Boucle", value=LOOP_LABELS[self.loop], inline=False)
        embed.set_footer(text=f"{len(self.queue)} titre(s) en attente")
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed

    def _vc(self) -> discord.VoiceClient:
        vc = self.guild.voice_client
        if vc is None or not vc.is_connected():
            raise MusicError("Je ne suis connecté à aucun salon vocal.")
        return vc

    def _require_current(self) -> Track:
        if self.current is None:
            raise MusicError("Aucune musique n'est en cours de lecture.")
        return self.current

    def _stop_audio(self) -> None:
        vc = self.guild.voice_client
        if vc is not None:
            vc.stop()

    def _check_index(self, index: int) -> None:
        if not self.queue:
            raise MusicError("La file d'attente est vide.")
        if not 1 <= index <= len(self.queue):
            raise MusicError(f"Index invalide : choisis un nombre entre 1 et {len(self.queue)}.")

    def _remember(self, track: Track) -> None:
        self.history.append(track)
        del self.history[:-HISTORY_SIZE]

    async def send(self, content: Optional[str] = None, **kwargs) -> Optional[discord.Message]:
        if self.text_channel is None:
            return None
        try:
            return await self.text_channel.send(content, **kwargs)
        except discord.HTTPException:
            return None

    async def _clear_controls(self) -> None:
        message, self.controls_message = self.controls_message, None
        if message is not None:
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                pass

    def add_tracks(self, tracks: list[Track], front: bool = False) -> int:
        tracks = tracks[: max(MAX_QUEUE_SIZE - len(self.queue), 0)]
        if front:
            self.queue[0:0] = tracks
        else:
            self.queue.extend(tracks)
        if tracks:
            self._wakeup.set()
        return len(tracks)

    def pause(self) -> None:
        vc = self._vc()
        self._require_current()
        if vc.is_paused():
            raise MusicError("La musique est déjà en pause.")
        vc.pause()
        self._pause_started = time.monotonic()

    def resume(self) -> None:
        vc = self._vc()
        self._require_current()
        if not vc.is_paused():
            raise MusicError("La musique n'est pas en pause.")
        vc.resume()
        if self._pause_started is not None:
            self._paused_total += time.monotonic() - self._pause_started
            self._pause_started = None

    def toggle_pause(self) -> bool:
        if self._vc().is_paused():
            self.resume()
            return False
        self.pause()
        return True

    def skip(self) -> Track:
        current = self._require_current()
        self._end_reason = "skipped"
        self._stop_audio()
        return current

    def skip_to(self, index: int) -> Track:
        # Le titre choisi passe en tête de file, puis skip classique : rien n'est supprimé.
        self._require_current()
        self._check_index(index)
        track = self.queue.pop(index - 1)
        self.queue.insert(0, track)
        self.skip()
        return track

    def previous(self) -> None:
        self._require_current()
        if not self.history:
            raise MusicError("Il n'y a pas de titre précédent.")
        self._end_reason = "previous"
        self._stop_audio()

    def stop(self) -> None:
        if self.current is None and not self.queue:
            raise MusicError("Il n'y a rien à arrêter.")
        self.queue.clear()
        self._end_reason = "stopped"
        self._stop_audio()

    def shuffle(self) -> None:
        if len(self.queue) < 2:
            raise MusicError("Il faut au moins 2 titres dans la file pour la mélanger.")
        random.shuffle(self.queue)

    def cycle_loop(self) -> LoopMode:
        modes = list(LoopMode)
        self.loop = modes[(modes.index(self.loop) + 1) % len(modes)]
        return self.loop

    def remove(self, index: int) -> Track:
        self._check_index(index)
        return self.queue.pop(index - 1)

    def move(self, source: int, destination: int) -> Track:
        self._check_index(source)
        self._check_index(destination)
        track = self.queue.pop(source - 1)
        self.queue.insert(destination - 1, track)
        return track

    def clear(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    async def destroy(self, disconnect: bool = True) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._end_reason = "stopped"
        self.queue.clear()
        self.bot.players.pop(self.guild.id, None)
        vc = self.guild.voice_client
        if vc is not None:
            vc.stop()
            if disconnect:
                try:
                    await vc.disconnect()
                except Exception:
                    log.exception("Erreur pendant la déconnexion vocale")
        await self._clear_controls()
        cancel(self._task)

    def _after(self, error: Optional[Exception]) -> None:
        if error:
            log.error("Erreur de lecture : %s", error)
            if self._end_reason == "finished":
                self._end_reason = "error"
        self._loop.call_soon_threadsafe(self._track_done.set)

    async def _wait_for_track(self) -> Track:
        while not self.queue:
            self._wakeup.clear()
            await self._wakeup.wait()
        return self.queue.pop(0)

    async def _create_source(self, track: Track) -> discord.PCMVolumeTransformer:
        stream_url, user_agent = await resolve_stream(track)
        before = FFMPEG_BEFORE
        if user_agent:
            before += f" -user_agent {shlex.quote(user_agent)}"
        source = discord.FFmpegPCMAudio(stream_url, before_options=before, options="-vn")
        return discord.PCMVolumeTransformer(source, volume=1.0)

    async def _run(self) -> None:
        while not self._destroyed:
            try:
                if not await self._play_once():
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erreur dans la boucle de lecture (serveur %s)", self.guild.id)
                self.current = None
                await asyncio.sleep(2)

    async def _play_once(self) -> bool:
        track = await self._wait_for_track()
        self.current = track
        announce, self._skip_announce = not self._skip_announce, False
        self._end_reason = "finished"
        self._track_done.clear()
        self._started_at = None
        self._paused_total = 0.0
        self._pause_started = None

        try:
            source = await self._create_source(track)
        except Exception as exc:
            log.warning("Lecture impossible (%s) : %s", track.url, exc)
            reason = str(exc) if isinstance(exc, MusicError) else "erreur inattendue"
            await self.send(f"Impossible de lire **{esc(track.title)}** ({reason[:200]}). Passage au suivant.")
            self.current = None
            return True

        vc = self.guild.voice_client
        started = False
        if self._destroyed or vc is None or not vc.is_connected():
            source.cleanup()
            await self.destroy(disconnect=False)
            return False
        if self._end_reason != "finished":
            # skip ou stop demandé pendant le chargement
            source.cleanup()
            self._track_done.set()
        else:
            vc.play(source, after=self._after)
            self._started_at = time.monotonic()
            started = True
            if announce:
                self.controls_message = await self.send(embed=self.now_playing_embed(), view=PlayerControls(self))

        await self._track_done.wait()
        if self._destroyed:
            return False

        reason = self._end_reason
        finished, self.current = self.current, None
        played = time.monotonic() - (self._started_at or time.monotonic()) - self._paused_total
        if reason == "finished" and started and played < MIN_PLAY_SECONDS \
                and (finished.duration is None or finished.duration > 6):
            reason = "error"

        if reason in ("finished", "skipped"):
            self._remember(finished)
            if reason == "finished" and self.loop is LoopMode.TRACK:
                self.queue.insert(0, finished)
                self._skip_announce = True
            elif self.loop is LoopMode.QUEUE:
                self.queue.append(finished)
        elif reason == "previous":
            self.queue.insert(0, finished)
            if self.history:
                self.queue.insert(0, self.history.pop())
        elif reason == "error":
            finished.stream_url = None
            await self.send(f"La lecture de **{esc(finished.title)}** s'est interrompue. Passage au suivant.")
        await self._clear_controls()
        return True


def build_queue_embed(player: Player, page: int = 1):
    total = len(player.queue)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), pages)

    lines = []
    if player.current:
        lines.append(f"**En cours :** {link(player.current)} `{fmt_duration(player.current.duration)}`\n")
    if total == 0:
        lines.append("*Aucun titre en attente.*")
    start = (page - 1) * PAGE_SIZE
    for number, track in enumerate(player.queue[start:start + PAGE_SIZE], start=start + 1):
        lines.append(f"`{number}.` {link(track, 55)} `{fmt_duration(track.duration)}` - {esc(track.requester, 20)}")

    embed = discord.Embed(title="File d'attente", description="\n".join(lines), colour=COLOR)
    footer = f"Page {page}/{pages} | {total} titre(s)"
    known = [t.duration for t in player.queue if t.duration]
    if known:
        footer += f" | {fmt_duration(sum(known))}"
    footer += f" | Boucle : {LOOP_LABELS[player.loop]}"
    embed.set_footer(text=footer)
    return embed, page, pages


class QueueView(discord.ui.View):
    def __init__(self, player: Player, page: int, pages: int):
        super().__init__(timeout=120)
        self.player = player
        self.page = page
        self.pages = pages
        self.message: Optional[discord.Message] = None
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= self.pages

    async def _refresh(self, interaction: discord.Interaction) -> None:
        embed, self.page, self.pages = build_queue_embed(self.player, self.page)
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Précédent", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._refresh(interaction)

    @discord.ui.button(label="Suivant", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class PlayerControls(discord.ui.View):
    def __init__(self, player: Player):
        super().__init__(timeout=None)
        self.player = player

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = self.player.guild.voice_client
        voice = getattr(interaction.user, "voice", None)
        if vc is None or voice is None or voice.channel != vc.channel:
            await interaction.response.send_message("Rejoins mon salon vocal pour utiliser ces boutons.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        if isinstance(error, MusicError):
            text = str(error)
        else:
            log.error("Erreur dans un bouton de contrôle", exc_info=error)
            text = "Une erreur inattendue est survenue."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.previous()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        paused = self.player.toggle_pause()
        await interaction.response.send_message("En pause." if paused else "Lecture reprise.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.skip()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.stop()
        await interaction.response.defer()

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.shuffle()
        await interaction.response.send_message("File d'attente mélangée.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        mode = self.player.cycle_loop()
        await interaction.response.send_message(f"Boucle : {LOOP_LABELS[mode]}", ephemeral=True)


def user_voice_channel(interaction: discord.Interaction):
    voice = getattr(interaction.user, "voice", None)
    if voice is None or voice.channel is None:
        raise MusicError("Tu dois d'abord rejoindre un salon vocal.")
    return voice.channel


SOURCES = [
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="YouTube Music", value="ytmusic"),
]


class Music(commands.Cog):
    def __init__(self, bot: "Pedro"):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Ces commandes ne fonctionnent que sur un serveur.", ephemeral=True)
            return False
        return True

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return
        original = getattr(error, "original", error)
        if isinstance(original, MusicError):
            text = str(original)
        else:
            name = interaction.command.name if interaction.command else "?"
            log.error("Erreur dans /%s", name, exc_info=error)
            text = "Une erreur inattendue est survenue."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    def get_player(self, guild: discord.Guild, channel) -> Player:
        player = self.bot.players.get(guild.id)
        if player is None:
            player = Player(self.bot, guild, channel)
            self.bot.players[guild.id] = player
        else:
            player.text_channel = channel
        return player

    def existing_player(self, interaction: discord.Interaction, same_channel: bool = True) -> Player:
        player = self.bot.players.get(interaction.guild.id)
        vc = interaction.guild.voice_client
        if player is None or vc is None:
            raise MusicError("Je ne suis connecté à aucun salon vocal.")
        if same_channel:
            voice = getattr(interaction.user, "voice", None)
            if voice is None or voice.channel != vc.channel:
                raise MusicError("Tu dois être dans mon salon vocal pour faire ça.")
        return player

    async def ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient:
        channel = user_voice_channel(interaction)
        guild = interaction.guild
        vc = guild.voice_client
        if vc is None:
            perms = channel.permissions_for(guild.me)
            if not (perms.connect and perms.speak):
                raise MusicError("Je n'ai pas la permission de rejoindre ce salon vocal et d'y parler.")
            try:
                vc = await channel.connect(self_deaf=True)
            except Exception as exc:
                log.exception("Connexion vocale impossible")
                raise MusicError(f"Connexion au salon vocal impossible ({type(exc).__name__}).") from exc
        elif vc.channel != channel:
            if any(not m.bot for m in vc.channel.members):
                raise MusicError(f"Je suis déjà en train de jouer dans {vc.channel.mention}.")
            await vc.move_to(channel)
        return vc

    async def queue_autocomplete(self, interaction: discord.Interaction, current: str):
        player = self.bot.players.get(interaction.guild_id) if interaction.guild_id else None
        if player is None:
            return []
        choices = []
        for number, track in enumerate(player.queue, start=1):
            label = f"{number}. {track.title}"
            if current and current.lower() not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=number))
            if len(choices) >= 25:
                break
        return choices

    async def enqueue(self, interaction: discord.Interaction, query: str, source: str, shuffle: bool, front: bool):
        await interaction.response.defer()

        tracks, playlist_title = await resolve_query(query, source, interaction.user.display_name)
        if not tracks:
            raise MusicError("Aucun titre trouvé pour cette requête.")
        await self.ensure_voice(interaction)
        player = self.get_player(interaction.guild, interaction.channel)

        if shuffle:
            random.shuffle(tracks)
        was_idle = player.current is None and not player.queue
        added = player.add_tracks(tracks, front=front)
        if added == 0:
            raise MusicError(f"La file d'attente est pleine ({MAX_QUEUE_SIZE} titres maximum).")

        embed = discord.Embed(colour=COLOR)
        if len(tracks) == 1:
            track = tracks[0]
            if was_idle:
                embed.description = f"**Lecture de** {link(track, 100)}"
            else:
                position = 1 if front else len(player.queue)
                embed.description = f"**Ajouté en position {position}** : {link(track, 100)}"
            embed.add_field(name="Durée", value=fmt_duration(track.duration))
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
        else:
            name = esc(playlist_title or "Playlist", 100)
            embed.description = f"**{added} titres ajoutés** depuis **{name}**" + (" (mélangés)" if shuffle else "")
            known = [t.duration for t in tracks[:added] if t.duration]
            if known:
                embed.add_field(name="Durée totale", value=fmt_duration(sum(known)))
            notes = []
            if len(tracks) >= MAX_PLAYLIST_ITEMS:
                notes.append(f"Import limité aux {MAX_PLAYLIST_ITEMS} premiers titres.")
            if added < len(tracks):
                notes.append(f"File limitée à {MAX_QUEUE_SIZE} titres : {len(tracks) - added} ignoré(s).")
            if notes:
                embed.set_footer(text=" ".join(notes))
        embed.add_field(name="Demandé par", value=esc(interaction.user.display_name, 30))
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="play", description="Joue un titre, une playlist ou un mix (YouTube / YouTube Music)")
    @app_commands.describe(
        query="Lien YouTube / YouTube Music ou mots-clés",
        source="Où chercher si tu tapes des mots-clés",
        shuffle="Mélanger les titres ajoutés",
    )
    @app_commands.choices(source=SOURCES)
    async def play(self, interaction: discord.Interaction, query: str,
                   source: Optional[app_commands.Choice[str]] = None, shuffle: bool = False):
        await self.enqueue(interaction, query, source.value if source else DEFAULT_SOURCE, shuffle, front=False)

    @app_commands.command(name="playnext", description="Ajoute un titre juste après la musique en cours")
    @app_commands.describe(query="Lien YouTube / YouTube Music ou mots-clés", source="Où chercher si tu tapes des mots-clés")
    @app_commands.choices(source=SOURCES)
    async def playnext(self, interaction: discord.Interaction, query: str,
                       source: Optional[app_commands.Choice[str]] = None):
        await self.enqueue(interaction, query, source.value if source else DEFAULT_SOURCE, False, front=True)

    @app_commands.command(name="pause", description="Met la musique en pause")
    async def pause(self, interaction: discord.Interaction):
        self.existing_player(interaction).pause()
        await interaction.response.send_message("Musique mise en pause.")

    @app_commands.command(name="resume", description="Reprend la lecture")
    async def resume(self, interaction: discord.Interaction):
        self.existing_player(interaction).resume()
        await interaction.response.send_message("Lecture reprise.")

    @app_commands.command(name="skip", description="Passe à la musique suivante")
    async def skip(self, interaction: discord.Interaction):
        skipped = self.existing_player(interaction).skip()
        await interaction.response.send_message(f"**{esc(skipped.title)}** passé.")

    @app_commands.command(name="skipto", description="Saute à un titre de la file sans supprimer les précédents")
    @app_commands.describe(index="Position du titre dans la file (voir /queue)")
    async def skipto(self, interaction: discord.Interaction, index: int):
        track = self.existing_player(interaction).skip_to(index)
        await interaction.response.send_message(f"Saut vers la position {index} : **{esc(track.title)}**")

    @skipto.autocomplete("index")
    async def skipto_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.queue_autocomplete(interaction, current)

    @app_commands.command(name="previous", description="Revient au titre précédent")
    async def previous(self, interaction: discord.Interaction):
        self.existing_player(interaction).previous()
        await interaction.response.send_message("Retour au titre précédent.")

    @app_commands.command(name="stop", description="Arrête la musique et vide la file d'attente")
    async def stop(self, interaction: discord.Interaction):
        self.existing_player(interaction).stop()
        await interaction.response.send_message("Lecture arrêtée, file d'attente vidée.")

    @app_commands.command(name="leave", description="Déconnecte le bot du salon vocal")
    async def leave(self, interaction: discord.Interaction):
        player = self.existing_player(interaction)
        await interaction.response.send_message("À bientôt.")
        await player.destroy()

    @app_commands.command(name="queue", description="Affiche la file d'attente")
    @app_commands.describe(page="Numéro de page")
    async def queue(self, interaction: discord.Interaction, page: int = 1):
        player = self.existing_player(interaction, same_channel=False)
        embed, page, pages = build_queue_embed(player, page)
        if pages <= 1:
            await interaction.response.send_message(embed=embed)
            return
        view = QueueView(player, page, pages)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="nowplaying", description="Affiche la musique en cours")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.existing_player(interaction, same_channel=False)
        if player.current is None:
            raise MusicError("Aucune musique n'est en cours de lecture.")
        await interaction.response.send_message(embed=player.now_playing_embed(progress=True))

    @app_commands.command(name="shuffle", description="Mélange la file d'attente")
    async def shuffle(self, interaction: discord.Interaction):
        player = self.existing_player(interaction)
        player.shuffle()
        await interaction.response.send_message(f"File d'attente mélangée ({len(player.queue)} titres).")

    @app_commands.command(name="loop", description="Règle le mode de répétition")
    @app_commands.describe(mode="Mode de répétition")
    @app_commands.choices(mode=[
        app_commands.Choice(name=label, value=mode.value) for mode, label in LOOP_LABELS.items()
    ])
    async def loop_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        player = self.existing_player(interaction)
        player.loop = LoopMode(mode.value)
        await interaction.response.send_message(f"Boucle : {LOOP_LABELS[player.loop]}")

    @app_commands.command(name="remove", description="Retire un titre de la file d'attente")
    @app_commands.describe(index="Position du titre dans la file")
    async def remove(self, interaction: discord.Interaction, index: int):
        track = self.existing_player(interaction).remove(index)
        await interaction.response.send_message(f"**{esc(track.title)}** retiré de la file.")

    @remove.autocomplete("index")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.queue_autocomplete(interaction, current)

    @app_commands.command(name="move", description="Déplace un titre dans la file d'attente")
    @app_commands.describe(source="Position actuelle du titre", destination="Nouvelle position")
    async def move(self, interaction: discord.Interaction, source: int, destination: int):
        track = self.existing_player(interaction).move(source, destination)
        await interaction.response.send_message(f"**{esc(track.title)}** déplacé de {source} à {destination}.")

    @move.autocomplete("source")
    async def move_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.queue_autocomplete(interaction, current)

    @app_commands.command(name="clear", description="Vide la file d'attente sans arrêter la musique en cours")
    async def clear(self, interaction: discord.Interaction):
        count = self.existing_player(interaction).clear()
        await interaction.response.send_message(f"File d'attente vidée ({count} titre(s)).")


class Pedro(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.players: dict[int, Player] = {}

    async def setup_hook(self) -> None:
        if not shutil.which("ffmpeg"):
            log.error("FFmpeg est introuvable dans le PATH")
        await self.add_cog(Music(self))
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Pedro connecté en tant que %s (%s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))

    async def close(self) -> None:
        for player in list(self.players.values()):
            await player.destroy()
        await super().close()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN manquant dans le fichier .env")
    Pedro().run(TOKEN)

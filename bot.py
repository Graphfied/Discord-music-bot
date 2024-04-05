import asyncio
import functools
import itertools
import math
import random
import nextcord
from async_timeout import timeout
from nextcord.ext import commands
import discord
import yt_dlp as youtube_dl


intents = nextcord.Intents.default()
intents.messages = True
intents.guilds = True
intents.voice_states = True
intents.message_content = True  # Add this line

# Silence unnecessary bug reports messages from youtube_dl
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass

class YTDLError(Exception):
    pass

class YTDLSource(nextcord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0'
    }


    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn'
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: nextcord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = f'{date[6:8]}.{date[4:6]}.{date[:4]}'
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError(f"Couldn't find anything that matches `{search}`")

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(f"Couldn't find anything that matches `{search}`")

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError(f"Couldn't fetch `{webpage_url}`")

        if 'entries' in processed_info:
            processed_info = processed_info['entries'][0]

        return cls(ctx, nextcord.FFmpegPCMAudio(processed_info['url'], **cls.FFMPEG_OPTIONS), data=processed_info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append(f'{days}d')
        if hours > 0:
            duration.append(f'{hours}h')
        if minutes > 0:
            duration.append(f'{minutes}m')
        if seconds > 0:
            duration.append(f'{seconds}s')

        return ', '.join(duration)

class MusicPlayer:
    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx: commands.Context):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and continue with the next song.
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.create_source(self._guild, source, loop=self.bot.loop)
                except YTDLError as e:
                    await self._channel.send(f'There was an error processing your song.\n`{e}`')
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            self.np = await self._channel.send(f'**Now Playing:** `{source.title}` requested by `{source.requester}`')
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

            try:
                # We're no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        return self.bot.loop.create_task(self._cog.cleanup(guild))

class Music(commands.Cog):
    __slots__ = ('bot', 'players')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass


    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel

        if ctx.voice_client:
            await ctx.voice_client.move_to(destination)
            return

        await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: nextcord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            return await ctx.send("You are neither connected to a voice channel nor specified a channel to join.")

        destination = channel or ctx.author.voice.channel

        if ctx.voice_client:
            await ctx.voice_client.move_to(destination)
            return

        ctx.voice_client = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_client:
            return await ctx.send("I'm not in a voice channel.")

        await ctx.voice_client.disconnect()

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the player."""

        if not ctx.voice_client:
            return await ctx.send("Not connected to a voice channel.")

        if not 0 < volume <= 100:
            return await ctx.send("Volume must be between 1 and 100")

        if ctx.voice_client.source:
            ctx.voice_client.source.volume = volume / 100

        await ctx.send(f"Changed volume to {volume}%")

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""

        await ctx.send(embed=self.get_player(ctx).current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send("I am not currently playing anything!")
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send("Paused the song!")

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send("I am not currently connected to voice!")
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send("Resumed the song!")

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send("I am not currently playing anything!")

        if ctx.author == self.get_player(ctx).current.requester:
            await ctx.send("Requester skipped the song")
            vc.stop()
        else:
            # Vote logic here
            pass

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue."""

        # Queue display logic here

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        player = self.get_player(ctx)
        if len(player.queue._queue) > 2:
            random.shuffle(player.queue._queue)
            await ctx.send("Shuffled the queue.")
        else:
            await ctx.send("Add more songs to shuffle the queue.")

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        player = self.get_player(ctx)

        if len(player.queue._queue) >= index:
            del player.queue._queue[index - 1]
            await ctx.send(f"Removed song number {index} from the queue.")
        else:
            await ctx.send("Song not found in queue.")

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song.
        If there are songs in the queue, this will be queued instead.
        """

        if not ctx.voice_client:
            await ctx.invoke(self._join)

        player = self.get_player(ctx)

        # Partial function to create YTDLSource with data provided
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)

        await player.queue.put(source)

    def get_player(self, ctx):
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not member.id == self.bot.user.id:
            return

        if after.channel is None:
            # We're no longer in a voice channel, clean up
            try:
                del self.players[member.guild.id]
            except KeyError:
                pass


def setup(bot):
    bot.add_cog(Music(bot))



# Initialize the bot
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    print('Bot is ready!')
    print('------')
    print('Use the !play command to play a song.')
    print('------')
    print('Use the !join command to join a voice channel.')
    print('------')
    print('Use the !leave command to leave a voice channel.')
    await bot.change_presence(activity=nextcord.Activity(type=nextcord.ActivityType.listening, name="777 💖"))

# Setup the bot with your cog
setup(bot)

# Run the bot with your token
bot.run('YOUR DISCORD TOKEN HERE')

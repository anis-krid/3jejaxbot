import discord
from discord.ext import commands
import asyncio
from collections import deque
import time
import re
import urllib.parse
import os
from flask import Flask
import threading
from pytube import YouTube

# ====== إعدادات asyncio ======
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

# ====== التوكن من متغيرات البيئة ======
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("❌ TOKEN not found in environment variables!")

# ====== مسار FFmpeg ======
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# ====== إنشاء البوت ======
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ====== متغيرات ======
queues = {}
now_playing = {}
loop_mode = {}
current_volume = {}
start_time = {}

# ====== إعدادات FFmpeg ======
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -loglevel quiet',
}

# ====== دوال مساعدة ======
def clean_url(url):
    try:
        if 'youtu.be' in url:
            video_id = url.split('/')[-1].split('?')[0]
            return f"https://www.youtube.com/watch?v={video_id}"
        
        if 'youtube.com/watch' in url:
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            video_id = query.get('v', [None])[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
        
        if 'youtube.com/playlist' in url:
            return url
        
        if 'watch' in url and 'v=' not in url:
            match = re.search(r'v=([^&]+)', url)
            if match:
                return f"https://www.youtube.com/watch?v={match.group(1)}"
        
        return url
        
    except Exception as e:
        print(f"Error cleaning URL: {e}")
        return url

def format_duration(seconds):
    if not seconds or seconds == 0:
        return "0:00"
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    return f"{minutes}:{seconds:02d}"

def create_progress_bar(current, total, length=20):
    if not total or total == 0:
        return f"`{'░' * length}` `0:00 / 0:00`"
    
    progress = min(current / total, 1.0)
    filled = int(length * progress)
    bar = "█" * filled + "░" * (length - filled)
    current_str = format_duration(current)
    total_str = format_duration(total)
    
    return f"`{bar}` `{current_str} / {total_str}`"

# ====== كلاس الصوت (باستخدام pytube) ======
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url') or data.get('url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')
        self.requester = None

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        url = clean_url(url)
        print(f"🔍 Cleaned URL: {url}")
        
        try:
            # استخدام pytube بدل yt-dlp
            yt = YouTube(url)
            audio_stream = yt.streams.filter(only_audio=True).first()
            
            if not audio_stream:
                raise Exception("No audio stream found")
            
            audio_url = audio_stream.url
            title = yt.title
            duration = yt.length
            thumbnail = yt.thumbnail_url
            
            print(f"🎵 Title: {title}")
            print(f"⏱️ Duration: {duration} seconds")
            
            source = discord.FFmpegPCMAudio(audio_url, executable=FFMPEG_PATH, **FFMPEG_OPTIONS)
            
            data = {
                'title': title,
                'webpage_url': url,
                'url': audio_url,
                'duration': duration,
                'thumbnail': thumbnail
            }
            
            return cls(source, data=data)
            
        except Exception as e:
            print(f"❌ Error extracting info: {e}")
            raise

# ====== تشغيل الأغنية التالية ======
async def play_next(guild_id):
    if guild_id not in queues or not queues[guild_id]:
        return
    
    voice_client = bot.get_guild(guild_id).voice_client
    if not voice_client:
        return
    
    if loop_mode.get(guild_id, False):
        song = queues[guild_id][0]
    else:
        song = queues[guild_id].popleft() if queues[guild_id] else None
        if not song:
            return
    
    try:
        player = await YTDLSource.from_url(song.url, loop=bot.loop, stream=True)
        player.requester = song.requester
        player.volume = current_volume.get(guild_id, 50) / 100
        
        start_time[guild_id] = time.time()
        
        def after_playing(error):
            if error:
                print(f"Playback error: {error}")
            asyncio.run_coroutine_threadsafe(asyncio.sleep(1), bot.loop)
            asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
        
        voice_client.play(player, after=after_playing)
        now_playing[guild_id] = player
        
        print(f"🔊 Now playing: {player.title}")
        
    except Exception as e:
        print(f"❌ Error playing next: {e}")
        await asyncio.sleep(1)
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

# ====== أزرار التحكم ======
class MusicControls(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
    
    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.primary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("⏸️ Paused!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    
    @discord.ui.button(label="▶️ Resume", style=discord.ButtonStyle.success)
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("▶️ Resumed!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is paused!", ephemeral=True)
    
    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()
            await interaction.response.send_message("⏭️ Skipped!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    
    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client:
            voice_client.stop()
            if self.guild_id in queues:
                queues[self.guild_id].clear()
            await interaction.response.send_message("⏹️ Stopped and cleared queue!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ I'm not in a voice channel!", ephemeral=True)
    
    @discord.ui.button(label="🔄 Loop", style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.guild_id not in loop_mode:
            loop_mode[self.guild_id] = False
        
        loop_mode[self.guild_id] = not loop_mode[self.guild_id]
        status = "enabled" if loop_mode[self.guild_id] else "disabled"
        await interaction.response.send_message(f"🔄 Loop {status}!", ephemeral=True)
    
    @discord.ui.button(label="🔊 +", style=discord.ButtonStyle.primary)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.guild_id not in current_volume:
            current_volume[self.guild_id] = 50
        
        current_volume[self.guild_id] = min(100, current_volume[self.guild_id] + 10)
        if self.guild_id in now_playing:
            now_playing[self.guild_id].volume = current_volume[self.guild_id] / 100
        
        await interaction.response.send_message(f"🔊 Volume: **{current_volume[self.guild_id]}%**", ephemeral=True)
    
    @discord.ui.button(label="🔊 -", style=discord.ButtonStyle.secondary)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.guild_id not in current_volume:
            current_volume[self.guild_id] = 50
        
        current_volume[self.guild_id] = max(0, current_volume[self.guild_id] - 10)
        if self.guild_id in now_playing:
            now_playing[self.guild_id].volume = current_volume[self.guild_id] / 100
        
        await interaction.response.send_message(f"🔊 Volume: **{current_volume[self.guild_id]}%**", ephemeral=True)

# ====== أوامر Slash ======
@bot.tree.command(name="play", description="Play a song from YouTube")
async def slash_play(interaction: discord.Interaction, input: str):
    await interaction.response.defer()
    
    if not interaction.user.voice:
        await interaction.followup.send("❌ You are not in a voice channel!")
        return
    
    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
    
    guild_id = interaction.guild.id
    if guild_id not in queues:
        queues[guild_id] = deque()
    if guild_id not in current_volume:
        current_volume[guild_id] = 50
    
    try:
        player = await YTDLSource.from_url(input, loop=bot.loop, stream=True)
        player.requester = interaction.user
        player.volume = current_volume[guild_id] / 100
        
        queues[guild_id].append(player)
        
        duration_str = format_duration(player.duration) if player.duration else "0:00"
        
        embed = discord.Embed(
            title="🎵 3JEJA MUSIC PANEL",
            description=f"**{player.title}**",
            color=0x1DB954
        )
        if player.thumbnail:
            embed.set_thumbnail(url=player.thumbnail)
        embed.add_field(name="⏱️ Duration", value=f"`{duration_str}`", inline=True)
        embed.add_field(name="📌 Position", value=f"`#{len(queues[guild_id])}`", inline=True)
        embed.set_footer(text=f"Requested by: {interaction.user.display_name}")
        
        view = MusicControls(guild_id)
        await interaction.followup.send(embed=embed, view=view)
        
        if not voice_client.is_playing():
            await play_next(guild_id)
    
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)[:200]}")

@bot.tree.command(name="skip", description="Skip the current song")
async def slash_skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped!")
    else:
        await interaction.response.send_message("❌ Nothing is playing!")

@bot.tree.command(name="pause", description="Pause the current song")
async def slash_pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await interaction.response.send_message("⏸️ Paused!")
    else:
        await interaction.response.send_message("❌ Nothing is playing!")

@bot.tree.command(name="resume", description="Resume the current song")
async def slash_resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await interaction.response.send_message("▶️ Resumed!")
    else:
        await interaction.response.send_message("❌ Nothing is paused!")

@bot.tree.command(name="stop", description="Stop and clear the queue")
async def slash_stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        voice_client.stop()
        guild_id = interaction.guild.id
        if guild_id in queues:
            queues[guild_id].clear()
        await interaction.response.send_message("⏹️ Stopped and cleared queue!")
    else:
        await interaction.response.send_message("❌ I'm not in a voice channel!")

@bot.tree.command(name="leave", description="Disconnect the bot")
async def slash_leave(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        await voice_client.disconnect()
        guild_id = interaction.guild.id
        if guild_id in queues:
            queues[guild_id].clear()
        await interaction.response.send_message("👋 Disconnected!")
    else:
        await interaction.response.send_message("❌ I'm not in a voice channel!")

@bot.tree.command(name="queue", description="Show the current queue")
async def slash_queue(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id not in queues or not queues[guild_id]:
        await interaction.response.send_message("📭 Queue is empty!")
        return
    
    queue_list = []
    for i, song in enumerate(queues[guild_id], 1):
        duration_str = format_duration(song.duration) if song.duration else "0:00"
        queue_list.append(f"**{i}.** {song.title} `[{duration_str}]`")
    
    queue_text = "\n".join(queue_list[:10])
    if len(queue_list) > 10:
        queue_text += f"\n...and {len(queue_list) - 10} more"
    
    await interaction.response.send_message(f"📋 **Queue:**\n{queue_text}")

@bot.tree.command(name="now", description="Show the currently playing song")
async def slash_now(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    guild_id = interaction.guild.id
    
    if voice_client and voice_client.is_playing():
        if guild_id in now_playing:
            song = now_playing[guild_id]
            
            current_time = time.time()
            elapsed = current_time - start_time.get(guild_id, current_time)
            progress_bar = create_progress_bar(elapsed, song.duration) if song.duration else "0:00"
            
            embed = discord.Embed(
                title="🎵 Now Playing",
                description=f"**{song.title}**",
                color=0x1DB954
            )
            if song.requester:
                embed.set_footer(text=f"Requested by: {song.requester.display_name}")
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            
            embed.add_field(name="📊 Progress", value=progress_bar, inline=False)
            
            await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ Nothing is playing!")

@bot.tree.command(name="loop", description="Toggle loop mode")
async def slash_loop(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id not in loop_mode:
        loop_mode[guild_id] = False
    
    loop_mode[guild_id] = not loop_mode[guild_id]
    status = "enabled" if loop_mode[guild_id] else "disabled"
    await interaction.response.send_message(f"🔄 Loop {status}!")

@bot.tree.command(name="volume", description="Set the volume (0-100)")
async def slash_volume(interaction: discord.Interaction, volume: int):
    if 0 <= volume <= 100:
        guild_id = interaction.guild.id
        current_volume[guild_id] = volume
        if guild_id in now_playing:
            now_playing[guild_id].volume = volume / 100
        await interaction.response.send_message(f"🔊 Volume set to **{volume}%**")
    else:
        await interaction.response.send_message("❌ Volume must be between 0 and 100!")

# ====== خادم ويب لـ Keep Alive ======
app = Flask(__name__)

@app.route('/')
def home():
    return "🎵 3JEJA Bot is alive!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

# ====== تشغيل البوت ======
@bot.event
async def on_ready():
    print(f"✅ Bot is running as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")

if __name__ == "__main__":
    keep_alive()
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Error: {e}")
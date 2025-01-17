import os

from instaloader import Instaloader, Post
from telegram import Update, TelegramObject
from telegram.ext import ConversationHandler, filters, MessageHandler, CommandHandler, TypeHandler, ApplicationBuilder, ContextTypes, ApplicationHandlerStop
from mysecrets import BOT_TOKEN
import ffmpeg
import subprocess as sp
import glob
import requests
import json
import gzip
import time
import datetime
import functools


os.chdir("/home/frame/cuteframe/")

insta = Instaloader(
    filename_pattern='{shortcode}',
    max_connection_attempts=1,
    save_metadata=False,
    download_comments=False,
    download_geotags=False,
    download_pictures=False,
    post_metadata_txt_pattern='',
    request_timeout=300
)
player = sp.Popen("exec mpv --fs --loop out/default.mp4", shell=True, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
sp.run("gpio -g mode 18 pwm && gpio pwmc 100", shell=True)

# When was the frame media updated last
when_updated_timestamp = datetime.datetime.now().replace(microsecond=0)

# The name of the file being displayed at the moment
file_being_displayed = 'out/default.mp4'


def record_when_updated() -> None:
    global when_updated_timestamp
    when_updated_timestamp = datetime.datetime.now().replace(microsecond=0)

def clear_tmp() -> None:
    files = glob.glob('tmp/*')
    for f in files:
        os.remove(f)

def update_display(file_path: str) -> None:
    global player
    global file_being_displayed
    player.kill()
    player.wait()
    if not os.path.exists(file_path):
        raise Exception("No file exists to display")
    player = sp.Popen(f"exec mpv --fs --loop {file_path}", shell=True, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    file_being_displayed = file_path
    clear_tmp()
    record_when_updated()

def resize_media(in_path: str, out_path: str) -> str:
    global player
    player.kill()  # Kill the player so we have some CPU for ffmpeg!

    if os.path.isfile(out_path):
        return out_path

    # Get the width and height
    probe = ffmpeg.probe(in_path)
    width, height = probe['streams'][0]['width'], probe['streams'][0]['height']
    print(f'Input file is {width}x{height}')

    stream = ffmpeg.input(in_path).video

    # Crop to the correct aspect ratio
    smallest_side = min(width, height)
    if width != height:
        excess_height = height - smallest_side
        excess_width = width - smallest_side
        stream = stream.crop(x=excess_width//2, y=excess_height//2, width=smallest_side, height=smallest_side)

    # Scale to match display
    if smallest_side != 720:
        stream = stream.filter('scale', 720, 720)

    # Different output command for images
    if in_path.endswith(('.png', '.jpg', '.jpeg')):
        stream = stream.output(out_path, vframes=1)
    else:
        stream = stream.output(out_path, vcodec='h264_v4l2m2m')

    stream.run(overwrite_output=True)
    return out_path

async def download_media(obj: TelegramObject, context: ContextTypes.DEFAULT_TYPE) -> str:
    print(f'Downloading {obj}')
    file = await context.bot.get_file(obj)
    out_file_path = f'tmp/{file.file_id}.{file.file_path.split(".")[-1]}'
    await file.download_to_drive(out_file_path)
    return out_file_path

def tgs_to_mp4(tgs_file_path: str) -> str | None:
    try:
        # A telegram sticker is just a Lottie JSON that has been gzipped
        with gzip.open(tgs_file_path) as f:
            lottie_json = json.loads(f.read())

        # Use "API" taken from https://lottietovideo.com/
        r = requests.post('https://l73mqtglr0.execute-api.eu-west-1.amazonaws.com/prod/', json={'name': 'lottietovideo', 'animation': lottie_json})
        if r.status_code != 200:
            print(f"Got error code {r.status_code} from lottietovideo API POST")
            return None

        tgs_id = r.json()['id'].split('-')[-1]

        # Wait for the video to be ready
        retry_count = 0
        while retry_count < 5 and requests.head(f"https://d2f5b11l106s2w.cloudfront.net/lottietovideo-{tgs_id}.mp4").status_code != 200:
            time.sleep(2)
            retry_count += 1

        if retry_count == 5:
            print(f"Lottietovideo API HEAD timeout")
            return None

        r = requests.get(f"https://d2f5b11l106s2w.cloudfront.net/lottietovideo-{tgs_id}.mp4")
        if r.status_code != 200:
            print(f"Got error code {r.status_code} from lottietovideo API GET")
            return None

        out_fp = f"{tgs_file_path.rstrip('.tgs')}.mp4"
        with open(out_fp, "wb") as f:
            f.write(r.content)

        return out_fp

    except Exception as e:
        print(e)
        return None


'''
Decorator that sends a message saying if the operation succeeded or not, based on an exception getting raised or not
'''
def respond_with_result(func):
    @functools.wraps(func)
    async def wrapper_respond_with_result(*args, **kwargs):
        ret = None
        try:
            ret = await func(*args, **kwargs)
            await args[0].message.reply_text("Display successfully updated")
        except Exception as e:
            await args[0].message.reply_text(f"Failed to update display with exception: {e}")
        return ret
    return wrapper_respond_with_result


'''
The following are all handlers called by the Python Telegram Bot in response to messages from the user
'''

@respond_with_result
async def url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global insta
    insta_shortcode = update.message.text.split('/reel/')[1].split('/')[0]
    print(f"Got shortcode {insta_shortcode} from url: {update.message.text}")
    post = Post.from_shortcode(insta.context, insta_shortcode)
    insta.download_post(post, 'tmp')
    update_display(resize_media(f'tmp/{insta_shortcode}.mp4', f'out/{insta_shortcode}.mp4'))

@respond_with_result
async def sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out_file_path = await download_media(update.message.sticker, context)
    tgs_mp4 = tgs_to_mp4(out_file_path)
    if tgs_mp4 is None:
        raise Exception("Failed to turn sticker to MP4")
    update_display(resize_media(tgs_mp4, f'out/{tgs_mp4.split("/")[-1]}'))

@respond_with_result
async def gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    out_file_path = await download_media(update.message.animation, context)
    update_display(resize_media(out_file_path, f'out/{out_file_path.split("/")[-1]}'))

@respond_with_result
async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    out_file_path = await download_media(update.message.photo[-1], context)
    update_display(resize_media(out_file_path, f'out/{out_file_path.split("/")[-1]}'))


async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"Got something unexpected: {update.message}")
    await context.bot.send_message(update.message.chat_id, "Sorry, I don't understand that command")

async def when_updated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Asked when was the last media update")
    date_diff = datetime.datetime.now() - when_updated_timestamp
    date_components = str(date_diff).split(':')
    await update.message.reply_text(f"Time since last update: {date_components[0]} hours and {int(date_components[1])} minutes")

async def whats_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Asked whats on the display")
    if file_being_displayed.endswith('.mp4'):
        await update.message.reply_video(file_being_displayed)
    elif file_being_displayed.endswith(('.jpg', '.jpeg', '.png')):
        await update.message.reply_photo(file_being_displayed)
    else:
        await update.message.reply_text(f'Unsupported file being displayed: {file_being_displayed}')

def set_brightness(percentage: int) -> None:
    value = 1023 * (1 - (percentage / 100))
    sp.run(f"gpio -g pwm 18 {value}", shell=True) # 0 is brightest, 1023 is dimmest

async def brightness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    print(f"Got brightness command with args: {context.args}")
    if len(context.args) > 0:
        try:
            set_brightness(int(context.args[0]))
            return ConversationHandler.END
        except ValueError:
            pass
    await update.message.reply_text("Now send a number between 0 and 100")
    return 0

async def brightness_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        set_brightness(int(update.message.text))
        return ConversationHandler.END
    except ValueError:
        pass
    await update.message.reply_text("Try again! Send a number between 0 and 100 (or /cancel to give up!)")
    return 0

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Try again another time!")
    return ConversationHandler.END

async def update_sleep_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    print(f"Got sleep schedule update command with args: {context.args}")
    try:
        hour, minute = [int(x) for x in context.args[0].split(':')]
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError
        
        for job in context.job_queue.get_jobs_by_name(name):
            callback = job.callback
            job.schedule_removal()

        context.job_queue.run_daily(callback, time=datetime.time(hour=hour, minute=minute), name=name)
        return
    except:
        pass
    # If we get here, the input was invalid
    await update.message.reply_text("You must include a time in the format HH:MM")

async def bedtime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_sleep_schedule(update, context, 'bedtime')

async def risetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_sleep_schedule(update, context, 'risetime')

async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Turning off!")
    os.system("sudo shutdown -h now")

async def reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Rebooting!")
    os.system("sudo reboot")

async def restrict_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in [1158879753, 1203514639]:
        pass
    else:
        await update.effective_message.reply_text("Hey! You are not allowed to use me!")
        raise ApplicationHandlerStop

async def post_init(application) -> None:
    await application.bot.set_my_commands([('brightness', 'Set the brightness [0-100]'),
                                           ('whenupdated', 'Find out when the last media update was received'),
                                           ('whatson', 'Find out what is currently being displayed'),
                                           ('bedtime', 'Set the time to turn off the display [hh:mm]'), 
                                           ('risetime', 'Set the time to turn on the display [hh:mm]'),
                                           ('shutdown', 'Shutdown safely'), 
                                           ('reboot', 'Reboot')])

async def display_off(_: ContextTypes.DEFAULT_TYPE):
    print("Scheduler says turn off display!")
    set_brightness(0)

async def display_on(_: ContextTypes.DEFAULT_TYPE):
    print("Scheduler says turn on display!")
    set_brightness(100)

# Make directories if they don't exist
if not os.path.exists('out'):
    os.makedirs('out')
if not os.path.exists('tmp'):
    os.makedirs('tmp')

print('About to build bot!')

app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

app.add_handler(TypeHandler(Update, restrict_users), -1)

app.add_handler(ConversationHandler(entry_points=[CommandHandler('brightness', brightness)], states={0: [MessageHandler(filters.TEXT & ~filters.COMMAND, brightness_value)],}, fallbacks=[CommandHandler("cancel", cancel)]))
app.add_handler(CommandHandler('shutdown', shutdown))
app.add_handler(CommandHandler('reboot', reboot))
app.add_handler(CommandHandler('bedtime', bedtime))
app.add_handler(CommandHandler('risetime', risetime))
app.add_handler(CommandHandler('whenupdated', when_updated))
app.add_handler(CommandHandler('whatson', whats_on))
app.add_handler(MessageHandler(filters.PHOTO, photo))
app.add_handler(MessageHandler(filters.ANIMATION, gif))
app.add_handler(MessageHandler(filters.Sticker.ALL, sticker))
app.add_handler(MessageHandler(filters.TEXT & (filters.Entity("url") | filters.Entity("text_link")), url))
app.add_handler(MessageHandler(filters.ALL, catch_all))

app.job_queue.run_daily(display_off, time=datetime.time(hour=23, minute=30), name='bedtime')
app.job_queue.run_daily(display_on, time=datetime.time(hour=7, minute=30), name='risetime')

print('Entering bot polling loop')
app.run_polling()

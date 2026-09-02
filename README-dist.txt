welcome to 0.0.2v we making it out of the streets with this one

put the media that you want to show up in the media folder and in their respective video/image/gif folder
some media should already be provided by me and hold examples of the configuration you can do

you can add some audio with the media to play along just make sure the name format is the same as the image/gif/video

if youre confused just check the files yourself and do what im already doing lol

for config.json:
tick_seconds: the tick rate at which the program counts per second (default 1)
odds: the 1/X chances (default 10 for 1/10 chances)
max_concurrent: maximum number of media that may overlap
reroll_in_same_tick: if media successfully shows up can reroll again to try and show multiple medias at once for more chaos (default true)
media_folder: default media path (default "media")
image_display_seconds: how long images last (default 2)
fade_in_seconds, fade_out_seconds: how long images fade in/out (default 0) (note total duration is image_display + fade_in + fade_out)
monitor: doesn't do anything yet lol
volume: change audio volume
opacity: change opacity
speed: change speed
pitch: change pitch
speed-pitch: change both speed and pitch this can be useful for random generation and keep both speed and pitch bound together
chroma_key: doesn't do anything yet lol part 2
position:absolute position of the media relative to screen. where for example position_x = 0 means right edge of the media sticks to the right edge of the screen and 1 means left edge of the media sticks to the left edge of the screen
scale: (need to add a single value for scale to not get media squish) relative to screen size. original point of reference is the mode:"fit" which tried to fit the media in the entire screen. and scale will be multiplied by that.
rotation: do a barrel roll (0-360 range)
flip:ǝslɐɟ ɹo ǝnɹʇ oʇ ʇᴉ ʇǝs ˙ʎllɐɔᴉʇɹǝʌ ɹo ʎllɐʇuozᴉɹoɥ ɐᴉpǝɯ ǝɥʇ sdᴉlɟ
AutoStart: doesn't do anything yet lol part 3
debug: to print stuff to the console (default true)
download_max_height: useless tbh unless you have source code :sob:
rescan_seconds: rate at which the program rescans the media folder to add new media or remove them (default 60)
show_console: if process runs on console or background (default true and must stay true else youll have to turn that shit off by task manager :sob:)

NOTE!!!: the position rotation scale, flip and any transform change can only be applied when the media mode is set to "custom" so make sure to change it

as for RANDOMNESS it can apply to most values (volume pitch position rotation scale flip)
for numbers you need to give it a STRING format "min~max" with ~ as a delimiter (didnt use - because thats the negative sign for numbers)
for booleans you can use "random" as a parameter

for more details refer to example.json which has "hints" and most of the parameters already tweaked use it as a template 

also also you would have app.log generated which shows what the app did so far idk if its important to 99% of the users if there will be any

(need to add weights for chance of appearance and an individual scale parameter to not stretch media)
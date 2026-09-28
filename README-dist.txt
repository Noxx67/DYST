on that v1.0 release im making it out the trenches with this one 

put the media that you want to show up in the media folder and in their respective video/image/gif folder
some media should already be provided by yours truly and hold examples of the many configurations you can do

stuff you can do:

you can add audio with the media to play along just make sure they share the same name 
>"img.png + img.mp3" means the mp3 plays along the image

shit ton of configuration all with the option to randomize them.
>for numerical values just use ~ between two number (ex: scale: "0.2~2" the " are important dont forget them)
>for categorical values/booleans just use "random" (ex: flip_h: random -> randomly flips the media horizontally)

you have both global config and configuration for every media and just like the audio they need to share the same name

some of the configs you can do include:
the chances of media appearing each tick, the tick rate, the maximum amount of media on screen,
position rotation scale and all that,
speed and volume
media chance weights
green screen removal using chroma key (supports multiple colors too even black and white wow so cool!1!!1)
run the program when windows starts up

all of them will be explained in detailed in the hints section of config.json (global) and config_template.json (template for the per-file configuration)

and if youre confused just check the files yourself and do what im already doing lol

IMPORTANT: now in the case the media get stuck or becomes too overwhelming you have 2 ways to kill the program
>shortcut: ctrl+shift+k
>tray icon in the bottom right that shows a menu where you can pause the media or stop the program

you also have app.log generated which shows what the app did so far idk if its important to 99% of the users if there will be any

shit to add next:
gui configuration for json files
some kind of server to upload and download user made media packs and configs (as if lmao)
linux support (idfk)

any bug reports or suggestions id recommend going on the repo
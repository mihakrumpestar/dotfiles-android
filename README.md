# Android dotfiles

## Prerequisites

Find phone on [xdaforums](https://xdaforums.com). Make sure you unlocked the bootloader.

Steps:

1. Install recovery (TWRP - at least 3.7, find latest version [https://twrp.me/](https://twrp.me/) using fastboot (with power + volume down)):

   ```sh
   fastboot devices
   fastboot flash recovery [recovery.img]
   fastboot boot [recovery.img]
   ```

   Reboot to recovery (with power + volume up)

2. Install latest firmware from [here](https://xmfirmwareupdater.com/firmware/surya/) using recovery.

3. Installation (in recovery):

   1. First time installation:

      - Wipe Dalvik, Cache and format data. This IS NOT OPTIONAL!
      - Reboot
      - Flash Rom
      - Flash [Gapps](https://nikgapps.com/downloads) (if required)
      - Flash KernelSU `boot.img` (if required)
      - Reboot to system

   2. Update installation:

      - Flash ROM zip file
      - Wipe Dalvik/cache
      - Reboot to system

## System setup

0. Setting:

   1. Developer options:
      - All `*animation scale`: animation off

1. Setup folder `phone-sync` and sync from server using Round Sync. Or using USB drive.

2. Install [Obtainium](https://github.com/ImranR98/Obtainium/releases). Restore from export.

3. Pair KDE Connect.

4. Install KernelSU modules:

   - [Open Webview](https://github.com/Magisk-Modules-Alt-Repo/open_webview/releases): choose `Vanadium`, activate it in Developer settings if not already
   - BCR ([Basic Call Recorder](https://github.com/chenxiaolong/BCR/releases)): enable it, set backup path and delete policy
   - [Rucky](https://raw.githubusercontent.com/mayankmetha/Rucky/master/nightly/rucky.zip)

5. Manual setup:

   - Obtanium: import and export config
   - Simple Contacts and Simple Messages: restore. Set automatic backups on Contacts, Massages have manual backup only.
   - Iceraven:

     1. In app settings “Set as default browser”
     2. Add-ons:
        - CanvasBlocker
        - Dark Reader
        - Flagfox
        - Grammar and Spell Checker - LanguageTool
        - I still don’t care about cookies
        - uBlock Origin

   - OsmAnd~:

     1. backup and restore
     2. install all required maps (Slovenia and Croatia)

Manually install `EasyPark: net.easypark.android` from `Aurora Store`.

Other:

- backup pictures and downloads
- don’t forget to run Round Sync to copy new backups to server
- [Bunny (Vandetta) plugins](https://bn-plugins.github.io/vd-web/#)

## Options I am not using

HID emulation:

- [Android_HID_Keyboard](https://github.com/Sucareto/Android_HID_Keyboard): only live input, can't paste/inject, mouse and keyboard support, requires profile in "android-usb-gadget"
- [android-usb-script](https://github.com/Netdex/android-usb-script): crashes if try to load any asset
- [android-usb-gadget](https://github.com/tejado/android-usb-gadget): can only add HID devices, which some apps need
- [android-hid-client](https://github.com/Arian04/android-hid-client): character device never initializes correctly

App freezing:

- [Drowser](https://gitlab.com/juanitobananas/drowser): best
- [SuperFreezZ](https://gitlab.com/SuperFreezZ/SuperFreezZ): discontinued
- [BatteryTool](https://github.com/Domi04151309/BatteryTool): app gets updated, but builds are not published for 2 years already

Launchers:

- [lunar-launcher](https://github.com/iamrasel/lunar-launcher): gestures are too clunky, a lot of useless and distracting panels, can't set to my wallpaper
- [Olauncher](https://github.com/tanujnotes/Olauncher): ok, but missing a lot of settings
- [EasyLauncher](https://github.com/DroidWorksStudio/EasyLauncher): ok, but no gestures and missing extra settings

## Optional

No needing currently:

- https://github.com/nikita-yfh/android-adb-tools
- https://apkpure.net/cssbuy/cn.onebound.cssbuy
- https://github.com/readrops/Readrops
- https://github.com/newhinton/disky
- https://apkpure.com/flud-torrent-downloader/com.delphicoder.flud
- https://github.com/anilbeesetti/nextplayer
- https://github.com/NeoApplications/Neo-Backup

Might use in the future:

- https://github.com/celzero/rethink-app: does not have option to run as root
- https://github.com/TrackerControl/tracker-control-android: does not have option to run as root

Apps no longer in use/installed:

- [Advanced Charging Controller](https://github.com/VR-25/acc/releases): Android 14 has "Charging control"
- [termux](https://github.com/termux/termux-app): deprecated as last update was Jan 2022
- FolderSync: replaced with Round Sync
- X - Twitter: replace with Squawker
- AdAway: does not work with KernelSU
- OnlyOffice
- [ReVanced](https://revanced.org/#main): non-root install ReVanced and microG
- Speedtest
- Ping Tools: replaced with Ping Utils
- geteduroam → Login collage Wi-Fi access
- Davx5
- MX Player: replaced with Next Player
- Battery Charge Limit: input_suspend - deprecated
- MyFitnessPal → Login
- Shelter → for sandboxing apps (allows having a second cloned app)
- Super Backup:

  1. Backup/restore
  2. set scheduled backup of Contacts, Messages and Call history

- OpenDocument Reader
- Sleep Cycle → Login
- Splitwise
- Slack
- Brave Private Web Browser
- APKPure
- Cinema-FV5
- Filmic Pro
- Lucky Patcher
- Moodle → Login
- OpenVPN for Android: now using WireGuard
- Xplore TV → Login
- RHVoice:

  1. download English (United States) → SLT voice
  2. Settings → Accessibility → Text-to-speach output → Preferred engine → select it again to activate

- BBS (betterbatterystats)
- Island (no longer mentained): replaced by Shelter (work profile)

## Setups

- Termux:

  1. [SSH](https://wiki.termux.com/wiki/Remote_Access):

     ```sh
     pkg install busybox termux-services
     source $PREFIX/etc/profile.d/start-services.sh

     pkg upgrade
     pkg install openssh openssh-sftp-server

     sv-enable sshd
     sv up sshd
     sv down sshd

     # or
     sshd
     pkill sshd

     $PREFIX/etc/ssh/sshd_config

     PrintMotd yes
     PasswordAuthentication no
     Subsystem sftp /data/data/com.termux/files/usr/libexec/sftp-server

     echo "[authorized key]" >> $HOME/.ssh/authorized_keys

     # IP
     ifconfig

     # Debug
     logcat -s 'sshd:*'
     ```

  2. Install other packages ([VS Code guide](https://www.reddit.com/r/termux/comments/13hwh9u/official_vscode_server_with_termux_terminal_code/?utm_source=share&utm_medium=web2x&context=3)):

     ```sh
     pkg upgrade

     # Additional repos
     pkg install x11-repo root-repo

     pkg install termux-api # https://wiki.termux.com/wiki/Termux:API

     # Arch and VSCode
     yes | pkg update
     yes | pkg install proot-distro
     proot-distro install archlinux
     proot-distro login archlinux

     # Install yay
     useradd -m -G wheel tempuser
     passwd tempuser
     echo "%wheel   ALL=(ALL:ALL) NOPASSWD: ALL" > /etc/sudoers.d/wheel
     echo "tempuser   ALL=(ALL:ALL) NOPASSWD: ALL" > /etc/sudoers.d/tempuser
     # relogin
     # su tempuser

     pacman -S --needed git base-devel
     git clone https://aur.archlinux.org/yay-bin.git
     cd yay-bin
     sudo -u tempuser makepkg -si # can't run as root user which is the default in proot-distro

     pacman -Syu base-devel git openssh
     # curl -L https://update.code.visualstudio.com/latest/cli-linux-arm64/stable | bsdtar -xf - -C /usr/local/bin
     yay -S visual-studio-code-bin
     code tunnel --accept-server-license-terms
     ```

## Hacks

Inject text to keyboard:

```sh
adb shell input text "insert%syour%stext%shere"
```

## Fixes

NewPipe fix:

```sh
adb devices
adb shell

# pkg search zip

pkg install zip tsu

su

mkdir -p /sdcard/temp

cp /data/data/org.polymorphicshade.newpipe/databases/newpipe.db /sdcard/temp/newpipe.db
cp /data/data/org.polymorphicshade.newpipe/databases/newpipe.settings /sdcard/temp/newpipe.settings

exit

cd /sdcard/temp
sudo zip newpipe.zip newpipe.db newpipe.settings

# zip them and import
```

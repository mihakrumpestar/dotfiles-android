# Android dotfiles

## Root methods

- Magisk (installs by flashing in recovery, detectable by banking apps)
- KernelSU (has to be supported/included with ROM, undetectable)
- APatch (flashes like Magisk, runs like KernelSU)

## ROMs

- [crDroid](https://crdroid.net/downloads)

## Prerequisites

Find phone on [xdaforums](https://xdaforums.com). Make sure you unlocked the bootloader.

Verify phone [here](https://www.mi.com/global/verify#/en/tab/imei) (you also get the region).

Steps:

1. Check phone codename:

   ```sh
   fastboot getvar product
   ```

2. Check that phone is unlocked:

   ```sh
   fastboot getvar unlocked
   ```

3. Install recovery (TWRP - at least 3.7, find latest version [https://twrp.me/](https://twrp.me/) using fastboot (with power + volume down)):

   ```sh
   fastboot devices
   fastboot flash recovery <recovery.img>
   fastboot boot <recovery.img>
   ```

   Reboot to recovery (with power + volume up)

4. Install latest firmware from [here](https://xmfirmwareupdater.com/firmware/surya/) using recovery.

5. Installation (in recovery):

   1. First time installation:

      - Wipe Dalvik, Cache and Format data. This IS NOT OPTIONAL!
      - Reboot recovery.
      - Flash ROM:

         ```sh
         adb sideload <*.zip>
         ```

      - Flash [Gapps](https://nikgapps.com/downloads) (if required)
      - Flash Magisk. (rename `.apk` to `.zip`)
      - Reboot to system

   2. Update installation:

      - Flash ROM zip file
      - Do NOT wipe Dalvik/cache now!
      - Reboot to system

## System setup

0. Settings:

   1. Accessibility:

      - Disable animations: true

1. Setup folder `phone-sync` and sync from server using Round Sync. Or using USB drive.

2. Install [Obtainium](https://github.com/ImranR98/Obtainium/releases). Restore from export.

3. Pair KDE Connect.

4. Install root modules:

   - BCR ([Basic Call Recorder](https://github.com/chenxiaolong/BCR/releases)): install app separately, enable it, set backup path and delete policy
   - [ScreenshotsEverywhere](https://github.com/vova7878-modules/ScreenshotsEverywhere/releases): screenshot anything

   Deprecated:

      - [Open Webview](https://github.com/Magisk-Modules-Alt-Repo/open_webview/releases): choose `Vanadium`, activate it in Developer settings if not already; not working on crDroid 14 kernel
      - [Rucky](https://raw.githubusercontent.com/mayankmetha/Rucky/master/nightly/rucky.zip): not working on crDroid 14 kernel
      - [microg_installer_revived](https://github.com/nift4/microg_installer_revived/releases): after already installed depending modules/apps using Obtainium

5. Manual setup:

   - Simple Contacts and Simple Messages: restore. Set automatic backups on Contacts, Massages have manual backup only.
   - Iceraven:

     1. In app settings “Set as default browser”
     2. Add-ons:
        - Dark Reader
        - Grammar and Spell Checker - LanguageTool
        - I still don't care about cookies
        - uBlock Origin

   - OsmAnd~:

     1. backup and restore
     2. install all required maps (Slovenia and Croatia)

Manually install `EasyPark: net.easypark.android` from `Aurora Store` (as no source supported by Obtainium has it available).

Other:

- backup pictures and downloads
- don’t forget to run Round Sync to copy new backups to server
- [Bunny (Vandetta) plugins](https://bn-plugins.github.io/vd-web/#)

## Optional

Not needing currently:

- [android-adb-tools](https://github.com/nikita-yfh/android-adb-tools)
- [cssbuy](https://apkpure.net/cssbuy/cn.onebound.cssbuy)
- [Readrops](https://github.com/readrops/Readrops)
- [disky](https://github.com/newhinton/disky)
- [flud](https://apkpure.com/flud-torrent-downloader/com.delphicoder.flud)
- [nextplayer](https://github.com/anilbeesetti/nextplayer)
- [Neo-Backup](https://github.com/NeoApplications/Neo-Backup)

Might use in the future:

- [rethink-app](https://github.com/celzero/rethink-app): does not have option to run as root
- [tracker-control-android](https://github.com/TrackerControl/tracker-control-android): does not have option to run as root

Apps no longer in use/installed:

- [mLauncher](https://github.com/DroidWorksStudio/mLauncher): buggy/unoptimized search, replaced with EasyLauncher
- [KeePassDX](https://github.com/Kunzisoft/KeePassDX): [Key Driver](https://apkpure.net/key-driver/com.kunzisoft.hardware.key) for it does not work on Android 14 (latest version only, older works)
- [Advanced Charging Controller](https://github.com/VR-25/acc/releases): Android 14 has build in "Charging control" (at least my ROM)
- FolderSync: replaced with Round Sync
- X - Twitter: replace with Squawker
- AdAway: does not work with KernelSU
- OnlyOffice
- [ReVanced](https://revanced.org/#main): non-root install ReVanced and microG
- Speedtest
- Ping Tools: replaced with Ping Utils
- Davx5
- MX Player: replaced with Next Player
- Battery Charge Limit: input_suspend - deprecated
- Shelter → for sandboxing apps (allows having a second cloned app)
- Super Backup:

  1. Backup/restore
  2. set scheduled backup of Contacts, Messages and Call history

- APKPure
- Cinema-FV5
- Filmic Pro
- Lucky Patcher
- RHVoice:

  1. download English (United States) → SLT voice
  2. Settings → Accessibility → Text-to-speach output → Preferred engine → select it again to activate

- BBS (betterbatterystats)
- Island (no longer maintained): replaced by Shelter (work profile)

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

# Android dotfiles

## Root methods

- Magisk (installs by flashing in recovery, detectable by banking apps)
- KernelSU Next (has to be supported/included with ROM, undetectable)
- APatch (flashes like Magisk, runs like KernelSU)

## ROMs

- [crDroid](https://crdroid.net/downloads)

## Prerequisites

Find phone on [xdaforums](https://xdaforums.com). Make sure you unlocked the bootloader.

Verify phone [here](https://www.mi.com/global/verify#/en/tab/imei) (you also get the region).

Get whole device info:

```sh
adb shell getprop

# Get list of all packages
adb shell pm list packages -3 | cut -f2 -d ':'
```

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

      - Reboot to system

   2. Update installation:

      - Flash ROM zip file
      - Do NOT wipe Dalvik/cache now!
      - Reboot to system

## System setup

0. Settings

1. Install:

   - [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next/releases)
   - [Shizuku](https://github.com/RikkaApps/Shizuku/releases)
   - [Obtainium](https://github.com/ImranR98/Obtainium/releases). Restore from export.

2. Setup sync folders.

3. Pair KDE Connect.

4. Install root modules:

   Not working on newer Android versions (15, 16):

   - [Basic Call Recorder](https://github.com/chenxiaolong/BCR/releases): install app separately, enable it, set backup path and delete policy

   Causing failure to boot:

   - [Simple-Flag-Secure](https://github.com/ShivamXD6/Simple-Flag-Secure): screenshot/record even if app has FLAG SECURE

5. Manual setup:

   - Simple Contacts and Simple Messages: restore. Set automatic backups on Contacts, Massages have manual backup only.
   - IronFox:

     1. In app settings “Set as default browser”
     2. Add-ons:
        - Dark Reader
        - I still don't care about cookies
        - uBlock Origin

   - OsmAnd~:

     1. backup and restore
     2. install all required maps (Slovenia and Croatia)

Other:

- backup pictures and downloads
- run Round Sync to copy new backups to server

## Optional

Apps no longer in use/installed:

- [Termux](https://github.com/termux/termux-app): just don't have any use for it
- [mLauncher](https://github.com/DroidWorksStudio/mLauncher): buggy/unoptimized search, replaced with EasyLauncher, replaced that one with Yam Launcher
- [KeePassDX](https://github.com/Kunzisoft/KeePassDX): [Key Driver](https://apkpure.net/key-driver/com.kunzisoft.hardware.key) for it does not work on Android 14 (latest version only, older works)
- AdAway: does not work with KernelSU
- Davx5
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
- Shelter → for sandboxing apps (allows having a second cloned app), now already build-in
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

# Custom ROM

For the personal phone. The work phone stays on its stock firmware and only follows [Debloat](debloat.md); both share [Shared setup](shared-setup.md).

Device models and codenames are not recorded here. Check yours before downloading anything: `fastboot getvar product`.

Choosing a phone matters more than choosing a ROM, because an unlockable bootloader is getting rare: [bootloader-unlock-wall-of-shame](https://github.com/zenfyrdev/bootloader-unlock-wall-of-shame).

## Root methods

- Magisk: installs by flashing in recovery, detectable by banking apps.
- KernelSU Next: has to be supported or included by the ROM, undetectable.
- APatch: flashes like Magisk, runs like KernelSU.

## ROMs

- [crDroid](https://crdroid.net/downloads): clean AOSP base with the hardware fixes this class of device needs.

Check that the ROM project lists your exact model before unlocking. An unsupported model usually boots, then loses a subsystem you cannot fix without the vendor firmware.

## Flashing

1. Check the device codename:

   ```sh
   fastboot getvar product
   ```

2. Check that the bootloader is unlocked:

   ```sh
   fastboot getvar unlocked
   ```

3. Install recovery, at least TWRP 3.7, with fastboot (power and volume down):

   ```sh
   fastboot devices
   fastboot flash recovery <recovery.img>
   fastboot boot <recovery.img>
   ```

   Reboot to recovery with power and volume up.

4. Install the latest stock firmware for the device, from the vendor or a firmware archive, using recovery. Do this before the ROM on a device that has been sitting on an old firmware.

5. Install the ROM:

   First time installation:

   - Wipe Dalvik, cache and format data. This is not optional.
   - Reboot recovery.
   - Flash the ROM:

     ```sh
     adb sideload <rom.zip>
     ```

   - Reboot to system.

   Update installation:

   - Flash the ROM zip.
   - Do not wipe Dalvik or cache.
   - Reboot to system.

6. Read [Shared setup](shared-setup.md) for the apps and settings both phones use.

## Device info

```sh
adb shell getprop
adb shell pm list packages -3 | cut -f2 -d ':'
```

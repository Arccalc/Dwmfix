# DWMfix

## The Problem
Windows 11 aggressive energy-saving features can sometimes throttle Desktop Window Manager (DWM) composition on secondary displays or when playing full-screen games, resulting in a perceived stutter on secondary screens.

## The Solution
DWMfix is a background utility that forces the Windows DWM to maintain high-performance composition by constantly rendering an imperceptible transparent widget. This prevents the OS from putting the rendering layer into a low-power, low-refresh state, keeping your second monitor stutter-free.

## How to use
1. Run `DWMfix.exe`.
2. A control panel will appear where you can manage the tool, toggle boost mode, or hide it to the system tray.
3. Keep it running in the tray while gaming or using your secondary monitor.

## Support the Developer
If this tool helped you fix your dual monitor stuttering, consider supporting the development!

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/pixelcraft404)
